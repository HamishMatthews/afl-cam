"""Compare ways to turn the two-camera footprint into a rectangular video.

The live canvas is an equirectangular projection about the bundle's world
axes (`stitcher._spherical_maps`). Each camera's rectangular frame lands on it
as a curved band, so the canvas has black wedges at the corners. This module
measures the options for getting rid of them on a rig's own geometry (K, lens
and R from `cams.txt`), so the choice is made from numbers:

  current     equirectangular in the world frame (what runs today)
  crop        largest axis-aligned rectangle that is fully covered
  stretch     scale every column so its covered span fills the bbox height
  cylinder    cylinder about the rig's own vertical axis, then crop
  panini      Panini projection (d between 0 = planar and large = cylinder)
              about the rig axis, then crop

Pure numpy; PNG output needs cv2 (optional). Run:

    python -m afl_cam.framing_options                       # calibration/stitch_live_full/cams.txt
    python -m afl_cam.framing_options --cams path/cams.txt --out framing.png
    python -m afl_cam.framing_options --synthetic           # demo rig, no calibration needed
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np

from afl_cam import lens

GRID_DIV = 4.0      # coarse canvas: pixels per radian = focal / GRID_DIV


@dataclass
class Cam:
    K: np.ndarray       # 3x3, raw frame pixels
    R: np.ndarray       # camera -> world, as in cams.txt (world ray = R K^-1 p)
    dist: np.ndarray    # (k1, k2, p1, p2, k3)
    size: Tuple[int, int]

    @property
    def axis(self) -> np.ndarray:
        return self.R[:, 2]


# ------------------------------------------------------------- the rig -----

def load_cams(path: Path) -> List[Cam]:
    """K, R and lens per camera from a `cams.txt` written by dump_camera_params.py."""
    rows: Dict[str, List[List[float]]] = {}
    dist: Dict[int, List[float]] = {}
    for line in Path(path).read_text().splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[0] in ("K0", "K1", "R0", "R1"):
            rows.setdefault(parts[0], []).append([float(v) for v in parts[1:]])
        elif len(parts) == 7 and parts[1] == "dist" and parts[0] in ("cam0", "cam1"):
            dist[int(parts[0][-1])] = [float(v) for v in parts[2:]]
    cams = []
    for i in (0, 1):
        if len(rows.get(f"K{i}", [])) != 3 or len(rows.get(f"R{i}", [])) != 3:
            raise ValueError(f"{path}: no K{i}/R{i} rows")
        cal = lens.load(f"cam{i}")
        cams.append(Cam(K=np.array(rows[f"K{i}"]), R=np.array(rows[f"R{i}"]),
                        dist=np.array(dist.get(i, cal["dist"])), size=tuple(cal["size"])))
    return cams


def synthetic_rig(yaw_deg: float = 35.0, pitch_deg: float = 15.0,
                  size: Tuple[int, int] = (3840, 2160)) -> List[Cam]:
    """Two IMX415-like cameras toed out by +-yaw and pitched down, with R as
    the bundle gives it (cam0 = identity, so the world frame is cam0's)."""
    k = np.array([[2239.0, 0, size[0] / 2], [0, 2238.8, size[1] / 2], [0, 0, 1]])
    yaw, pitch = math.radians(yaw_deg), math.radians(pitch_deg)

    def ry(a):
        return np.array([[math.cos(a), 0, math.sin(a)], [0, 1, 0], [-math.sin(a), 0, math.cos(a)]])

    def rx(a):
        return np.array([[1, 0, 0], [0, math.cos(a), -math.sin(a)], [0, math.sin(a), math.cos(a)]])

    # the rig is two cameras yawed about its own vertical axis, then the whole
    # rig is pitched down about the world x axis (optical axis y > 0 = down)
    sign = 1.0 if (rx(pitch) @ ry(yaw))[1, 2] > 0 else -1.0
    r0 = rx(sign * pitch) @ ry(-yaw)
    r1 = rx(sign * pitch) @ ry(yaw)
    d = np.zeros(5)
    cams = [Cam(k.copy(), r0, d, size), Cam(k.copy(), r1, d, size)]
    base = cams[0].R.T         # re-express in cam0's frame, like a bundle does
    for c in cams:
        c.R = base @ c.R
    return cams


def rig_frame(cams: List[Cam]) -> np.ndarray:
    """Rows = the rig's (x right, y down, z forward) axes in world coordinates."""
    o0, o1 = cams[0].axis, cams[1].axis
    y = np.cross(o0, o1)
    y /= np.linalg.norm(y)
    down = cams[0].R[:, 1] + cams[1].R[:, 1]       # image-down in world
    if y @ down < 0:
        y = -y
    z = o0 + o1
    z = z - (z @ y) * y
    z /= np.linalg.norm(z)
    x = np.cross(y, z)
    return np.stack([x, y, z])


# ------------------------------------------------------------ projections --
# Each maps ray angles (phi azimuth, theta elevation, both radians in a frame
# whose rows are `basis`) to unit-focal plane coords (x', y'), and back.

@dataclass
class Projection:
    name: str
    basis: np.ndarray
    fwd: Callable[[np.ndarray, np.ndarray], Tuple[np.ndarray, np.ndarray]]
    inv: Callable[[np.ndarray, np.ndarray], Tuple[np.ndarray, np.ndarray]]

    def forward(self, rays: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
        r = rays @ self.basis.T
        phi = np.arctan2(r[:, 0], r[:, 2])
        theta = np.arctan2(r[:, 1], np.hypot(r[:, 0], r[:, 2]))
        return self.fwd(phi, theta)

    def rays(self, x: np.ndarray, y: np.ndarray) -> np.ndarray:
        phi, theta = self.inv(x, y)
        r = np.stack([np.cos(theta) * np.sin(phi), np.sin(theta),
                      np.cos(theta) * np.cos(phi)], axis=1)
        return r @ self.basis          # rig/world frame -> world


def equirect(basis: np.ndarray) -> Projection:
    return Projection("equirect", basis, lambda p, t: (p, t), lambda x, y: (x, y))


def cylinder(basis: np.ndarray) -> Projection:
    return Projection("cylinder", basis, lambda p, t: (p, np.tan(t)),
                      lambda x, y: (x, np.arctan(y)))


def panini(basis: np.ndarray, d: float) -> Projection:
    def fwd(p, t):
        s = (d + 1.0) / (d + np.cos(p))
        return s * np.sin(p), s * np.tan(t)

    def inv(x, y):
        a = x / (d + 1.0)
        p = np.arctan(a) + np.arcsin(np.clip(a * d / np.sqrt(1.0 + a * a), -1, 1))
        s = (d + 1.0) / (d + np.cos(p))
        return p, np.arctan(y / s)

    return Projection(f"panini d={d:g}", basis, fwd, inv)


# --------------------------------------------------------------- coverage --

def _frame_rays(cam: Cam, nx: int = 161, ny: int = 91) -> Tuple[np.ndarray, np.ndarray]:
    """World rays through a grid of raw frame pixels (edges included)."""
    w, h = cam.size
    gx, gy = np.meshgrid(np.linspace(0, w - 1, nx), np.linspace(0, h - 1, ny))
    x, y = lens.undistort_pixels(gx.ravel(), gy.ravel(), cam.K, cam.dist)
    p = np.stack([x, y, np.ones_like(x)])
    rays = (cam.R @ np.linalg.inv(cam.K) @ p).T
    return rays / np.linalg.norm(rays, axis=1, keepdims=True), (gx.shape, np.array([w, h]))


def _covered(cam: Cam, rays: np.ndarray) -> np.ndarray:
    """Which world rays land inside this camera's raw frame."""
    c = rays @ cam.R             # = R^T ray, per row
    z = c[:, 2]
    ok = z > 0.05
    zs = np.where(ok, z, 1.0)
    px = cam.K[0, 0] * c[:, 0] / zs + cam.K[0, 2]
    py = cam.K[1, 1] * c[:, 1] / zs + cam.K[1, 2]
    ok &= (np.abs(c[:, 0] / zs) < 3) & (np.abs(c[:, 1] / zs) < 3)
    px, py = lens.distort_pixels(np.where(ok, px, 0.0), np.where(ok, py, 0.0), cam.K, cam.dist)
    return ok & (px >= 0) & (px <= cam.size[0] - 1) & (py >= 0) & (py <= cam.size[1] - 1)


@dataclass
class Footprint:
    proj: Projection
    f: float                     # canvas px per unit of x', y'
    x0: float                    # canvas origin in x', y' units
    y0: float
    masks: List[np.ndarray]      # per camera bool (h, w)
    cell_scale: List[np.ndarray] # per camera: linear scale of each source grid cell (canvas px / src px)
    cell_col: List[np.ndarray]   # per camera: canvas column of each source grid cell

    @property
    def union(self) -> np.ndarray:
        return self.masks[0] | self.masks[1]


def footprint(cams: List[Cam], proj: Projection, f: float) -> Footprint:
    """Rasterise both cameras' coverage on the projection's canvas (f px per unit)."""
    pts_x, pts_y, shapes = [], [], []
    for cam in cams:
        rays, (shape, wh) = _frame_rays(cam)
        x, y = proj.forward(rays)
        pts_x.append(x)
        pts_y.append(y)
        shapes.append((shape, wh))
    x0 = min(a.min() for a in pts_x)
    x1 = max(a.max() for a in pts_x)
    y0 = min(a.min() for a in pts_y)
    y1 = max(a.max() for a in pts_y)
    w = int(math.ceil((x1 - x0) * f)) + 1
    h = int(math.ceil((y1 - y0) * f)) + 1
    gx, gy = np.meshgrid((np.arange(w) + 0.5) / f + x0, (np.arange(h) + 0.5) / f + y0)
    rays = proj.rays(gx.ravel(), gy.ravel())
    masks = [_covered(c, rays).reshape(h, w) for c in cams]

    scales, cols = [], []
    for i, cam in enumerate(cams):
        shape, wh = shapes[i]
        ny, nx = shape
        px = ((pts_x[i] - x0) * f).reshape(shape)
        py = ((pts_y[i] - y0) * f).reshape(shape)
        # canvas area of each grid cell (two triangles) over its source area
        ax, ay = px[:-1, :-1], py[:-1, :-1]
        bx, by = px[:-1, 1:], py[:-1, 1:]
        cx, cy = px[1:, 1:], py[1:, 1:]
        dx, dy = px[1:, :-1], py[1:, :-1]
        area = 0.5 * np.abs((bx - ax) * (cy - ay) - (cx - ax) * (by - ay)) \
            + 0.5 * np.abs((cx - ax) * (dy - ay) - (dx - ax) * (cy - ay))
        src = (wh[0] / (nx - 1)) * (wh[1] / (ny - 1))
        # source px per canvas px at unit focal: normalise by the camera's own focal
        scales.append(np.sqrt(area / src) * (cam.K[0, 0] / f))
        cols.append(np.clip(((ax + cx) / 2).astype(int), 0, w - 1))
    return Footprint(proj, f, x0, y0, masks, scales, cols)


def largest_rectangle(mask: np.ndarray) -> Tuple[int, int, int, int]:
    """(x, y, w, h) of the largest axis-aligned all-True rectangle."""
    h, w = mask.shape
    heights = np.zeros(w, dtype=np.int64)
    best = (0, 0, 0, 0)
    best_area = 0
    for y in range(h):
        heights = np.where(mask[y], heights + 1, 0)
        stack: List[Tuple[int, int]] = []       # (start column, height)
        for x in range(w + 1):
            cur = int(heights[x]) if x < w else 0
            start = x
            while stack and stack[-1][1] >= cur:
                s, hh = stack.pop()
                area = hh * (x - s)
                if area > best_area:
                    best_area = area
                    best = (s, y - hh + 1, x - s, hh)
                start = s
            stack.append((start, cur))
    return best


# ---------------------------------------------------------------- options --

@dataclass
class Row:
    option: str
    size: Tuple[int, int]        # output px at the full focal (1.0 = source scale)
    black_pct: float
    kept_pct: float              # % of the covered ground that stays in the video
    scale_min: float             # local linear scale (output px per source px)
    scale_max: float
    note: str = ""

    def md(self) -> str:
        return (f"| {self.option} | {self.size[0]}x{self.size[1]} | {self.black_pct:.1f} | "
                f"{self.kept_pct:.1f} | {self.scale_min:.2f} to {self.scale_max:.2f} | {self.note} |")


def _scale_range(fp: Footprint, window: Optional[Tuple[int, int, int, int]] = None,
                 col_scale: Optional[np.ndarray] = None) -> Tuple[float, float]:
    vals = []
    for s, c in zip(fp.cell_scale, fp.cell_col):
        s = s.copy()
        if col_scale is not None:
            s = s * col_scale[c]
        if window is not None:
            x, _, w, _ = window
            s = s[(c >= x) & (c < x + w)]
        vals.append(s.ravel())
    v = np.concatenate(vals)
    return float(np.percentile(v, 1)), float(np.percentile(v, 99))


def evaluate(cams: List[Cam], progress: Callable[[str], None] = lambda m: None
             ) -> Tuple[List[Row], Dict[str, Tuple[Footprint, Optional[tuple]]]]:
    """All options on this rig. Returns table rows and, per panel name, the
    footprint and the crop rectangle (or None) for drawing."""
    f_full = float(np.median([c.K[0, 0] for c in cams]))
    f = f_full / GRID_DIV
    up = f_full / f
    world = np.eye(3)
    rig = rig_frame(cams)
    rows: List[Row] = []
    panels: Dict[str, Tuple[Footprint, Optional[tuple]]] = {}

    def add(name: str, fp: Footprint, window: Optional[tuple], stretch: Optional[np.ndarray] = None,
            note: str = ""):
        union = fp.union
        covered = int(union.sum())
        h, w = union.shape
        if window is None:
            size = (w, h)
            black = 100.0 * (1.0 - covered / (w * h))
            kept = 100.0
        else:
            x, y, ww, hh = window
            size = (ww, hh)
            black = 0.0
            kept = 100.0 * int(union[y:y + hh, x:x + ww].sum()) / covered
        smin, smax = _scale_range(fp, window, stretch)
        rows.append(Row(name, (int(size[0] * up) // 4 * 4, int(size[1] * up) // 2 * 2),
                        black, kept, smin, smax, note))

    progress("equirect (current)")
    cur = footprint(cams, equirect(world), f)
    add("current (equirect, black margins)", cur, None)
    rect = largest_rectangle(cur.union)
    add("crop the current canvas", cur, rect)
    panels["current"] = (cur, None)
    panels["crop"] = (cur, rect)

    # stretch: each column's covered span scaled to the bbox height
    union = cur.union
    span = union.sum(axis=0).astype(np.float64)
    ok = span > 0
    col_scale = np.where(ok, union.shape[0] / np.maximum(span, 1), 1.0)
    add("stretch columns to full height", cur, None, stretch=col_scale,
        note="straight lines bend wherever the column scale changes")
    panels["stretch"] = (cur, None)

    progress("cylinder about the rig axis")
    cyl = footprint(cams, cylinder(rig), f)
    add("cylinder in rig frame (black margins)", cyl, None)
    rect = largest_rectangle(cyl.union)
    add("cylinder, cropped", cyl, rect)
    panels["cylinder"] = (cyl, rect)

    for d in (0.5, 1.0):
        progress(f"panini d={d:g}")
        pan = footprint(cams, panini(rig, d), f)
        add(f"panini d={d:g} (black margins)", pan, None)
        rect = largest_rectangle(pan.union)
        add(f"panini d={d:g}, cropped", pan, rect)
        panels[f"panini d={d:g}"] = (pan, rect)
    return rows, panels


def table(rows: List[Row]) -> str:
    head = ("| option | output px (source scale) | black % | ground kept % | local scale (1%..99%) | notes |\n"
            "|---|---|---|---|---|---|")
    return head + "\n" + "\n".join(r.md() for r in rows)


# ------------------------------------------------------------------ image --

def montage(panels: Dict[str, Tuple[Footprint, Optional[tuple]]], out: Path, width: int = 900) -> bool:
    try:
        import cv2
    except ImportError:
        return False
    tiles = []
    for name, (fp, rect) in panels.items():
        h, w = fp.union.shape
        img = np.zeros((h, w, 3), np.uint8)
        m0, m1 = fp.masks
        img[m0 & ~m1] = (200, 120, 40)       # BGR: cam0 blue
        img[m1 & ~m0] = (40, 140, 230)       # cam1 orange
        img[m0 & m1] = (170, 90, 150)        # overlap purple
        if rect is not None:
            x, y, rw, rh = rect
            cv2.rectangle(img, (x, y), (x + rw - 1, y + rh - 1), (80, 220, 80), 2)
        s = width / w
        img = cv2.resize(img, (width, max(1, int(h * s))), interpolation=cv2.INTER_NEAREST)
        cv2.putText(img, name, (10, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
        tiles.append(img)
    hmax = max(t.shape[0] for t in tiles)
    pad = [cv2.copyMakeBorder(t, 0, hmax - t.shape[0], 0, 6, cv2.BORDER_CONSTANT) for t in tiles]
    cols = 2
    while len(pad) % cols:
        pad.append(np.zeros_like(pad[0]))
    grid = np.vstack([np.hstack(pad[i:i + cols]) for i in range(0, len(pad), cols)])
    return bool(cv2.imwrite(str(out), grid))


def main(argv: Optional[List[str]] = None) -> int:
    from afl_cam import config
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--cams", type=Path, default=config.STITCH_PARAMS_DIR / "cams.txt",
                    help="cams.txt from dump_camera_params.py (default: the live set)")
    ap.add_argument("--synthetic", action="store_true",
                    help="demo rig (two IMX415-like cameras, toe-out and pitch from --yaw/--pitch)")
    ap.add_argument("--yaw", type=float, default=35.0)
    ap.add_argument("--pitch", type=float, default=15.0)
    ap.add_argument("--out", type=Path, default=Path("framing_options.png"))
    args = ap.parse_args(argv)
    if args.synthetic:
        cams = synthetic_rig(args.yaw, args.pitch)
        print(f"synthetic rig: toe-out {args.yaw:g} deg each, pitch {args.pitch:g} deg down")
    else:
        try:
            cams = load_cams(args.cams)
        except (OSError, ValueError) as e:
            print(f"cannot read {args.cams}: {e}\n(use --cams, or --synthetic for a demo)", file=sys.stderr)
            return 2
        print(f"rig from {args.cams}")
    rows, panels = evaluate(cams, progress=lambda m: print("  ..", m, file=sys.stderr))
    print(table(rows))
    print("\ncrop rectangles are in output px at source scale; black % is of the bounding box")
    if montage(panels, args.out):
        print(f"wrote {args.out}")
    else:
        print("cv2 not available: no image written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
