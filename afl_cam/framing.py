"""Framing guides for the phone's Setup view: where the seam and the
horizon fall on the stitched canvas, read from the live map set.

Stdlib only (the daemon imports it). Reads params.bin plus ONE row of
each blend-weight file, never the full maps (`stitch_maps.py` has the
format; this is its read-only, numpy-free cousin).
"""

import math
import struct
from pathlib import Path
from typing import Optional

PARAMS_FLOATS = 64
CAM_BLOCK = 28


def read_params(params_dir: Path) -> Optional[list]:
    try:
        data = (Path(params_dir) / "params.bin").read_bytes()
    except OSError:
        return None
    if len(data) != PARAMS_FLOATS * 4:
        return None
    return list(struct.unpack(f"<{PARAMS_FLOATS}f", data))


def horizon_row(params: list) -> Optional[float]:
    """Canvas row of the level horizon (zero pitch), averaged over both
    cameras. The spherical warp maps canvas row r of a camera's ROI to
    v = (r + tl_y) / warp_scale with v = pi/2 at the horizon (see
    stitcher._spherical_maps), placed on the canvas at off_y."""
    scale = params[4]
    if scale <= 0:
        return None
    rows = []
    for cam in (0, 1):
        base = 8 + CAM_BLOCK * cam
        tl_y, off_y = params[base + 10], params[base + 12]
        rows.append(scale * math.pi / 2 - tl_y + off_y)
    return sum(rows) / len(rows)


def _weight_row(path: Path, row: int, width: int) -> Optional[bytes]:
    """Channel 0 of one canvas row of a w{0,1}.bin (uint8 RGBA per px)."""
    try:
        with open(path, "rb") as fh:
            fh.seek(row * width * 4)
            data = fh.read(width * 4)
    except OSError:
        return None
    if len(data) != width * 4:
        return None
    return data[0::4]


def seam_columns(params_dir: Path, width: int, row: int) -> Optional[dict]:
    """Overlap band and seam (where cam1's weight overtakes cam0's) on one
    canvas row, in canvas pixels."""
    w0 = _weight_row(Path(params_dir) / "w0.bin", row, width)
    w1 = _weight_row(Path(params_dir) / "w1.bin", row, width)
    if w0 is None or w1 is None:
        return None
    both = [x for x in range(width) if w0[x] and w1[x]]
    if not both:
        return None
    seam = next((x for x in both if w1[x] >= w0[x]), both[-1])
    return {"overlap": [both[0], both[-1]], "seam": seam}


def framing(params_dir: Path) -> Optional[dict]:
    """Guide positions as fractions of the canvas (0..1), so the page can
    draw them over a preview of any size. None when the set is missing."""
    params = read_params(params_dir)
    if params is None:
        return None
    width, height = int(params[0]), int(params[1])
    if width <= 0 or height <= 0:
        return None
    hz = horizon_row(params)
    row = int(hz) if hz is not None and 0 <= hz < height else height // 2
    cols = seam_columns(params_dir, width, row)
    out = {
        "canvas": [width, height],
        "horizon": round(hz / height, 4)
        if hz is not None and 0 <= hz < height else None,
        "seam": round(cols["seam"] / width, 4) if cols else None,
        "overlap": [round(c / width, 4) for c in cols["overlap"]]
        if cols else None,
    }
    try:
        out["aligned_at"] = (Path(params_dir) / "params.bin").stat().st_mtime
    except OSError:
        out["aligned_at"] = None
    return out
