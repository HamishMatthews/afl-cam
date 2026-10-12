"""Lens model: the tracked per-camera intrinsics and the distortion maths.

`intrinsics/cam{0,1}.json` hold K and OpenCV's (k1, k2, p1, p2, k3), fitted
from checkerboard stills by `python -m afl_cam.lens fit <captures dir>`.
The warp is built in the undistorted frame (a pinhole with the same K) and
the lens is applied last, so the live maps keep holding RAW source
coordinates and the shader never changes.

    python -m afl_cam.lens fit calibration            # both cameras, writes intrinsics/
    python -m afl_cam.lens fit calibration --cam cam0 --out /tmp/try
"""

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional, Tuple

import cv2
import numpy as np

INTRINSICS_DIR = Path(__file__).resolve().parent / "intrinsics"
PATTERN = (10, 7)           # inner corners of the 11x8-square board
EDGE_STRIP = 0.25           # outer fraction of the frame where the overlap sits
MIN_EDGE_IMAGES = 8         # boards that must reach into each edge strip
MIN_TILTED = 4              # boards tilted >= TILT_DEG off the image plane
TILT_DEG = 25


def load(cam: str) -> dict:
    """{'mtx': 3x3 K, 'dist': (k1, k2, p1, p2, k3), 'size': (w, h)} for 'cam0'/'cam1'."""
    d = json.loads((INTRINSICS_DIR / f"{cam}.json").read_text())
    return {"mtx": np.array(d["K"], np.float64), "dist": np.array(d["dist"], np.float64),
            "size": tuple(d["image_size"])}


def _distort(x, y, dist):
    k1, k2, p1, p2, k3 = dist
    r2 = x * x + y * y
    radial = 1 + r2 * (k1 + r2 * (k2 + r2 * k3))
    return (x * radial + 2 * p1 * x * y + p2 * (r2 + 2 * x * x),
            y * radial + p1 * (r2 + 2 * y * y) + 2 * p2 * x * y)


def _undistort(xd, yd, dist, iters: int = 60, tol: float = 1e-12):
    """Invert _distort by fixed-point iteration (the distortion is a few %)."""
    k1, k2, p1, p2, k3 = dist
    x, y = xd, yd
    for _ in range(iters):
        r2 = x * x + y * y
        radial = 1 + r2 * (k1 + r2 * (k2 + r2 * k3))
        nx = (xd - 2 * p1 * x * y - p2 * (r2 + 2 * x * x)) / radial
        ny = (yd - p1 * (r2 + 2 * y * y) - 2 * p2 * x * y) / radial
        done = max(np.max(np.abs(nx - x)), np.max(np.abs(ny - y))) < tol
        x, y = nx, ny
        if done:
            return x, y
    raise ValueError("undistort did not converge: points are too far outside the frame")


def _has_distortion(dist) -> bool:
    return dist is not None and bool(np.any(dist))


def distort_pixels(x, y, K, dist):
    """Pixels of the undistorted (pinhole, same K) frame -> raw pixels."""
    if not _has_distortion(dist):
        return x, y
    fx, fy, cx, cy = K[0, 0], K[1, 1], K[0, 2], K[1, 2]
    xd, yd = _distort((x - cx) / fx, (y - cy) / fy, dist)
    return fx * xd + cx, fy * yd + cy


def undistort_pixels(x, y, K, dist):
    """Raw pixels -> pixels of the undistorted (pinhole, same K) frame."""
    if not _has_distortion(dist):
        return x, y
    fx, fy, cx, cy = K[0, 0], K[1, 1], K[0, 2], K[1, 2]
    xn, yn = _undistort((x - cx) / fx, (y - cy) / fy, dist)
    return fx * xn + cx, fy * yn + cy


def unproject(pts: np.ndarray, K: np.ndarray, dist) -> np.ndarray:
    """Raw pixels (Nx2) -> camera-frame rays (Nx3, z = 1)."""
    x, y = undistort_pixels(pts[:, 0], pts[:, 1], K, dist)
    return np.stack([(x - K[0, 2]) / K[0, 0], (y - K[1, 2]) / K[1, 1], np.ones(len(pts))], axis=1)


def check_monotonic(dist, rmax: float = 3.0) -> None:
    """The radial map r*(1 + k1 r^2 + k2 r^4 + k3 r^6) must keep growing out to
    rmax (3 = 72 deg off axis), or rays beyond the frame fold back into it."""
    k1, k2, _, _, k3 = dist
    r2 = np.linspace(0, rmax, 600) ** 2
    if np.any(1 + 3 * k1 * r2 + 5 * k2 * r2 ** 2 + 7 * k3 * r2 ** 3 <= 0):
        raise ValueError(f"distortion {list(dist)} folds back inside r < {rmax}")


# --- fitting -----------------------------------------------------------------

def detect_corners(gray: np.ndarray, pattern: Tuple[int, int] = PATTERN) -> Optional[np.ndarray]:
    """Sub-pixel inner-corner grid (Nx2), or None. Boards cut by the frame
    edge are missed at full size but found on a 1/2 or 1/3 copy; the rescale
    is pixel-centre aligned so every scale gives the same coordinates."""
    flags = cv2.CALIB_CB_NORMALIZE_IMAGE | cv2.CALIB_CB_EXHAUSTIVE | cv2.CALIB_CB_ACCURACY
    for s in (1, 2, 3):
        g = gray if s == 1 else cv2.resize(gray, (gray.shape[1] // s, gray.shape[0] // s),
                                           interpolation=cv2.INTER_AREA)
        ok, corners = cv2.findChessboardCornersSB(g, pattern, flags)
        if ok:
            return corners.reshape(-1, 2).astype(np.float64) * s + (s - 1) / 2
    return None


def _residuals(corners, K, dist, rvecs, tvecs, objp) -> List[np.ndarray]:
    out = []
    for c, rv, tv in zip(corners, rvecs, tvecs):
        proj, _ = cv2.projectPoints(objp, rv, tv, K, dist)
        out.append(np.linalg.norm(proj.reshape(-1, 2) - c, axis=1))
    return out


def fit(corners: List[np.ndarray], size: Tuple[int, int], pattern: Tuple[int, int] = PATTERN) -> dict:
    """K and (k1, k2, k3) from the corner grids of one camera (tangential terms
    stay 0: held-out error does not improve with them). Corners further than
    4x the median residual from the fit (a bezel-hugging detection, motion
    blur) are dropped and the fit is repeated."""
    objp = np.zeros((pattern[0] * pattern[1], 3), np.float64)
    objp[:, :2] = np.mgrid[0:pattern[0], 0:pattern[1]].T.reshape(-1, 2)   # unit squares: K, dist are scale-free
    keep = [np.ones(len(objp), bool) for _ in corners]
    for _ in range(3):
        fitted = keep
        rms, K, dist, rvecs, tvecs = cv2.calibrateCamera(
            [objp[k].astype(np.float32) for k in fitted],
            [c[k].astype(np.float32) for c, k in zip(corners, fitted)],
            size, None, None, flags=cv2.CALIB_ZERO_TANGENT_DIST)
        res = _residuals(corners, K, dist, rvecs, tvecs, objp)
        thr = max(0.5, 4 * float(np.median(np.concatenate([r[k] for r, k in zip(res, fitted)]))))
        keep = [r < thr for r in res]
        if all((a == b).all() for a, b in zip(keep, fitted)):
            break
    dist = dist.ravel()
    check_monotonic(dist)
    per_image = [float(np.sqrt((r[k] ** 2).mean())) for r, k in zip(res, fitted)]
    tilts = [float(np.degrees(np.arccos(abs(cv2.Rodrigues(rv)[0][2, 2])))) for rv in rvecs]
    return {"K": K, "dist": dist, "rms": float(rms), "per_image": per_image, "tilts": tilts,
            "rejected": [int((~k).sum()) for k in fitted], "size": size}


def coverage(corners: List[np.ndarray], fit_result: dict) -> List[str]:
    """Where the corners fell, as report lines; warns about thin spots (the
    distortion is only constrained where boards were held)."""
    w, h = fit_result["size"]
    pts = np.concatenate(corners)
    gx, gy = 16, 9
    grid, _, _ = np.histogram2d(pts[:, 1], pts[:, 0], bins=[gy, gx], range=[[0, h], [0, w]])
    lines = [f"corners per {w // gx}px cell (top to bottom):"]
    lines += ["  " + " ".join(f"{int(v):3d}" if v else "  ." for v in row) for row in grid]
    lines.append(f"cells covered: {(grid > 0).mean() * 100:.0f}%")
    reach = []
    for name, sel in (("left", lambda p: p[:, 0] < EDGE_STRIP * w), ("right", lambda p: p[:, 0] > (1 - EDGE_STRIP) * w)):
        n_img = sum(bool(sel(c).any()) for c in corners)
        reach.append(n_img)
        lines.append(f"boards reaching the {name} {EDGE_STRIP:.0%} strip: {n_img}")
    tilted = sum(t >= TILT_DEG for t in fit_result["tilts"])
    lines.append(f"board tilt: median {np.median(fit_result['tilts']):.0f} deg, max {max(fit_result['tilts']):.0f} deg, "
                 f"{tilted} boards >= {TILT_DEG} deg")
    if min(reach) < MIN_EDGE_IMAGES:
        lines.append(f"WARNING: fewer than {MIN_EDGE_IMAGES} boards reach an edge strip; hold the board near the "
                     "left/right edges (that is where the overlap sits)")
    if tilted < MIN_TILTED:
        lines.append(f"WARNING: fewer than {MIN_TILTED} boards tilted {TILT_DEG}+ deg; tilt it more so focal length "
                     "and distortion separate")
    return lines


def fit_dir(captures: Path, cam: str, out_dir: Path, pattern: Tuple[int, int] = PATTERN) -> dict:
    """Detect corners in captures/<cam>_*.jpg, fit, print the report, write <out_dir>/<cam>.json."""
    files = sorted(captures.glob(f"{cam}_*.jpg"))
    if len(files) < 10:
        raise SystemExit(f"{cam}: {len(files)} captures in {captures}, need at least 10")
    corners, used, size = [], [], None
    for f in files:
        gray = cv2.imread(str(f), cv2.IMREAD_GRAYSCALE)
        if gray is None:
            raise SystemExit(f"cannot read {f}")
        size = size or (gray.shape[1], gray.shape[0])
        found = detect_corners(gray, pattern)
        if found is None:
            print(f"  {f.name}: no {pattern[0]}x{pattern[1]} board found, skipped")
        else:
            corners.append(found)
            used.append(f)
    result = fit(corners, size, pattern)
    K, dist = result["K"], result["dist"]
    print(f"{cam}: {len(corners)}/{len(files)} captures, RMS {result['rms']:.3f} px, "
          f"{sum(result['rejected'])} corners rejected")
    print(f"  fx={K[0, 0]:.2f} fy={K[1, 1]:.2f} cx={K[0, 2]:.2f} cy={K[1, 2]:.2f}  "
          f"k1={dist[0]:.5f} k2={dist[1]:.5f} k3={dist[4]:.5f}")
    worst = [i for i in sorted(range(len(used)), key=lambda i: -result["rejected"][i])[:3] if result["rejected"][i]]
    if worst:
        print("  most rejected corners (retake these poses): "
              + ", ".join(f"{used[i].name}={result['rejected'][i]}" for i in worst))
    for line in coverage(corners, result):
        print("  " + line)
    out_dir.mkdir(parents=True, exist_ok=True)
    fields = {
        "image_size": list(size),
        "K": [[round(float(v), 4) for v in row] for row in K],
        "dist": [round(float(v), 6) for v in dist],
        "rms_px": round(result["rms"], 4),
        "images": len(corners),
        "corners_rejected": sum(result["rejected"]),
    }
    body = ",\n".join(f'  "{k}": {json.dumps(v)}' for k, v in fields.items())   # one line per field
    (out_dir / f"{cam}.json").write_text("{\n" + body + "\n}\n")
    print(f"  wrote {out_dir / (cam + '.json')}")
    return result


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m afl_cam.lens", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("fit", help="fit intrinsics from checkerboard stills")
    p.add_argument("captures", type=Path, help="directory with cam0_NNN.jpg / cam1_NNN.jpg")
    p.add_argument("--cam", choices=("cam0", "cam1", "both"), default="both")
    p.add_argument("--out", type=Path, default=INTRINSICS_DIR)
    p.add_argument("--pattern", default="10x7", help="inner corners, columns x rows")
    args = ap.parse_args(argv)
    cols, rows = (int(v) for v in args.pattern.split("x"))
    for cam in ("cam0", "cam1") if args.cam == "both" else (args.cam,):
        fit_dir(args.captures, cam, args.out, (cols, rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
