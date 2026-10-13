"""Crop the live map set to its largest fully covered rectangle.

The canvas is a spherical projection, so each camera's frame lands on it as
a curved band and the corners are black. Cropping to the inscribed rectangle
trims the sky and the grass in front of the rig, leaves no black margin and
gives the encoder fewer pixels. The crop is a derived map set in the same
format (`stitch_maps.py`): cam2stitch, the shader and the encoder just see a
smaller canvas, so nothing native changes and the full set stays untouched.

    python -m afl_cam.crop_maps <live_dir> <crop_dir>

The crop set records a signature of the live files it came from and is
rebuilt only when they change (a re-align or a seam refine rewrites them).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np

from . import cropsig, stitch_maps
from .cropsig import SIG_FILE

COARSE = 8          # the rectangle search runs on 8x8 blocks, then grows at full resolution
EDGE_PX = 2         # keep this far inside the source frame (bilinear taps at the border)

Rect = Tuple[int, int, int, int]      # x, y, w, h


def coverage(live: stitch_maps.LiveMaps) -> np.ndarray:
    """bool (canvas_h, canvas_w): some camera has a source pixel here, at
    least EDGE_PX inside its frame."""
    cw, ch = live.canvas
    sw, sh = live.src
    cov = np.zeros((ch, cw), bool)
    for cam in live.cams:
        x, y = cam.map[:, :, 0], cam.map[:, :, 1]
        ok = ((x >= EDGE_PX) & (x <= sw - 1 - EDGE_PX)
              & (y >= EDGE_PX) & (y <= sh - 1 - EDGE_PX))
        ox, oy = cam.off
        cov[oy:oy + ok.shape[0], ox:ox + ok.shape[1]] |= ok
    return cov


def largest_rectangle(mask: np.ndarray) -> Rect:
    """The largest axis-aligned all-True rectangle (x, y, w, h); w == 0 when none."""
    h, w = mask.shape
    heights = np.zeros(w, np.int64)
    best, best_area = (0, 0, 0, 0), 0
    for y in range(h):
        heights = np.where(mask[y], heights + 1, 0)
        stack: List[Tuple[int, int]] = []       # (start column, height)
        for x in range(w + 1):
            cur = int(heights[x]) if x < w else 0
            start = x
            while stack and stack[-1][1] >= cur:
                s, hh = stack.pop()
                if hh * (x - s) > best_area:
                    best_area = hh * (x - s)
                    best = (s, y - hh + 1, x - s, hh)
                start = s
            stack.append((start, cur))
    return best


def crop_rect(cov: np.ndarray) -> Rect:
    """Largest fully covered rectangle with width % 4 == 0 and even height
    (the SSBO packing and NV12 chroma blocks need them), exact at full
    resolution: found on conservative 8x8 blocks, then grown a pixel at a time."""
    ch, cw = cov.shape
    bh, bw = ch // COARSE, cw // COARSE
    blocks = cov[:bh * COARSE, :bw * COARSE].reshape(bh, COARSE, bw, COARSE).all(axis=(1, 3))
    x, y, w, h = largest_rectangle(blocks)
    if w == 0:
        raise ValueError("no fully covered rectangle on the canvas")
    x, y, w, h = x * COARSE, y * COARSE, w * COARSE, h * COARSE
    grown = True
    while grown:
        grown = False
        if y > 0 and cov[y - 1, x:x + w].all():
            y, h, grown = y - 1, h + 1, True
        if y + h < ch and cov[y + h, x:x + w].all():
            h, grown = h + 1, True
        if x > 0 and cov[y:y + h, x - 1].all():
            x, w, grown = x - 1, w + 1, True
        if x + w < cw and cov[y:y + h, x + w].all():
            w, grown = w + 1, True
    return x, y, w // 4 * 4, h // 2 * 2


def crop(live: stitch_maps.LiveMaps, rect: Rect) -> stitch_maps.LiveMaps:
    """The map set restricted to `rect`: canvas = the rectangle, each
    camera's map cut to the part inside it and re-placed, weights sliced.
    `tl` moves by the rows/columns cut off a camera's top-left so the
    horizon guide (framing.horizon_row) still lands on the same ground."""
    x, y, w, h = rect
    params = np.asarray(live.params, np.float32).copy()
    cams = []
    for i, cam in enumerate(live.cams):
        ox, oy = cam.off
        mh, mw = cam.map.shape[:2]
        x0, y0 = max(ox, x), max(oy, y)
        x1, y1 = min(ox + mw, x + w), min(oy + mh, y + h)
        if x1 <= x0 or y1 <= y0:
            raise ValueError(f"camera {i} has no pixels inside the crop {rect}")
        base = 8 + stitch_maps.CAM_BLOCK * i
        params[base + 9] += x0 - ox
        params[base + 10] += y0 - oy
        cams.append(stitch_maps.CameraMap(
            map=np.ascontiguousarray(cam.map[y0 - oy:y1 - oy, x0 - ox:x1 - ox]),
            off=(x0 - x, y0 - y),
            weight=np.ascontiguousarray(cam.weight[y:y + h, x:x + w])))
    return stitch_maps.LiveMaps(canvas=(w, h), src=live.src, params=params, cams=cams)


def make_crop_set(live_dir: Path, crop_dir: Path) -> dict:
    """Write (or reuse) the crop set for `live_dir`. Returns {rect, canvas,
    reused}. Raises OSError/ValueError when the live set is missing or torn."""
    live_dir, crop_dir = Path(live_dir), Path(crop_dir)
    sig = cropsig.signature(live_dir)
    if sig is None:
        raise OSError(f"no complete map set in {live_dir}")
    done = cropsig.current(live_dir, crop_dir)
    if done is not None and stitch_maps.try_load(crop_dir) is not None:
        return {"rect": done["rect"], "canvas": done["canvas"], "reused": True}
    live = stitch_maps.load(live_dir)
    rect = crop_rect(coverage(live))
    cropped = crop(live, rect)
    try:
        (crop_dir / SIG_FILE).unlink()      # a torn set must not look current
    except OSError:
        pass
    stitch_maps.write(crop_dir, cropped)
    info = {"rect": list(rect), "canvas": list(cropped.canvas)}
    tmp = crop_dir / (SIG_FILE + ".tmp")
    tmp.write_text(json.dumps({"sig": sig, **info}))
    tmp.replace(crop_dir / SIG_FILE)
    return {**info, "reused": False}


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("live_dir", type=Path)
    ap.add_argument("crop_dir", type=Path)
    args = ap.parse_args(argv)
    try:
        info = make_crop_set(args.live_dir, args.crop_dir)
    except (OSError, ValueError) as e:
        print(f"crop failed: {e}", file=sys.stderr)
        return 1
    print(json.dumps(info))
    return 0


if __name__ == "__main__":
    sys.exit(main())
