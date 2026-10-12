"""Seam refinement: bake the measured overlap misalignment into the maps.

A rotation-only spherical model aligns exactly one depth; the rest of the
overlap ghosts (parallax: the near ground at the bottom of the frame sits
much closer than the far side of the oval at the top). This measures the
residual displacement field between the two warped views (the stitch-score
ruler), fits a smooth model of it over the overlap, and warps each camera's
map by half of it in opposite directions so the views meet. The correction
fades out away from the overlap so the rest of each view is untouched.

It also matches the two cameras' brightness over the overlap with a
per-camera gain (params.bin [5..6], applied by the shader). Only the map
files change: live and offline stitching both pick it up.
"""

from typing import Optional, Tuple

import cv2
import numpy as np

from . import stitch_maps, stitch_score


def _design(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    # smooth in y (depth falls off down the frame), linear in x
    return np.stack([np.ones_like(y), y, y * y, y ** 3, x, x * y], axis=-1)


def fit_field(tiles: np.ndarray, box: Tuple[int, int, int, int]):
    """Robust least-squares fit of (dx, dy) over the overlap. Returns
    f(xs, ys) -> (dx, dy) on canvas coords, clamped to the fitted y range."""
    y0, y1, x0, x1 = box
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    sx, sy = max(1.0, (x1 - x0) / 2), max(1.0, (y1 - y0) / 2)
    tx = tiles[:, 0] + x0
    ty = tiles[:, 1] + y0
    ylo, yhi = ty.min(), ty.max()

    def norm(xs, ys):
        return (xs - cx) / sx, (np.clip(ys, ylo, yhi) - cy) / sy

    a = _design(*norm(tx, ty))
    coefs = []
    for col in (2, 3):
        b = tiles[:, col]
        keep = np.ones(len(b), bool)
        for _ in range(4):   # reject tiles that matched the wrong thing
            c, *_ = np.linalg.lstsq(a[keep], b[keep], rcond=None)
            r = np.abs(a @ c - b)
            mad = np.median(r[keep]) + 1e-6
            keep = r < max(3 * 1.4826 * mad, 0.5)
        coefs.append(c)

    def field(xs, ys):
        d = _design(*norm(xs, ys))
        return d @ coefs[0], d @ coefs[1]
    return field


def _taper(xs: np.ndarray, box, side: str, fade: float) -> np.ndarray:
    """1 inside the overlap, fading linearly to 0 over `fade` px into the
    camera's own (non-overlap) side."""
    _, _, x0, x1 = box
    if side == "left":
        return np.clip(1 - (x0 - xs) / fade, 0, 1)
    return np.clip(1 - (xs - x1) / fade, 0, 1)


def apply_field(live: stitch_maps.LiveMaps, field, box,
                fade: Optional[float] = None) -> stitch_maps.LiveMaps:
    """Warp each camera's map by half the field, opposite ways. The score
    convention is view1(p) = view0(p + d), so view0 samples at p + d/2 and
    view1 at p - d/2: both then show what sat at p + d/2."""
    fade = fade or float(box[3] - box[2])
    left = 0 if live.cams[0].off[0] <= live.cams[1].off[0] else 1
    cams = []
    for i, cam in enumerate(live.cams):
        mh, mw = cam.map.shape[:2]
        ox, oy = cam.off
        qx, qy = np.meshgrid(np.arange(mw, dtype=np.float32), np.arange(mh, dtype=np.float32))
        px, py = qx + ox, qy + oy
        dx, dy = field(px.ravel(), py.ravel())
        t = _taper(px.ravel(), box, "left" if i == left else "right", fade)
        s = 0.5 if i == 0 else -0.5
        sx = (qx.ravel() + s * t * dx).reshape(mh, mw).astype(np.float32)
        sy = (qy.ravel() + s * t * dy).reshape(mh, mw).astype(np.float32)
        nx = cv2.remap(cam.map[:, :, 0], sx, sy, cv2.INTER_LINEAR,
                       borderMode=cv2.BORDER_CONSTANT, borderValue=-1)
        ny = cv2.remap(cam.map[:, :, 1], sx, sy, cv2.INTER_LINEAR,
                       borderMode=cv2.BORDER_CONSTANT, borderValue=-1)
        valid = (cam.map[:, :, 0] >= 0).astype(np.float32)
        ok = cv2.remap(valid, sx, sy, cv2.INTER_LINEAR,
                       borderMode=cv2.BORDER_CONSTANT, borderValue=0) > 0.999
        nx[~ok] = -1
        ny[~ok] = -1
        far = (t == 0).reshape(mh, mw)      # outside the fade: bit-exact original
        nx[far] = cam.map[:, :, 0][far]
        ny[far] = cam.map[:, :, 1][far]
        cams.append(stitch_maps.CameraMap(np.stack([nx, ny], -1), cam.off, cam.weight))
    return stitch_maps.LiveMaps(canvas=live.canvas, src=live.src, params=live.params, cams=cams)


def refine(f0: np.ndarray, f1: np.ndarray, live: stitch_maps.LiveMaps, rounds: int = 2):
    """Refined map set + the score before and after. Two rounds: the second
    mops up what the first one's smooth model under-fit."""
    before = stitch_score.score(f0, f1, live)
    cur = live
    for _ in range(rounds):
        (v0, v1), (m0, m1) = stitch_score.warp_views(f0, f1, cur)
        box = stitch_score.overlap_box(m0, m1)
        y0, y1, x0, x1 = box
        res = stitch_score.residual_stats(
            stitch_score._gray(v0[y0:y1, x0:x1]), stitch_score._gray(v1[y0:y1, x0:x1]),
            (m0 & m1)[y0:y1, x0:x1])
        if res is None or res["n_tiles"] < 8:
            break
        cur = apply_field(cur, fit_field(res["tiles"], box), box)
    cur = match_exposure(f0, f1, cur)
    after = stitch_score.score(f0, f1, cur)
    return cur, before, after


def match_exposure(f0: np.ndarray, f1: np.ndarray, live: stitch_maps.LiveMaps,
                   max_gain: float = 1.5) -> stitch_maps.LiveMaps:
    """Set the per-camera gains so both cameras are equally bright over the
    overlap, meeting in the middle (cam0 x sqrt(r), cam1 / sqrt(r)) so
    neither view is pushed far into clipping."""
    plain = live.with_gains(1.0, 1.0)
    r = stitch_score.score(f0, f1, plain)["luma_ratio"]   # cam1 / cam0
    if not r:
        return live
    g = float(np.clip(np.sqrt(r), 1 / max_gain, max_gain))
    return live.with_gains(g, 1 / g)
