"""Extract camera params for the analytic GPU spherical shader.

Intrinsics come from the MEASURED checkerboard calibration
(calibration/cam{0,1}_calib.npz) — the two cameras' independent 5-coeff fits
agree on K to 0.04%, the trustworthy signal. The fitted distortion
coefficients describe the BOARD's waviness (paper board), not the lens, and
are zeroed: the stitch treats the lens as pinhole + measured K.

The inter-camera rotation R comes from OpenCV's stitcher bundle adjustment
on the RAW stills (the same estimateTransform flow the product uses) — its R
is the photometrically validated one (the ray-angle Kabsch fit is biased by
parallax: it landed 2.8 deg from the photometric optimum).

Usage: python3 scripts/dump_camera_params.py <cam0.jpg> <cam1.jpg> <out_dir>
"""

import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from afl_cam.stitcher import Stitcher, _build_uint8_weights, _spherical_bounds, _spherical_maps  # noqa: E402

CALIB_DIR = Path(__file__).resolve().parent.parent / "calibration"


def load_measured(cam_key):
    f = CALIB_DIR / f"{cam_key}_calib.npz"
    if not f.exists():
        raise SystemExit(f"missing {f.name} — run scripts/calibrate.py first")
    d = np.load(f)
    return {"mtx": d["mtx"], "dist": d["dist"].ravel()}


def main() -> None:
    argv = sys.argv[1:]
    canvas_width = None
    if "--canvas-width" in argv:
        i = argv.index("--canvas-width")
        if i + 1 >= len(argv):
            raise SystemExit("--canvas-width needs a value")
        canvas_width = int(argv[i + 1])
        del argv[i:i + 2]
    if len(argv) != 3:
        raise SystemExit(
            "usage: python3 scripts/dump_camera_params.py <cam0.jpg> <cam1.jpg> "
            "<out_dir> [--canvas-width W]")
    cam0_path, cam1_path, out_dir = argv[0], argv[1], Path(argv[2])
    out_dir.mkdir(parents=True, exist_ok=True)

    f0 = cv2.imread(cam0_path)
    f1 = cv2.imread(cam1_path)
    if f0 is None or f1 is None:
        raise SystemExit("cannot read inputs")
    src_w, src_h = f0.shape[1], f0.shape[0]
    full_size = (src_w, src_h)

    calibs = [load_measured("cam0"), load_measured("cam1")]
    print(f"measured calib: cam0 fx={calibs[0]['mtx'][0,0]:.1f} "
          f"cam1 fx={calibs[1]['mtx'][0,0]:.1f} (distortion zeroed)")

    stills = Stitcher()
    stitcher = None
    cameras = None
    work_scale = None
    for seed in (0, 42):
        cv2.setRNGSeed(seed)
        for mode in (cv2.Stitcher_PANORAMA, cv2.Stitcher_SCANS):
            s = cv2.Stitcher.create(mode)
            s.setRegistrationResol(stills._registration_resol)
            s.setSeamEstimationResol(stills._seam_resol)
            s.setCompositingResol(-1)
            s.setPanoConfidenceThresh(stills._pano_confidence)
            if s.estimateTransform([stills._enhance(f0), stills._enhance(f1)]) == cv2.Stitcher_OK:
                stitcher, cameras = s, s.cameras()
                break
        if cameras is not None:
            work_scale = stitcher.workScale()
            focals = [cam.focal / work_scale for cam in cameras]
            if all(0.3 * src_w < f < 3.0 * src_w for f in focals):
                break
            stitcher, cameras = None, None
    if stitcher is None:
        raise SystemExit("bundle estimation failed")

    # override the estimator's intrinsics with the measured ones
    cam_dump = []
    for idx, cam in enumerate(cameras):
        m = calibs[idx]["mtx"]
        fx, fy, cx, cy = m[0, 0], m[1, 1], m[0, 2], m[1, 2]
        cam.focal = fx * work_scale
        cam.ppx = cx * work_scale
        cam.ppy = cy * work_scale
        cam.aspect = fy / fx
        K = np.array([[fx, 0, cx], [0, fy, cy], [0, 0, 1]], dtype=np.float64)
        R = np.asarray(cam.R, dtype=np.float64).reshape(3, 3)
        cam_dump.append((K, R))
        print(f"cam{idx}: K=[[{fx:.1f},0,{cx:.1f}],[0,{fy:.1f},{cy:.1f}]]")

    # spherical geometry, plain math (same formulas the GPU shader will use)
    w_scale = float(np.median([cam.focal for cam in cameras])) / work_scale
    b0 = _spherical_bounds(cameras[0], work_scale, full_size, w_scale)
    b1 = _spherical_bounds(cameras[1], work_scale, full_size, w_scale)
    min_x = min(b0[0], b1[0])
    max_x = max(b0[0] + b0[2], b1[0] + b1[2])
    min_y = min(b0[1], b1[1])
    max_y = max(b0[1] + b0[3], b1[1] + b1[3])
    nat_cw, nat_ch = max_x - min_x, max_y - min_y
    # scale_k = canvas pixels per native unit: 1.0 = native (~5760 wide),
    # or scaled to a target width (3840 for the live-stitch encoder).
    k_scale = (canvas_width / nat_cw) if canvas_width else 1.0

    m0 = _spherical_maps(cameras[0], work_scale, full_size, k_scale, None, w_scale)
    m1 = _spherical_maps(cameras[1], work_scale, full_size, k_scale, None, w_scale)

    off0 = (int(round((b0[0] - min_x) * k_scale)), int(round((b0[1] - min_y) * k_scale)))
    off1 = (int(round((b1[0] - min_x) * k_scale)), int(round((b1[1] - min_y) * k_scale)))
    roi0 = (off0[1], off0[1] + m0[0].shape[0], off0[0], off0[0] + m0[0].shape[1])
    roi1 = (off1[1], off1[1] + m1[0].shape[0], off1[0], off1[0] + m1[0].shape[1])
    # Canvas dims derive from the rounded rois (not ceil of the native span)
    # so every camera map fits exactly at any scale. Even dims required for
    # NV12 2x2 chroma blocks in the GPU path; width must also be % 4 == 0
    # for the SSBO byte-packing path in cam2stitch.
    out_w = (max(roi0[3], roi1[3]) + 3) // 4 * 4
    out_h = (max(roi0[1], roi1[1]) + 1) // 2 * 2

    mask0 = (m0[0] >= 0).astype(np.uint8) * 255
    mask1 = (m1[0] >= 0).astype(np.uint8) * 255
    oy0, oy1 = max(roi0[0], roi1[0]), min(roi0[1], roi1[1])
    ox0, ox1 = max(roi0[2], roi1[2]), min(roi0[3], roi1[3])
    overlap_c0 = (oy0 - roi0[0], oy1 - roi0[0], ox0 - roi0[2], ox1 - roi0[2])
    overlap_c1 = (oy0 - roi1[0], oy1 - roi1[0], ox0 - roi1[2], ox1 - roi1[2])
    mask0_ol = mask0[overlap_c0[0]:overlap_c0[1], overlap_c0[2]:overlap_c0[3]] > 0
    mask1_ol = mask1[overlap_c1[0]:overlap_c1[1], overlap_c1[2]:overlap_c1[3]] > 0
    w0u, w1u = _build_uint8_weights(mask0_ol, mask1_ol, 300)

    def full_weights(roi, mask, ol_roi, wc):
        full = np.zeros((out_h, out_w), np.float32)
        full[roi[0]:roi[1], roi[2]:roi[3]] = (mask > 0).astype(np.float32) * 255.0
        full[ol_roi[0]:ol_roi[1], ol_roi[2]:ol_roi[3]] = wc
        return full

    overlap_roi = (oy0, oy1, ox0, ox1)
    w0 = full_weights(roi0, mask0, overlap_roi, w0u)
    w1 = full_weights(roi1, mask1, overlap_roi, w1u)

    # GPU-ready params: per-cam block (28 floats):
    #   krinv(9) tl(2) off(2) mapwh(2) pin fx,fy,cx,cy(4) raw fx,fy,cx,cy(4)
    #   k1,k2,p1,p2,k3(5)
    # Distortion is zeroed (see module docstring); the pinhole and raw
    # matrices are then identical, and the shader's forward-distortion solve
    # degenerates to a no-op.
    with open(out_dir / "cams.txt", "w") as fh:
        fh.write(f"canvas_w {out_w}\ncanvas_h {out_h}\nsrc_w {src_w}\nsrc_h {src_h}\n")
        fh.write(f"warp_scale {w_scale}\n")
        for idx, (K, R) in enumerate(cam_dump):
            fh.write(f"cam{idx} kscale {k_scale}\n")
            for name, mtx in (("K", K), ("R", R)):
                for row in mtx:
                    fh.write(name + str(idx) + " " + " ".join(f"{v:.12g}" for v in row) + "\n")
            tl_x, tl_y, _, _ = _spherical_bounds(cameras[idx], work_scale, full_size, w_scale)
            fh.write(f"cam{idx} tl {tl_x} {tl_y}\n")
            fh.write(f"cam{idx} dist " + " ".join(f"{v:.6g}" for v in calibs[idx]["dist"]) + "\n")

    np.repeat(w0[:, :, None].astype(np.uint8), 4, axis=2).tofile(out_dir / "w0.bin")
    np.repeat(w1[:, :, None].astype(np.uint8), 4, axis=2).tofile(out_dir / "w1.bin")
    # Warp maps for the GPU map-texture path (stitch_nv12.comp fetches these
    # with texelFetch instead of running the analytic warp per pixel): the
    # exact (s,t) source coords cv2.remap consumes, at the ROI shape the
    # shader indexes via params off/mapwh. Invalid = -1 (matches the guard).
    np.stack([m0[0], m0[1]], axis=-1).astype(np.float32).tofile(out_dir / "map0.bin")
    np.stack([m1[0], m1[1]], axis=-1).astype(np.float32).tofile(out_dir / "map1.bin")
    cv2.cvtColor(f0, cv2.COLOR_BGR2RGB).tofile(out_dir / "cam0.rgb")
    cv2.cvtColor(f1, cv2.COLOR_BGR2RGB).tofile(out_dir / "cam1.rgb")

    # flat GPU params: [0..3] dims, [4] warp_scale, [5..7] pad,
    # cam0 [8..35], cam1 [36..63]
    params = np.zeros(64, np.float32)
    params[0:4] = [out_w, out_h, src_w, src_h]
    # warp_scale and tl are in CANVAS units — scale them with k_scale so the
    # shader's (mc + tl)/warp_scale stays equivalent to the native math:
    #   u = (i/k + tl)/w == (i + tl*k)/(w*k)
    params[4] = w_scale * k_scale
    for bidx, (K, R) in enumerate(cam_dump):
        base = 8 + bidx * 28
        krinv = (K @ R.T).astype(np.float32).ravel()
        params[base:base + 9] = krinv
        b = b0 if bidx == 0 else b1
        off = off0 if bidx == 0 else off1
        mshape = m0[0].shape if bidx == 0 else m1[0].shape
        params[base + 9:base + 11] = [b[0] * k_scale, b[1] * k_scale]
        params[base + 11:base + 13] = [off[0], off[1]]
        params[base + 13:base + 15] = [mshape[1], mshape[0]]
        params[base + 15:base + 19] = [K[0, 0], K[1, 1], K[0, 2], K[1, 2]]
        params[base + 19:base + 23] = [K[0, 0], K[1, 1], K[0, 2], K[1, 2]]
        params[base + 23:base + 28] = calibs[bidx]["dist"]
    params.tofile(out_dir / "params.bin")
    print(f"params.bin: warp_scale={w_scale:.1f}, krinv0={params[8:11]}, "
          f"pin0={params[23:27]}, raw0={params[27:31]}, dist0={params[31:36]}")

    # python-side reference from the same plain math (distortion is zero, so
    # the stills warp directly — the GPU must match this exactly)
    warp0 = cv2.remap(f0, m0[0], m0[1], cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=0)
    warp1 = cv2.remap(f1, m1[0], m1[1], cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=0)
    p0 = np.zeros((out_h, out_w, 3), np.uint8)
    p1 = np.zeros((out_h, out_w, 3), np.uint8)
    p0[roi0[0]:roi0[1], roi0[2]:roi0[3]] = warp0
    p1[roi1[0]:roi1[1], roi1[2]:roi1[3]] = warp1
    ref = (p0.astype(np.float32) * w0[:, :, None] + p1.astype(np.float32) * w1[:, :, None]) / 255.0
    ref = np.clip(ref, 0, 255).astype(np.uint8)
    cv2.imwrite(str(out_dir / "ref_analytic.png"), ref)
    print(f"canvas {out_w}x{out_h}, wrote cams.txt + weights + ref_analytic.png")


if __name__ == "__main__":
    main()
