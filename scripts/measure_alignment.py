"""Measure the residual misalignment between the two warped camera views.

Ghosting in the overlap is exactly the residual displacement between the two
warps. This quantifies it with tiled phase correlation (robust to the two
cameras' differing exposure) over the overlap region, for several alignment
variants:

  post    — fresh 2-view bundle with the bundle's own focal (what the
            post-stitch engine does per session)
  bundleK — fresh bundle R but intrinsics overridden with the measured
            checkerboard K (what dump_camera_params.py does)
  frozen  — measured K + the R frozen in calibration/stitch_live_full/cams.txt

Usage:
    python3 scripts/measure_alignment.py [cam0.jpg] [cam1.jpg] [--width 1600]
"""

import sys
import types
from pathlib import Path

import cv2
import numpy as np

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from afl_cam.stitcher import Stitcher, _camera_matrices, _spherical_bounds, _spherical_maps  # noqa: E402

CALIB_DIR = REPO / "calibration"


def load_measured(cam_key):
    d = np.load(CALIB_DIR / f"{cam_key}_calib.npz")
    return {"mtx": d["mtx"], "dist": d["dist"].ravel()}


def fake_cam(fx, fy, cx, cy, R, aspect=None):
    return types.SimpleNamespace(
        focal=fx, ppx=cx, ppy=cy,
        aspect=(fy / fx) if aspect is None else aspect,
        R=R.astype(np.float64).reshape(3, 3))


def fresh_bundle(stills, f0, f1):
    """Reproduce the post-stitch bundle loop exactly."""
    for seed in (0, 42):
        cv2.setRNGSeed(seed)
        for mode in (cv2.Stitcher_PANORAMA, cv2.Stitcher_SCANS):
            s = cv2.Stitcher.create(mode)
            s.setRegistrationResol(stills._registration_resol)
            s.setSeamEstimationResol(stills._seam_resol)
            s.setCompositingResol(-1)
            s.setPanoConfidenceThresh(stills._pano_confidence)
            if s.estimateTransform([stills._enhance(f0), stills._enhance(f1)]) == cv2.Stitcher_OK:
                cams = s.cameras()
                ws = s.workScale()
                if all(0.3 * f0.shape[1] < c.focal / ws < 3.0 * f0.shape[1] for c in cams):
                    return cams, ws
    return None, None


def build_warp_pair(cams, work_scale, full_size, w_scale, k=1.0):
    """Maps + warped stills for both cameras (no undistort — dist is zeroed)."""
    m0 = _spherical_maps(cams[0], work_scale, full_size, k, None, w_scale)
    m1 = _spherical_maps(cams[1], work_scale, full_size, k, None, w_scale)
    b0 = _spherical_bounds(cams[0], work_scale, full_size, w_scale)
    b1 = _spherical_bounds(cams[1], work_scale, full_size, w_scale)
    min_x = min(b0[0], b1[0])
    min_y = min(b0[1], b1[1])
    off0 = (int(round((b0[0] - min_x) * k)), int(round((b0[1] - min_y) * k)))
    off1 = (int(round((b1[0] - min_x) * k)), int(round((b1[1] - min_y) * k)))
    out_w = (max(off0[0] + m0[0].shape[1], off1[0] + m1[0].shape[1]) + 3) // 4 * 4
    out_h = (max(off0[1] + m0[0].shape[0], off1[1] + m1[0].shape[0]) + 1) // 2 * 2
    return m0, m1, off0, off1, out_w, out_h


def overlap_crops(out_w, m0, m1, off0, off1):
    oy0 = max(off0[1], off1[1])
    oy1 = min(off0[1] + m0[0].shape[0], off1[1] + m1[0].shape[0])
    ox0 = max(off0[0], off1[0])
    ox1 = min(off0[0] + m0[0].shape[1], off1[0] + m1[0].shape[1])
    c0 = (oy0 - off0[1], oy1 - off0[1], ox0 - off0[0], ox1 - off0[0])
    c1 = (oy0 - off1[1], oy1 - off1[1], ox0 - off1[0], ox1 - off1[0])
    return c0, c1, (oy0, oy1, ox0, ox1)


def residual_stats(g0, g1, tile=96, stride=48):
    """Tiled phase-correlation shift |dx| between two aligned patches.

    g0/g1: grayscale float32 arrays of the same shape (the overlap crops).
    CROSS-CAMERA PITFALL: the two cameras' different exposure/gain give
    decorrelated fine texture (noise), which phase correlation latches onto
    and over-reads shifts (3-26px of noise measured on perfectly aligned
    content). The fix is a lowpass pre-filter (sigma 8) — large-scale
    content correlates across cameras and reads sub-pixel when aligned.
    Returns (shifts, tiles) where shifts are (x, y, magnitude) per tile.
    """
    g0 = cv2.GaussianBlur(g0, (0, 0), 8)
    g1 = cv2.GaussianBlur(g1, (0, 0), 8)
    e0 = cv2.Sobel(g0, cv2.CV_32F, 1, 0, ksize=3) ** 2 + cv2.Sobel(g0, cv2.CV_32F, 0, 1, ksize=3) ** 2
    e1 = cv2.Sobel(g1, cv2.CV_32F, 1, 0, ksize=3) ** 2 + cv2.Sobel(g1, cv2.CV_32F, 0, 1, ksize=3) ** 2
    hann = np.hanning(tile)[:, None] * np.hanning(tile)[None, :]
    shifts = []
    for y in range(0, g0.shape[0] - tile + 1, stride):
        for x in range(0, g0.shape[1] - tile + 1, stride):
            a = e0[y:y + tile, x:x + tile] * hann
            b = e1[y:y + tile, x:x + tile] * hann
            if a.std() < 0.5 or b.std() < 0.5:  # textureless tile
                continue
            (dx, dy), resp = cv2.phaseCorrelate(a.astype(np.float64), b.astype(np.float64))
            if resp < 0.05:  # low confidence
                continue
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
        "max": float(np.max(mags)),
        "tiles": arr,
    }


def measure_variant(label, cams, work_scale, f0, f1, w_scale, k=1.0, tag=""):
    full_size = (f0.shape[1], f0.shape[0])
    m0, m1, off0, off1, out_w, out_h = build_warp_pair(cams, work_scale, full_size, w_scale, k)
    w0 = cv2.remap(f0, m0[0], m0[1], cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=0)
    w1 = cv2.remap(f1, m1[0], m1[1], cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=0)
    c0, c1, ol_roi = overlap_crops(out_w, m0, m1, off0, off1)
    ow = ol_roi[3] - ol_roi[2]
    oh = ol_roi[1] - ol_roi[0]
    g0 = cv2.cvtColor(w0[c0[0]:c0[1], c0[2]:c0[3]], cv2.COLOR_BGR2GRAY).astype(np.float32)
    g1 = cv2.cvtColor(w1[c1[0]:c1[1], c1[2]:c1[3]], cv2.COLOR_BGR2GRAY).astype(np.float32)
    res = residual_stats(g0, g1)
    print(f"[{label}] canvas {out_w}x{out_h} overlap {ow}x{oh} "
          f"(= {ow / out_w * 100:.0f}% of canvas width)")
    if res is None:
        print(f"[{label}] no textured tiles — cannot measure")
        return None
    print(f"[{label}] {res['n_tiles']} tiles: median {res['median']:.2f}px  "
          f"mean {res['mean']:.2f}px  p90 {res['p90']:.2f}px  max {res['max']:.2f}px")
    # spatial pattern: median magnitude per 3x2 grid over the overlap
    tiles = res["tiles"]
    grid_y, grid_x = 2, 3
    print(f"[{label}] median |shift| by region (rows x cols):")
    for gy in range(grid_y):
        row = []
        for gx in range(grid_x):
            xs = (tiles[:, 0] > ow * gx / grid_x) & (tiles[:, 0] <= ow * (gx + 1) / grid_x)
            ys = (tiles[:, 1] > oh * gy / grid_y) & (tiles[:, 1] <= oh * (gy + 1) / grid_y)
            sel = tiles[xs & ys]
            row.append(f"{np.median(sel[:, 4]):5.2f}" if len(sel) else "   - ")
        print(f"[{label}]   {' '.join(row)}")
    # fine row profile: top = distant (horizon), bottom = near ground
    bands = 8
    prof = []
    for b in range(bands):
        ys = (tiles[:, 1] > oh * b / bands) & (tiles[:, 1] <= oh * (b + 1) / bands)
        sel = tiles[ys]
        prof.append(f"{np.median(sel[:, 4]):.1f}" if len(sel) else "-")
    print(f"[{label}] row profile (top→bottom, {bands} bands): {' '.join(prof)}")
    # save an edge-overlay diff of the overlap for visual inspection
    vis = np.dstack([np.zeros_like(g0), np.zeros_like(g0), np.zeros_like(g0)])
    edges0 = cv2.Canny(g0.astype(np.uint8), 60, 160)
    edges1 = cv2.Canny(g1.astype(np.uint8), 60, 160)
    vis[:, :, 0] = edges0  # red = cam0 edges
    vis[:, :, 1] = edges1  # green = cam1 edges
    out_png = f"/tmp/align_{tag}_{label}.png"
    cv2.imwrite(out_png, vis)
    print(f"[{label}] edge overlay (red=cam0, green=cam1) → {out_png}")

    # feature-based angular residual (definitive: geometry error vs parallax)
    K0, R0 = _camera_matrices(cams[0], work_scale)
    K1, R1 = _camera_matrices(cams[1], work_scale)
    sres = sift_residual(f0, f1, (K0, K1), (R0, R1), w_scale)
    if sres:
        print(f"[{label}] sift: {sres['n']} matches, angular residual "
              f"median {sres['median']:.2f}px mean {sres['mean']:.2f}px "
              f"p90 {sres['p90']:.2f}px max {sres['max']:.2f}px")
    return res


def parse_frozen_cams(path):
    rows = {}
    with open(path) as fh:
        for line in fh:
            parts = line.split()
            if len(parts) < 2:
                continue
            try:
                vals = [float(v) for v in parts[1:]]
            except ValueError:
                continue
            rows.setdefault(parts[0], []).append(vals)
    R = {i: np.array(rows[f"R{i}"], dtype=np.float64) for i in (0, 1)}
    K = {i: np.array(rows[f"K{i}"], dtype=np.float64) for i in (0, 1)}
    return K, R, rows["warp_scale"][0][0]


def sift_residual(f0, f1, K, R, w_scale):
    """Feature-based angular residual: for each SIFT match, the angle between
    the two cameras' rays to the matched point, in canvas-px equivalents.

    Geometry error shows as residual that's large even for distant features;
    pure parallax shows as residual growing for near features. We can't know
    depth per feature, but the distribution tells the story: a tight cluster
    at 0-1px = good alignment; wide spread with nonzero median = geometry
    error (the cameras' rotations disagree).
    """
    sift = cv2.SIFT_create(nfeatures=4000)
    kp0, des0 = sift.detectAndCompute(f0, None)
    kp1, des1 = sift.detectAndCompute(f1, None)
    bf = cv2.BFMatcher(cv2.NORM_L2)
    knn = bf.knnMatch(des0, des1, k=2)
    good = [m for m, n in knn if m.distance < 0.7 * n.distance]
    print(f"  sift: {len(kp0)}/{len(kp1)} keypoints, {len(good)} good matches")
    if not good:
        return None
    K0i = np.linalg.inv(K[0])
    K1i = np.linalg.inv(K[1])
    angs = []
    for m in good:
        p0 = np.array([kp0[m.queryIdx].pt[0], kp0[m.queryIdx].pt[1], 1.0])
        p1 = np.array([kp1[m.trainIdx].pt[0], kp1[m.trainIdx].pt[1], 1.0])
        d0 = R[0] @ K0i @ p0
        d1 = R[1] @ K1i @ p1
        d0 /= np.linalg.norm(d0)
        d1 /= np.linalg.norm(d1)
        a = np.arccos(np.clip(d0 @ d1, -1.0, 1.0))
        angs.append(a * w_scale)  # canvas px at the canvas center scale
    arr = np.asarray(angs)
    return {"n": len(good), "median": float(np.median(arr)), "mean": float(np.mean(arr)),
            "p90": float(np.percentile(arr, 90)), "max": float(np.max(arr)), "all": arr}


def main():
    argv = sys.argv[1:]
    frozen_path = CALIB_DIR / "stitch_live_full" / "cams.txt"
    if "--cams" in argv:
        i = argv.index("--cams")
        frozen_path = Path(argv[i + 1])
        del argv[i:i + 2]
    f0p = Path(argv[0]) if len(argv) > 0 else \
        REPO / "outputs" / "20260901_190732" / "cam0.jpg"
    f1p = Path(argv[1]) if len(argv) > 1 else \
        REPO / "outputs" / "20260901_190732" / "cam1.jpg"
    f0 = cv2.imread(str(f0p))
    f1 = cv2.imread(str(f1p))
    if f0 is None or f1 is None:
        raise SystemExit("cannot read stills")
    print(f"stills: {f0p} ({f0.shape[1]}x{f0.shape[0]})  {f1p}")

    stills = Stitcher()
    cal0, cal1 = load_measured("cam0"), load_measured("cam1")
    mtx0, mtx1 = cal0["mtx"], cal1["mtx"]

    # variant: post (fresh bundle, bundle focal — the per-session engine path)
    cams, ws = fresh_bundle(stills, f0, f1)
    if cams is None:
        print("fresh bundle FAILED on these stills")
    else:
        w_scale = float(np.median([c.focal for c in cams])) / ws
        print(f"bundle focal: cam0={cams[0].focal / ws:.1f} cam1={cams[1].focal / ws:.1f} "
              f"(measured {mtx0[0, 0]:.1f}/{mtx1[0, 0]:.1f})")
        measure_variant("post", cams, ws, f0, f1, w_scale, tag="post")

    # variant: bundleK (fresh bundle R + measured K — the dump_camera_params flow)
    if cams is not None:
        cam_k = []
        for idx, cam in enumerate(cams):
            m = mtx0 if idx == 0 else mtx1
            cam_k.append(fake_cam(m[0, 0], m[1, 1], m[0, 2], m[1, 2], cam.R))
        w_scale = float(np.median([c.focal for c in cam_k])) / 1.0
        measure_variant("bundleK", cam_k, 1.0, f0, f1, w_scale, tag="bundleK")

    # variant: frozen (cams.txt from the last realign)
    if frozen_path.exists():
        K, R, w_scale = parse_frozen_cams(frozen_path)
        cam_f = [fake_cam(K[i][0, 0], K[i][1, 1], K[i][0, 2], K[i][1, 2], R[i])
                 for i in (0, 1)]
        measure_variant("frozen", cam_f, 1.0, f0, f1, w_scale, tag="frozen")
    else:
        print("no frozen cams.txt")


if __name__ == "__main__":
    main()
