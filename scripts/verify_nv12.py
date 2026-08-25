"""NV12 reference + SSIM harness for the live GPU stitch (S1 validation).

Replicates the exact math of stitch_nv12.comp in numpy: analytic spherical
warp (params.bin), bilinear NV12 sampling with border-black (cv2.remap —
texel centers match the shader's (s+0.5)/dims), BT.601 limited-range decode,
weighted blend, BT.601 encode with 2x2 chroma block means.

Usage:
    python3 scripts/verify_nv12.py <params_dir> [gpu_out.nv12]

    <params_dir>   dir with params.bin, w0.bin, w1.bin, cam0.rgb, cam1.rgb
                   (as written by dump_camera_params.py)
    gpu_out.nv12   optional GPU output to compare (Y/U/V plane SSIM)

Writes <params_dir>/ref.nv12 (reference output) + ref.png (side-by-side
eyeball: left = reference, right = GPU output when given).
"""

import sys
from pathlib import Path

import cv2
import numpy as np

# Must match the compiled shader (stitch_nv12.comp)
RANGE_FULL = 0


def load_nv12_to_rgb(params_dir: Path):
    """Build NV12 test inputs from the dump script's camN.rgb stills."""
    src_w = 3840
    cam0 = np.fromfile(params_dir / "cam0.rgb", np.uint8).reshape(-1, src_w, 3)
    cam1 = np.fromfile(params_dir / "cam1.rgb", np.uint8).reshape(-1, src_w, 3)
    nv12 = []
    for rgb in (cam0, cam1):
        bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
        yuv = cv2.cvtColor(bgr, cv2.COLOR_BGR2YUV_I420)
        h, w = rgb.shape[:2]
        y = yuv[:h]
        u = yuv[h:h + h // 4].reshape(h // 2, w // 2)
        v = yuv[h + h // 4:].reshape(h // 2, w // 2)
        nv12.append((y, u, v))
    return nv12


def nv12_to_rgb(y, u, v):
    yf = (y.astype(np.float64) - 16.0) * (255.0 / 219.0)
    uf = u.astype(np.float64) - 128.0
    vf = v.astype(np.float64) - 128.0
    r = yf + 1.402 * vf
    g = yf - 0.344136 * uf - 0.714136 * vf
    b = yf + 1.772 * uf
    return np.clip(np.stack([r, g, b], axis=-1), 0, 255)


def rgb_to_y(rgb):
    return np.clip(np.tensordot(rgb, [0.257, 0.504, 0.098], axes=([2], [0]))
                   + 16.0, 0, 255)


def rgb_to_u(rgb):
    return np.clip(np.tensordot(rgb, [-0.148, -0.291, 0.439], axes=([2], [0]))
                   + 128.0, 0, 255)


def rgb_to_v(rgb):
    return np.clip(np.tensordot(rgb, [0.439, -0.368, -0.071], axes=([2], [0]))
                   + 128.0, 0, 255)


def warp_coords_at(params, cam, xs, ys):
    """Plain-math warp at arbitrary pixel-corner coords (float32).
    Replicates the shader's warp_cam exactly (distortion zeroed)."""
    warp_scale = float(params[4])
    base = 8 + cam * 28
    tl_x, tl_y = float(params[base + 9]), float(params[base + 10])
    off_x, off_y = float(params[base + 11]), float(params[base + 12])
    map_w, map_h = float(params[base + 13]), float(params[base + 14])
    krinv = params[base:base + 9].reshape(3, 3).astype(np.float64)

    cx, cy = np.meshgrid(xs, ys)  # pixel-corner coords

    mcx = cx - off_x
    mcy = cy - off_y
    valid = (mcx >= 0) & (mcx < map_w) & (mcy >= 0) & (mcy < map_h)

    u = (mcx + tl_x) / warp_scale
    v = (mcy + tl_y) / warp_scale
    sin_v = np.sin(np.pi - v)
    dir_x = sin_v * np.sin(u)
    dir_y = np.cos(np.pi - v)
    dir_z = sin_v * np.cos(u)

    x = krinv[0, 0] * dir_x + krinv[0, 1] * dir_y + krinv[0, 2] * dir_z
    y = krinv[1, 0] * dir_x + krinv[1, 1] * dir_y + krinv[1, 2] * dir_z
    z = krinv[2, 0] * dir_x + krinv[2, 1] * dir_y + krinv[2, 2] * dir_z
    valid &= z > 0

    sx = np.where(valid, x / np.maximum(z, 1e-12), -1).astype(np.float32)
    sy = np.where(valid, y / np.maximum(z, 1e-12), -1).astype(np.float32)
    return sx, sy


def warp_coords(params, cam):
    """Warp over the full canvas grid."""
    return warp_coords_at(params, cam,
                          np.arange(int(params[0]), dtype=np.float64),
                          np.arange(int(params[1]), dtype=np.float64))


def sample_plane(img, sx, sy, scale):
    """Bilinear sample at raw coords (sx, sy).

    cv2.remap samples at integer pixel centers; the shader samples at
    (s+0.5)/dims with texel centers (i+0.5)/N, so the source coordinate is
    exactly s (the +0.5 cancels). scale adjusts for the half-res UV plane
    (shader UV coord = (s+0.5)/2 - 0.5 = s/2 - 0.25). The shader's explicit
    out-of-frame guard is applied by the CALLER on the full-res s.
    """
    if scale != 1.0:
        sx = sx * scale - 0.25
        sy = sy * scale - 0.25
    return cv2.remap(img, sx, sy, cv2.INTER_LINEAR,
                     borderMode=cv2.BORDER_CONSTANT, borderValue=0)


def reference_nv12(params_dir: Path):
    params = np.fromfile(params_dir / "params.bin", np.float32)
    cw, ch = int(params[0]), int(params[1])
    assert cw % 2 == 0 and ch % 2 == 0, "canvas must be even for NV12"

    w0 = np.fromfile(params_dir / "w0.bin", np.uint8).reshape(ch, cw, 4)
    w1 = np.fromfile(params_dir / "w1.bin", np.uint8).reshape(ch, cw, 4)
    cams = load_nv12_to_rgb(params_dir)

    # per-camera warp: Y and UV planes sampled at the warp coords.
    # The shader's guard (s < 0 or s >= dims-1, full-res pixel-corner space)
    # zeroes out-of-frame samples — mirror it here.
    src_w, src_h = int(params[2]), int(params[3])
    rgb_cams = []
    for cam in range(2):
        y, u, v = cams[cam]
        sx, sy = warp_coords(params, cam)
        valid = (sx >= 0) & (sx < src_w - 1.0) & (sy >= 0) & (sy < src_h - 1.0)
        ys = sample_plane(y, sx, sy, 1.0)
        us = sample_plane(u, sx, sy, 0.5)
        vs = sample_plane(v, sx, sy, 0.5)
        rgb = nv12_to_rgb(ys, us, vs)
        rgb[~valid] = 0  # RGB-space black — matches the shader's guard return
        rgb_cams.append(rgb)

    # blend with the uint8 weights
    out = (rgb_cams[0] * (w0[..., :1] / 255.0) +
           rgb_cams[1] * (w1[..., :1] / 255.0))

    y_out = np.round(rgb_to_y(out)).astype(np.uint8)

    # chroma: blend at the 2x2 BLOCK CENTERS (2bx+1, 2by+1) — matching the
    # shader's block-center sampling. The weight texel there is the nearest
    # canvas pixel (2bx+1, 2by+1) per the shader's (p+0.5)/dims + NEAREST.
    bxs = np.arange(cw // 2, dtype=np.float64) * 2 + 1
    bys = np.arange(ch // 2, dtype=np.float64) * 2 + 1
    rgb_chroma = []
    for cam in range(2):
        y, u, v = cams[cam]
        sx, sy = warp_coords_at(params, cam, bxs, bys)
        valid = (sx >= 0) & (sx < src_w - 1.0) & (sy >= 0) & (sy < src_h - 1.0)
        ys = sample_plane(y, sx, sy, 1.0)
        us = sample_plane(u, sx, sy, 0.5)
        vs = sample_plane(v, sx, sy, 0.5)
        rgb = nv12_to_rgb(ys, us, vs)
        rgb[~valid] = 0  # RGB-space black — matches the shader's guard return
        rgb_chroma.append(rgb)
    w0c = w0[2 * np.arange(ch // 2) + 1][:, 2 * np.arange(cw // 2) + 1, 0] / 255.0
    w1c = w1[2 * np.arange(ch // 2) + 1][:, 2 * np.arange(cw // 2) + 1, 0] / 255.0
    outc = rgb_chroma[0] * w0c[..., None] + rgb_chroma[1] * w1c[..., None]
    u_out = np.round(rgb_to_u(outc)).astype(np.uint8)
    v_out = np.round(rgb_to_v(outc)).astype(np.uint8)

    nv12 = np.zeros((ch * 3 // 2, cw), np.uint8)
    nv12[:ch] = y_out
    nv12[ch:] = np.stack([u_out, v_out], axis=-1).reshape(ch // 2, cw)
    return nv12


def nv12_to_bgr(nv12):
    return cv2.cvtColor(nv12, cv2.COLOR_YUV2BGR_NV12)


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    params_dir = Path(sys.argv[1])
    gpu_path = Path(sys.argv[2]) if len(sys.argv) > 2 else None

    ref = reference_nv12(params_dir)
    ref.tofile(params_dir / "ref.nv12")
    print(f"reference written: {params_dir / 'ref.nv12'} ({ref.shape[1]}x{ref.shape[0]})")

    ref_bgr = nv12_to_bgr(ref)
    if gpu_path is not None and gpu_path.exists():
        gpu = np.fromfile(gpu_path, np.uint8).reshape(ref.shape)
        h = ref.shape[0] * 2 // 3  # NV12: rows = h*3/2 -> h
        planes = {
            "Y": (ref[:h], gpu[:h]),
            "U": (ref[h:, 0::2], gpu[h:, 0::2]),
            "V": (ref[h:, 1::2], gpu[h:, 1::2]),
        }
        print("GPU vs reference:")
        for name, (a, b) in planes.items():
            corr = cv2.matchTemplate(a.astype(np.float32), b.astype(np.float32),
                                     cv2.TM_CCOEFF_NORMED)[0, 0]
            mad = float(np.abs(a.astype(int) - b.astype(int)).mean())
            print(f"  {name}: corr={corr:.6f} mean|diff|={mad:.3f}/255")
        side = np.hstack([ref_bgr, nv12_to_bgr(gpu)])
        cv2.imwrite(str(params_dir / "ref.png"), side)
    else:
        cv2.imwrite(str(params_dir / "ref.png"), ref_bgr)
        print("no GPU output given — wrote ref.png only")


if __name__ == "__main__":
    main()
