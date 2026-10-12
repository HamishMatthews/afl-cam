"""The live-stitch map set: one on-disk format, one writer, one reader.

`scripts/dump_camera_params.py` writes it, `cam2stitch` (GPU, live) and
`VideoStitcher` (CPU, separate-mode re-stitch) both read it, so a re-stitch
of per-camera files reproduces the live geometry and blend exactly.

Files in the params dir (all raw little-endian, written atomically):
  params.bin  64 float32: [0..3] canvas_w, canvas_h, src_w, src_h,
              [4] warp_scale, [5..6] per-camera exposure gain (0 = 1.0),
              per-camera 28-float block at 8 + 28*cam:
              krinv(9) tl(2) off(2) mapwh(2) pin fx,fy,cx,cy(4)
              raw fx,fy,cx,cy(4) dist(5)
  map{0,1}.bin  float32 (map_h, map_w, 2): the raw source (x, y) for each
              canvas pixel of the camera's ROI (placed at `off`); -1 =
              no source.
  w{0,1}.bin  uint8 (canvas_h, canvas_w, 4): blend weight 0..255 repeated
              in 4 channels (the GPU samples it as RGBA8).
"""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np

PARAMS_FLOATS = 64
CAM_BLOCK = 28


@dataclass
class CameraMap:
    map: np.ndarray        # float32 (h, w, 2) raw source coords, -1 = none
    off: Tuple[int, int]   # (x, y) of the map's top-left on the canvas
    weight: np.ndarray     # uint8 (canvas_h, canvas_w)


@dataclass
class LiveMaps:
    canvas: Tuple[int, int]   # (w, h)
    src: Tuple[int, int]      # (w, h) raw camera frame
    params: np.ndarray        # the 64 raw floats, kept for re-writing
    cams: List[CameraMap]

    def gain(self, cam: int) -> float:
        """Exposure gain the shader applies to this camera (1.0 if unset)."""
        g = float(self.params[5 + cam])
        return g if g > 0 else 1.0

    def with_gains(self, g0: float, g1: float) -> "LiveMaps":
        params = np.asarray(self.params, np.float32).copy()
        params[5:7] = [g0, g1]
        return LiveMaps(canvas=self.canvas, src=self.src, params=params, cams=self.cams)


def _read(path: Path, dtype, shape) -> np.ndarray:
    data = np.fromfile(path, dtype=dtype)
    if data.size != int(np.prod(shape)):
        raise ValueError(f"{path.name}: {data.size} values, expected {shape}")
    return data.reshape(shape)


def load(params_dir: Path) -> LiveMaps:
    """Read a map set; raises OSError/ValueError on a missing or torn set."""
    params_dir = Path(params_dir)
    params = _read(params_dir / "params.bin", np.float32, (PARAMS_FLOATS,))
    cw, ch, sw, sh = (int(v) for v in params[0:4])
    cams = []
    for i in (0, 1):
        base = 8 + CAM_BLOCK * i
        off = (int(params[base + 11]), int(params[base + 12]))
        mw, mh = int(params[base + 13]), int(params[base + 14])
        m = _read(params_dir / f"map{i}.bin", np.float32, (mh, mw, 2))
        w = _read(params_dir / f"w{i}.bin", np.uint8, (ch, cw, 4))[:, :, 0]
        if off[0] < 0 or off[1] < 0 or off[0] + mw > cw or off[1] + mh > ch:
            raise ValueError(f"map{i} ROI {off}+{mw}x{mh} outside canvas {cw}x{ch}")
        cams.append(CameraMap(map=m, off=off, weight=np.ascontiguousarray(w)))
    return LiveMaps(canvas=(cw, ch), src=(sw, sh), params=params, cams=cams)


def try_load(params_dir: Path) -> Optional[LiveMaps]:
    """Map set or None when it is missing or torn (a crashed re-align
    leaves 0-byte files — 2026-09-01)."""
    try:
        return load(params_dir)
    except (OSError, ValueError):
        return None


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)


def write(params_dir: Path, maps: LiveMaps) -> None:
    """Write a map set. Every file goes tmp + rename: a kill mid-write must
    never leave a truncated set (it silently breaks the next recording)."""
    params_dir = Path(params_dir)
    params_dir.mkdir(parents=True, exist_ok=True)
    params = np.asarray(maps.params, np.float32).copy()
    params[0:4] = [maps.canvas[0], maps.canvas[1], maps.src[0], maps.src[1]]
    for i, cam in enumerate(maps.cams):
        base = 8 + CAM_BLOCK * i
        params[base + 11:base + 13] = cam.off
        params[base + 13:base + 15] = [cam.map.shape[1], cam.map.shape[0]]
        _atomic_write(params_dir / f"w{i}.bin",
                      np.repeat(cam.weight[:, :, None].astype(np.uint8), 4, axis=2).tobytes())
        _atomic_write(params_dir / f"map{i}.bin", cam.map.astype(np.float32).tobytes())
    _atomic_write(params_dir / "params.bin", params.tobytes())
