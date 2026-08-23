"""Extract bundle-adjusted camera params (K, R per camera) via OpenCV's
stitcher and dump them for the analytic GPU spherical shader.

Uses the same estimateTransform flow as the product's VideoStitcher, but
exports only the (K, R) math — no OpenCV remap machinery.

Usage: python3 scripts/dump_camera_params.py <cam0.jpg> <cam1.jpg> <out_dir>
"""

import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from afl_cam.stitcher import Stitcher, _build_uint8_weights, _spherical_bounds, _spherical_maps  # noqa: E402


def main() -> None:
    cam0_path, cam1_path, out_dir = sys.argv[1], sys.argv[2], Path(sys.argv[3])
    out_dir.mkdir(parents=True, exist_ok=True)

    f0 = cv2.imread(cam0_path)
    f1 = cv2.imread(cam1_path)
    if f0 is None or f1 is None:
        raise SystemExit("cannot read inputs")
    src_w, src_h = f0.shape[1], f0.shape[0]
    full_size = (src_w, src_h)

    stills = Stitcher()
    left, right = stills._undistort_pair(f0, f1)

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
            if s.estimateTransform([stills._enhance(left), stills._enhance(right)]) == cv2.Stitcher_OK:
                stitcher, cameras = s, s.cameras()
                break
        if cameras is not None:
            work_scale = stitcher.workScale()
            focals = [cam.focal / work_scale for cam in cameras]
            if all(0.3 * src_w < f < 3.0 * src_w for f in focals):
                break
            stitcher = cameras = None

    if stitcher is None:
        raise SystemExit("calibration failed")

    # spherical geometry, plain math (same formulas the GPU shader will use)
    w_scale = float(np.median([cam.focal for cam in cameras])) / work_scale
    b0 = _spherical_bounds(cameras[0], work_scale, full_size, w_scale)
    b1 = _spherical_bounds(cameras[1], work_scale, full_size, w_scale)
    min_x = min(b0[0], b1[0])
    max_x = max(b0[0] + b0[2], b1[0] + b1[2])
    min_y = min(b0[1], b1[1])
    max_y = max(b0[1] + b0[3], b1[1] + b1[3])
    nat_cw, nat_ch = max_x - min_x, max_y - min_y
    k_scale = 1.0  # native resolution
    out_w = int(np.ceil(nat_cw * k_scale)) // 2 * 2
    out_h = int(np.ceil(nat_ch * k_scale)) // 2 * 2

    # per-camera K (at work scale, then rescaled to full res) and R
    cam_dump = []
    for idx, cam in enumerate(cameras):
        f = cam.focal / work_scale
        cx = cam.ppx / work_scale
        cy = cam.ppy / work_scale
        aspect = getattr(cam, "aspect", 1.0)
        K = np.array([[f, 0, cx], [0, f * aspect, cy], [0, 0, 1]], dtype=np.float64)
        R = np.asarray(cam.R, dtype=np.float64).reshape(3, 3)
        cam_dump.append((K, R))
        print(f"cam{idx}: focal={f:.1f} pp=({cx:.1f},{cy:.1f}) aspect={aspect:.3f}")

    # build the float maps with plain math for a python-side reference
    undist_maps = [None, None]
    m0 = _spherical_maps(cameras[0], work_scale, full_size, k_scale, undist_maps[0], w_scale)
    m1 = _spherical_maps(cameras[1], work_scale, full_size, k_scale, undist_maps[1], w_scale)

    off0 = (int(round((b0[0] - min_x) * k_scale)), int(round((b0[1] - min_y) * k_scale)))
    off1 = (int(round((b1[0] - min_x) * k_scale)), int(round((b1[1] - min_y) * k_scale)))
    roi0 = (off0[1], off0[1] + m0[0].shape[0], off0[0], off0[0] + m0[0].shape[1])
    roi1 = (off1[1], off1[1] + m1[0].shape[0], off1[0], off1[0] + m1[0].shape[1])

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

    # GPU-ready params: per-camera K (3 rows), R (3 rows), plus the spherical
    # warp constants the shader evaluates per pixel (see _spherical_maps)
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

    np.repeat(w0[:, :, None].astype(np.uint8), 4, axis=2).tofile(out_dir / "w0.bin")
    np.repeat(w1[:, :, None].astype(np.uint8), 4, axis=2).tofile(out_dir / "w1.bin")
    cv2.cvtColor(f0, cv2.COLOR_BGR2RGB).tofile(out_dir / "cam0.rgb")
    cv2.cvtColor(f1, cv2.COLOR_BGR2RGB).tofile(out_dir / "cam1.rgb")

    # flat GPU params: [0..3] dims, [4] warp_scale, [5..7] pad,
    # cam0 [8..23], cam1 [24..39]: krinv(9) tl(2) off(2) mapwh(2) pad(1)
    params = np.zeros(40, np.float32)
    params[0:4] = [out_w, out_h, src_w, src_h]
    params[4] = w_scale
    for bidx, (K, R) in enumerate(cam_dump):
        base = 8 + bidx * 16
        krinv = (K @ R.T).astype(np.float32).ravel()
        params[base:base + 9] = krinv
        b = b0 if bidx == 0 else b1
        off = off0 if bidx == 0 else off1
        mshape = m0[0].shape if bidx == 0 else m1[0].shape
        params[base + 9:base + 11] = [b[0], b[1]]
        params[base + 11:base + 13] = [off[0], off[1]]
        params[base + 13:base + 15] = [mshape[1], mshape[0]]
    params.tofile(out_dir / "params.bin")
    print(f"params.bin: warp_scale={w_scale:.1f}, krinv0={params[8:11]}, tl0={params[17:19]}, off0={params[19:21]}, map0={params[21:23]}")

    # python-side reference from the same plain math (float maps + remap,
    # which agrees with plain bilinear to 0.18 — verified)
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
