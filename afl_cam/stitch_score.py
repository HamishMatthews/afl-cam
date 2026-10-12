"""Stitch quality score: how badly the overlap ghosts, as numbers.

Given one camera-pair frame and a live map set, warp each camera into canvas
space on its own and compare the two views where both have pixels:

  misalign_*  residual displacement between the views (px, canvas space) —
              this IS the ghosting. Tiled normalised
              cross-correlation after a sigma-8 blur (CLAUDE.md metric
              traps: unblurred cross-camera texture over-reads; ray-angle
              residuals measure toe-in, not ghosting).
  luma_*      exposure mismatch between the views over the overlap: the
              brightness step a viewer sees where the blend hands over.

`render_seam_crop` saves the blended overlap (the shader's output, via the
VideoStitcher reference) beside a red/green edge overlay for eyeballing.
"""

from pathlib import Path
from typing import Optional

import cv2
import numpy as np

from . import stitch_maps


def warp_views(f0: np.ndarray, f1: np.ndarray, live: stitch_maps.LiveMaps):
    """Each camera warped alone into the full canvas, plus its valid mask."""
    cw, ch = live.canvas
    views, masks = [], []
    for cam, frame in zip(live.cams, (f0, f1)):
        mh, mw = cam.map.shape[:2]
        x0, y0 = cam.off
        warp = cv2.remap(frame, cam.map[:, :, 0], cam.map[:, :, 1], cv2.INTER_LINEAR,
                         borderMode=cv2.BORDER_CONSTANT, borderValue=0)
        view = np.zeros((ch, cw) + frame.shape[2:], frame.dtype)
        view[y0:y0 + mh, x0:x0 + mw] = warp
        mask = np.zeros((ch, cw), bool)
        sh, sw = frame.shape[:2]
        mx, my = cam.map[:, :, 0], cam.map[:, :, 1]
        mask[y0:y0 + mh, x0:x0 + mw] = (mx >= 0) & (my >= 0) & (mx <= sw - 1) & (my <= sh - 1)
        views.append(view)
        masks.append(mask)
    return views, masks


def overlap_box(mask0: np.ndarray, mask1: np.ndarray):
    """(y0, y1, x0, x1) bounding box of the pixels both cameras cover."""
    both = mask0 & mask1
    if not both.any():
        return None
    ys = np.flatnonzero(both.any(axis=1))
    xs = np.flatnonzero(both.any(axis=0))
    return int(ys[0]), int(ys[-1]) + 1, int(xs[0]), int(xs[-1]) + 1


def _subpixel(c: np.ndarray, i: int) -> float:
    """Parabolic peak refinement of 1-D correlation profile c around i."""
    if 0 < i < len(c) - 1:
        d = c[i - 1] - 2 * c[i] + c[i + 1]
        if d != 0:
            return i + 0.5 * (c[i - 1] - c[i + 1]) / d
    return float(i)


def residual_stats(g0: np.ndarray, g1: np.ndarray, mask: Optional[np.ndarray] = None,
                   tile: int = 96, stride: int = 48, search: int = 32) -> Optional[dict]:
    """Tiled displacement between two aligned grayscale views (canvas px).

    Each tile of g1 is located in g0 within +-`search` px by normalised
    cross-correlation (gain/offset invariant, so the cameras' differing
    exposure doesn't bias it) with a parabolic sub-pixel peak.
    CROSS-CAMERA PITFALL: decorrelated fine texture (sensor noise) over-reads
    shifts, so both views are blurred sigma 8 first. (The old phase
    correlation of gradient energy under-read small shifts by ~2x — a 2 px
    ghost read 1.0 px; tests/unit/test_stitch_score.py pins this.)
    Tiles whose search window is under 95% inside `mask` (both views valid)
    or that are textureless/low-confidence are skipped. `tiles` rows are
    (x, y, dx, dy, |shift|): tile centre and cam1-vs-cam0 displacement.
    """
    g0 = cv2.GaussianBlur(g0.astype(np.float32), (0, 0), 8)
    g1 = cv2.GaussianBlur(g1.astype(np.float32), (0, 0), 8)
    r = search
    shifts = []
    for y in range(r, g0.shape[0] - tile - r + 1, stride):
        for x in range(r, g0.shape[1] - tile - r + 1, stride):
            if mask is not None and mask[y - r:y + tile + r, x - r:x + tile + r].mean() < 0.95:
                continue
            tpl = g1[y:y + tile, x:x + tile]
            if tpl.std() < 1.0:  # textureless tile
                continue
            res = cv2.matchTemplate(g0[y - r:y + tile + r, x - r:x + tile + r], tpl,
                                    cv2.TM_CCOEFF_NORMED)
            _, peak, _, (px, py) = cv2.minMaxLoc(res)
            if peak < 0.5:  # low confidence
                continue
            dx = _subpixel(res[py], px) - r
            dy = _subpixel(res[:, px], py) - r
            shifts.append((x + tile / 2, y + tile / 2, dx, dy, np.hypot(dx, dy)))
    if not shifts:
        return None
    arr = np.asarray(shifts)
    mags = arr[:, 4]
    return {
        "n_tiles": len(shifts),
        "median": float(np.median(mags)),
        "mean": float(np.mean(mags)),
        "p90": float(np.percentile(mags, 90)),
        "p95": float(np.percentile(mags, 95)),
        "max": float(np.max(mags)),
        "tiles": arr,
    }


def _gray(img: np.ndarray) -> np.ndarray:
    return img if img.ndim == 2 else cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)


def score(f0: np.ndarray, f1: np.ndarray, live: stitch_maps.LiveMaps) -> dict:
    """Ghosting + exposure-step numbers for one frame pair (see module doc)."""
    (v0, v1), (m0, m1) = warp_views(f0, f1, live)
    box = overlap_box(m0, m1)
    if box is None:
        raise ValueError("the two cameras do not overlap on this canvas")
    y0, y1, x0, x1 = box
    g0 = _gray(v0[y0:y1, x0:x1]).astype(np.float32)
    g1 = _gray(v1[y0:y1, x0:x1]).astype(np.float32)
    both = (m0 & m1)[y0:y1, x0:x1]

    out = {"canvas": list(live.canvas), "overlap_box": [y0, y1, x0, x1]}
    res = residual_stats(g0, g1, both)
    if res is None:
        out.update(misalign_tiles=0, misalign_median=None, misalign_p95=None)
    else:
        out.update(misalign_tiles=res["n_tiles"],
                   misalign_median=round(res["median"], 3),
                   misalign_p95=round(res["p95"], 3))

    # exposure step: blur away texture and residual misalignment first
    b0 = cv2.GaussianBlur(g0, (0, 0), 8)[both]
    b1 = cv2.GaussianBlur(g1, (0, 0), 8)[both]
    lit = (b0 > 8) & (b1 > 8)          # ignore black borders / crushed shadows
    if lit.any():
        out["luma_step"] = round(float(np.median(b1[lit] - b0[lit])), 2)
        out["luma_ratio"] = round(float(np.median(b1[lit]) / np.median(b0[lit])), 4)
    else:
        out["luma_step"] = out["luma_ratio"] = None
    return out


def render_seam_crop(f0: np.ndarray, f1: np.ndarray, live: stitch_maps.LiveMaps,
                     path: Path, stitched: Optional[np.ndarray] = None,
                     max_width: int = 1600) -> None:
    """Save the blended overlap (top) above a red=cam0 / green=cam1 edge
    overlay (bottom): ghosting shows as doubled edges in both."""
    (v0, v1), (m0, m1) = warp_views(f0, f1, live)
    box = overlap_box(m0, m1)
    if box is None:
        raise ValueError("the two cameras do not overlap on this canvas")
    y0, y1, x0, x1 = box
    if stitched is None:
        from .stitcher import VideoStitcher
        vs = VideoStitcher(output_width=0)
        vs._use_live_maps(live)
        stitched = vs._stitch_quality(f0, f1)
    blend = stitched[y0:y1, x0:x1]
    if blend.ndim == 2:
        blend = cv2.cvtColor(blend, cv2.COLOR_GRAY2BGR)
    edges = np.zeros_like(blend)
    for ch, view in ((2, v0), (1, v1)):
        g = cv2.GaussianBlur(_gray(view[y0:y1, x0:x1]), (0, 0), 1.5)
        edges[:, :, ch] = cv2.Canny(g, 40, 120)
    img = np.vstack([blend, edges])
    if img.shape[1] > max_width:
        k = max_width / img.shape[1]
        img = cv2.resize(img, (max_width, int(round(img.shape[0] * k))), interpolation=cv2.INTER_AREA)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(path), img)
