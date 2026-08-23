"""Photometric refinement of the inter-camera rotation — measures and reduces
the TRUE alignment error (ghosting) in the overlap, instead of hiding it with
blending.

Cost = mean |warp0 - warp1| over the overlap, minimized over the relative
rotation (3 DOF) with the bundle-adjusted estimate as init. Optimization runs
at 1/4 resolution (alignment error is spatially smooth; the optimum
transfers); the final score is computed once at full resolution.

Usage: python3 scripts/refine_alignment.py <cam0.jpg> <cam1.jpg>
"""

import gc
import sys
import time
from pathlib import Path

import cv2
import numpy as np
from scipy.optimize import minimize
from scipy.linalg import expm

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from afl_cam.stitcher import Stitcher, _spherical_bounds, _spherical_maps  # noqa: E402


def get_geometry(f0, f1, shared_focal=False):
    stills = Stitcher()
    left, right = stills._undistort_pair(f0, f1)
    full_w = left.shape[1]
    full_size = (left.shape[1], left.shape[0])

    stitcher, cameras, work_scale = None, None, None
    for seed in (0, 42):
        cv2.setRNGSeed(seed)
        for mode in (cv2.Stitcher_PANORAMA, cv2.Stitcher_SCANS):
            s = cv2.Stitcher.create(mode)
            s.setRegistrationResol(stills._registration_resol)
            s.setSeamEstimationResol(stills._seam_resol)
            s.setCompositingResol(-1)
            s.setPanoConfidenceThresh(stills._pano_confidence)
            if s.estimateTransform([stills._enhance(left), stills._enhance(right)]) == cv2.Stitcher_OK:
                stitcher, cameras = s, s.cameras()
                break
        if cameras is not None:
            work_scale = stitcher.workScale()
            focals = [cam.focal / work_scale for cam in cameras]
            if all(0.3 * full_w < f < 3.0 * full_w for f in focals):
                break
            stitcher, cameras = None, None
    if stitcher is None:
        raise SystemExit("calibration failed")

    if shared_focal:
        fmed = float(np.median([cam.focal for cam in cameras]))
        for cam in cameras:
            cam.focal = fmed

    w_scale = float(np.median([cam.focal for cam in cameras])) / work_scale
    return stills, cameras, work_scale, w_scale, full_size


def build_maps(cam, work_scale, full_size, w_scale, scale_k=1.0):
    b = _spherical_bounds(cam, work_scale, full_size, w_scale)
    m = _spherical_maps(cam, work_scale, full_size, scale_k, None, w_scale)
    return b, m


def ghosting_score(f0, f1, w0_map, w1_map, scale_k=1.0):
    """Mean |warp0 - warp1| over the overlap region, at map resolution."""
    m0x, m0y = w0_map[0], w0_map[1]
    m1x, m1y = w1_map[0], w1_map[1]
    b0b, b1b = w0_map[2], w1_map[2]
    min_x = min(b0b[0], b1b[0])
    min_y = min(b0b[1], b1b[1])
    off0 = (int(round((b0b[0] - min_x) * scale_k)), int(round((b0b[1] - min_y) * scale_k)))
    off1 = (int(round((b1b[0] - min_x) * scale_k)), int(round((b1b[1] - min_y) * scale_k)))
    out_w = max(off0[0] + m0x.shape[1], off1[0] + m1x.shape[1])
    out_h = max(off0[1] + m0x.shape[0], off1[1] + m1x.shape[0])

    def place(mx, my, off):
        full = np.full((out_h, out_w, 2), -1.0, np.float32)
        full[off[1]:off[1] + mx.shape[0], off[0]:off[0] + mx.shape[1], 0] = mx
        full[off[1]:off[1] + my.shape[0], off[0]:off[0] + my.shape[1], 1] = my
        return full

    fm0 = place(m0x, m0y, off0)
    fm1 = place(m1x, m1y, off1)

    valid = (fm0[:, :, 0] >= 0) & (fm1[:, :, 0] >= 0)
    del fm0, fm1
    ys, xs = np.where(valid)
    if len(xs) < 100:
        return float("inf")
    p0 = cv2.remap(f0, m0x, m0y, cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=0)
    p1 = cv2.remap(f1, m1x, m1y, cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=0)
    score = float(np.abs(
        p0[ys - off0[1], xs - off0[0]].astype(np.float32) -
        p1[ys - off1[1], xs - off1[0]].astype(np.float32)).mean())
    del p0, p1, valid, ys, xs
    gc.collect()
    return score


def skew(w):
    return np.array([[0, -w[2], w[1]], [w[2], 0, -w[0]], [-w[1], w[0], 0]])


def main() -> None:
    f0 = cv2.imread(sys.argv[1])
    f1 = cv2.imread(sys.argv[2])
    if f0 is None or f1 is None:
        raise SystemExit("cannot read inputs")

    t0 = time.time()
    stills, cameras, work_scale, w_scale, full_size = get_geometry(f0, f1, shared_focal=True)
    print(f"shared-focal calibration: {time.time() - t0:.1f}s")

    # optimize at 1/4 resolution
    kq = 0.25
    b0q, m0q = build_maps(cameras[0], work_scale, full_size, w_scale, kq)
    b1q, m1q = build_maps(cameras[1], work_scale, full_size, w_scale, kq)
    w0q = (m0q[0], m0q[1], b0q)
    before_q = ghosting_score(f0, f1, w0q, (m1q[0], m1q[1], b1q), kq)
    print(f"ghosting score BEFORE refinement (1/4 res): {before_q:.2f}")

    R1_init = np.asarray(cameras[1].R, dtype=np.float64).reshape(3, 3).copy()
    f0_init = float(cameras[0].focal)
    f1_init = float(cameras[1].focal)
    evals = [0]

    def apply(x):
        # x = [log(f0/f0_init), log(f1/f1_init), w0, w1, w2]
        cameras[0].focal = f0_init * np.exp(x[0])
        cameras[1].focal = f1_init * np.exp(x[1])
        cameras[1].R = (expm(skew(x[2:5])) @ R1_init).astype(np.float64)

    def cost(x):
        apply(x)
        b0p, m0p = build_maps(cameras[0], work_scale, full_size, w_scale, kq)
        b1p, m1p = build_maps(cameras[1], work_scale, full_size, w_scale, kq)
        evals[0] += 1
        if evals[0] % 10 == 0:
            print(f"  ...{evals[0]} evals")
        return ghosting_score(f0, f1, (m0p[0], m0p[1], b0p), (m1p[0], m1p[1], b1p), kq)

    t0 = time.time()
    res = minimize(cost, np.zeros(5), method="Powell",
                   options={"maxiter": 120, "xtol": 1e-4, "ftol": 1e-3})
    print(f"refinement took {time.time() - t0:.1f}s ({res.nfev} evals)")

    apply(res.x)
    after_q = cost(res.x)
    print(f"ghosting score AFTER refinement (1/4 res):  {after_q:.2f}  ({(1 - after_q / before_q) * 100:.0f}% reduction)")
    print(f"focal factors: {np.exp(res.x[0]):.4f} / {np.exp(res.x[1]):.4f}  rotation delta: {res.x[2:5]} (rad), success={res.success}")

    # one full-res before/after measurement
    b0f, m0f = build_maps(cameras[0], work_scale, full_size, w_scale, 1.0)
    b1f, m1f = build_maps(cameras[1], work_scale, full_size, w_scale, 1.0)
    # full-res "before": rebuild cam1 at the initial rotation
    cameras[1].R = R1_init
    b1i, m1i = build_maps(cameras[1], work_scale, full_size, w_scale, 1.0)
    before_f = ghosting_score(f0, f1, (m0f[0], m0f[1], b0f), (m1i[0], m1i[1], b1i), 1.0)
    cameras[1].R = expm(skew(res.x)) @ R1_init
    after_f = ghosting_score(f0, f1, (m0f[0], m0f[1], b0f), (m1f[0], m1f[1], b1f), 1.0)
    print(f"FULL-RES: before {before_f:.2f} -> after {after_f:.2f} ({(1 - after_f / before_f) * 100:.0f}% reduction)")


if __name__ == "__main__":
    main()
