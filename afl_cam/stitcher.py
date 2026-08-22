"""Panorama stitching for side-by-side 4K camera pairs."""

import logging
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple, Any

import cv2
import numpy as np

log = logging.getLogger(__name__)

_PKG_DIR = Path(__file__).resolve().parent
_PROJECT_DIR = _PKG_DIR.parent
_CALIB_DIR = _PROJECT_DIR / "calibration"
_OUTPUTS_DIR = _PROJECT_DIR / "outputs"


def next_output_dir() -> Path:
    """Return the next auto-numbered output directory."""
    _OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    highest = 0
    for child in _OUTPUTS_DIR.iterdir():
        if child.is_dir() and child.name.isdigit():
            highest = max(highest, int(child.name))
    out = _OUTPUTS_DIR / f"{highest + 1:03d}"
    out.mkdir(parents=True, exist_ok=True)
    return out


class Stitcher:
    """Stitch two side-by-side camera frames into a panorama."""

    def __init__(self, calib_dir: Optional[Path] = None, *,
                 clahe_clip: float = 3.0, clahe_tile: int = 8,
                 registration_resol: float = 0.6, seam_resol: float = 0.3,
                 pano_confidence: float = 0.8):
        self._clahe = cv2.createCLAHE(clipLimit=clahe_clip, tileGridSize=(clahe_tile, clahe_tile))
        self._registration_resol = registration_resol
        self._seam_resol = seam_resol
        self._pano_confidence = pano_confidence

        self._calib: Dict[str, Any] = {}
        target_dir = calib_dir if calib_dir is not None else _CALIB_DIR
        for cam in ("cam0", "cam1"):
            calib_file = target_dir / f"{cam}_calib.npz"
            if calib_file.exists():
                self._calib[cam] = np.load(str(calib_file))
                log.info(f"Loaded calibration: {calib_file.name} (rms={self._calib[cam]['rms']:.3f})")

    def stitch(self, left: np.ndarray, right: np.ndarray) -> Optional[np.ndarray]:
        left, right = self._undistort_pair(left, right)
        for label, l, r in [("enhanced", self._enhance(left), self._enhance(right)),
                            ("raw", left, right)]:
            pano = self._run_stitcher(l, r, label)
            if pano is not None:
                return pano
        log.error("All stitching attempts failed. Quality alignment requires 25-30% camera overlap.")
        return None

    def _undistort_pair(self, left: np.ndarray, right: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
        if "cam0" in self._calib:
            left = self._undistort(left, self._calib["cam0"])
        if "cam1" in self._calib:
            right = self._undistort(right, self._calib["cam1"])
        return left, right

    def _enhance(self, bgr: np.ndarray) -> np.ndarray:
        lab = cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB)
        l_channel, a_channel, b_channel = cv2.split(lab)
        l_enhanced = self._clahe.apply(l_channel)
        return cv2.cvtColor(cv2.merge([l_enhanced, a_channel, b_channel]), cv2.COLOR_LAB2BGR)

    @staticmethod
    def _undistort(img: np.ndarray, calib: Any) -> np.ndarray:
        h, w = img.shape[:2]
        new_mtx, _ = cv2.getOptimalNewCameraMatrix(calib["mtx"], calib["dist"], (w, h), 1, (w, h))
        return cv2.undistort(img, calib["mtx"], calib["dist"], None, new_mtx)

    @staticmethod
    def _build_remap_maps(calib: Any, size: Tuple[int, int]) -> Tuple[np.ndarray, np.ndarray]:
        h, w = size
        new_mtx, _ = cv2.getOptimalNewCameraMatrix(calib["mtx"], calib["dist"], (w, h), 1, (w, h))
        return cv2.initUndistortRectifyMap(calib["mtx"], calib["dist"], None, new_mtx, (w, h), cv2.CV_32FC1)

    def _run_stitcher(self, left: np.ndarray, right: np.ndarray, label: str) -> Optional[np.ndarray]:
        in_w = left.shape[1]
        for seed in (0, 42):
            cv2.setRNGSeed(seed)
            for mode, name in [(cv2.Stitcher_PANORAMA, "PANORAMA"), (cv2.Stitcher_SCANS, "SCANS")]:
                stitcher = cv2.Stitcher.create(mode)
                stitcher.setRegistrationResol(self._registration_resol)
                stitcher.setSeamEstimationResol(self._seam_resol)
                stitcher.setCompositingResol(-1)
                stitcher.setPanoConfidenceThresh(self._pano_confidence)
                status, pano = stitcher.stitch([left, right])
                if status == cv2.Stitcher_OK and pano.shape[1] >= in_w:
                    log.info(f"[{label}] {name}: {pano.shape[1]}x{pano.shape[0]} (seed {seed})")
                    return pano
        return None


class VideoStitcher:
    """Panorama stitching for video frame pairs."""

    def __init__(self, calib_dir: Optional[Path] = None, *, mode: str = "quality",
                 blend_px: int = 300, output_width: int = 3840):
        if mode not in ("quality", "fast"):
            raise ValueError(f"mode must be 'quality' or 'fast', got '{mode}'")
        self._mode = mode
        self._stills = Stitcher(calib_dir=calib_dir)
        self._blend_px = blend_px
        self._output_width = output_width
        self._output_shape: Optional[Tuple[int, int]] = None
        self._out_buffer: Optional[np.ndarray] = None

        self._ref_pano: Optional[np.ndarray] = None
        self._map0: Optional[Tuple[np.ndarray, np.ndarray]] = None
        self._map1: Optional[Tuple[np.ndarray, np.ndarray]] = None
        self._w0_color: Optional[np.ndarray] = None
        self._w1_color: Optional[np.ndarray] = None
        self._roi0: Optional[Tuple[int, int, int, int]] = None
        self._roi1: Optional[Tuple[int, int, int, int]] = None
        self._mask0: Optional[np.ndarray] = None
        self._mask1: Optional[np.ndarray] = None
        self._overlap_roi: Optional[Tuple[int, int, int, int]] = None
        self._overlap_c0: Optional[Tuple[int, int, int, int]] = None
        self._overlap_c1: Optional[Tuple[int, int, int, int]] = None

        self._dx: float = 0.0
        self._dy: float = 0.0

    def calibrate(self, left: np.ndarray, right: np.ndarray) -> bool:
        # One method: the spherical quality path.  Saved camera params are
        # reused across sessions (the cameras are static); they are verified
        # against the frames and re-estimated if the cameras moved.  If the
        # estimate fails, calibrate fails — no fallbacks.
        return self._calibrate_quality(left, right)

    def stitch(self, left: np.ndarray, right: np.ndarray) -> np.ndarray:
        if self._output_shape is None:
            raise RuntimeError("Call calibrate() before stitch()")
        return self._stitch_quality(left, right)

    @property
    def output_shape(self) -> Optional[Tuple[int, int]]:
        return self._output_shape

    @property
    def reference_pano(self) -> Optional[np.ndarray]:
        return self._ref_pano

    def _calibrate_quality(self, left: np.ndarray, right: np.ndarray) -> bool:
        """Estimate the camera geometry fresh from these frames.

        The cameras are calibrated on every recording — no persisted state,
        no fallbacks.  The estimator needs a matchable scene and roughly
        20%+ overlap; otherwise calibration fails and the stitch is refused.
        """
        left, right = self._stills._undistort_pair(left, right)
        full_w = left.shape[1]

        stitcher = None
        cameras = None
        work_scale = None

        for seed in (0, 42):
            cv2.setRNGSeed(seed)
            for mode in (cv2.Stitcher_PANORAMA, cv2.Stitcher_SCANS):
                s = cv2.Stitcher.create(mode)
                s.setRegistrationResol(self._stills._registration_resol)
                s.setSeamEstimationResol(self._stills._seam_resol)
                s.setCompositingResol(-1)
                s.setPanoConfidenceThresh(self._stills._pano_confidence)

                if s.estimateTransform([self._stills._enhance(left), self._stills._enhance(right)]) == cv2.Stitcher_OK:
                    stitcher = s
                    cameras = s.cameras()
                    break
            if cameras is not None:
                work_scale = stitcher.workScale()
                focals = [cam.focal / work_scale for cam in cameras]
                if all(0.3 * full_w < f < 3.0 * full_w for f in focals):
                    break
                stitcher = None
                cameras = None

        if stitcher is None:
            return False

        status, pano = stitcher.composePanorama([left, right])
        if status == cv2.Stitcher_OK:
            self._ref_pano = pano

        return self._build_quality_maps(left, right, cameras, work_scale)

    def _build_quality_maps(self, left: np.ndarray, right: np.ndarray, cameras: List[Any], work_scale: float) -> bool:
        full_size = (left.shape[1], left.shape[0])
        cam0, cam1 = cameras

        undist_maps = [
            self._stills._build_remap_maps(self._stills._calib[key], (full_size[1], full_size[0]))
            if key in self._stills._calib else None for key in ("cam0", "cam1")
        ]

        w_scale = float(np.median([cam.focal for cam in cameras])) / work_scale
        b0 = _spherical_bounds(cam0, work_scale, full_size, w_scale)
        b1 = _spherical_bounds(cam1, work_scale, full_size, w_scale)

        min_x = min(b0[0], b1[0])
        max_x = max(b0[0] + b0[2], b1[0] + b1[2])
        min_y = min(b0[1], b1[1])
        max_y = max(b0[1] + b0[3], b1[1] + b1[3])
        nat_cw, nat_ch = max_x - min_x, max_y - min_y

        k = self._output_width / nat_cw if (self._output_width and self._output_width < nat_cw) else 1.0
        out_w = int(np.ceil(nat_cw * k)) // 2 * 2
        out_h = int(np.ceil(nat_ch * k)) // 2 * 2

        map0 = _spherical_maps(cam0, work_scale, full_size, k, undist_maps[0], w_scale)
        map1 = _spherical_maps(cam1, work_scale, full_size, k, undist_maps[1], w_scale)

        self._map0 = cv2.convertMaps(map0[0], map0[1], cv2.CV_16SC2)
        self._map1 = cv2.convertMaps(map1[0], map1[1], cv2.CV_16SC2)

        off0 = (int(round((b0[0] - min_x) * k)), int(round((b0[1] - min_y) * k)))
        off1 = (int(round((b1[0] - min_x) * k)), int(round((b1[1] - min_y) * k)))

        mh0, mw0 = map0[0].shape[:2]
        mh1, mw1 = map1[0].shape[:2]
        out_w = max(out_w, off0[0] + mw0, off1[0] + mw1)
        out_h = max(out_h, off0[1] + mh0, off1[1] + mh1)
        out_w += out_w % 2
        out_h += out_h % 2

        self._roi0 = (off0[1], off0[1] + mh0, off0[0], off0[0] + mw0)
        self._roi1 = (off1[1], off1[1] + mh1, off1[0], off1[0] + mw1)
        self._output_shape = (out_h, out_w)
        self._out_buffer = np.zeros((out_h, out_w, 3), dtype=np.uint8)

        self._mask0 = (map0[0] >= 0).astype(np.uint8) * 255
        self._mask1 = (map1[0] >= 0).astype(np.uint8) * 255

        oy0, oy1 = max(self._roi0[0], self._roi1[0]), min(self._roi0[1], self._roi1[1])
        ox0, ox1 = max(self._roi0[2], self._roi1[2]), min(self._roi0[3], self._roi1[3])
        self._overlap_roi = (oy0, oy1, ox0, ox1)

        self._overlap_c0 = (oy0 - self._roi0[0], oy1 - self._roi0[0], ox0 - self._roi0[2], ox1 - self._roi0[2])
        self._overlap_c1 = (oy0 - self._roi1[0], oy1 - self._roi1[0], ox0 - self._roi1[2], ox1 - self._roi1[2])

        mask0_ol = self._mask0[self._overlap_c0[0]:self._overlap_c0[1], self._overlap_c0[2]:self._overlap_c0[3]] > 0
        mask1_ol = self._mask1[self._overlap_c1[0]:self._overlap_c1[1], self._overlap_c1[2]:self._overlap_c1[3]] > 0

        w0, w1 = _build_uint8_weights(mask0_ol, mask1_ol, max(30, int(round(self._blend_px * k))))
        self._w0_color = np.repeat(w0[:, :, np.newaxis], 3, axis=2).astype(np.float32) / 255.0
        self._w1_color = np.repeat(w1[:, :, np.newaxis], 3, axis=2).astype(np.float32) / 255.0

        self._dx = b1[0] - b0[0]
        self._dy = b1[1] - b0[1]
        return True

    def _stitch_quality(self, left: np.ndarray, right: np.ndarray) -> np.ndarray:
        warp0 = cv2.remap(left, self._map0[0], self._map0[1], cv2.INTER_LINEAR)
        warp1 = cv2.remap(right, self._map1[0], self._map1[1], cv2.INTER_LINEAR)

        r0 = self._roi0
        cv2.copyTo(warp0, self._mask0, self._out_buffer[r0[0]:r0[1], r0[2]:r0[3]])
        r1 = self._roi1
        cv2.copyTo(warp1, self._mask1, self._out_buffer[r1[0]:r1[1], r1[2]:r1[3]])

        c0, c1 = self._overlap_c0, self._overlap_c1
        ol0 = warp0[c0[0]:c0[1], c0[2]:c0[3]].astype(np.float32)
        ol1 = warp1[c1[0]:c1[1], c1[2]:c1[3]].astype(np.float32)

        blended = (ol0 * self._w0_color + ol1 * self._w1_color).astype(np.uint8)
        ol_roi = self._overlap_roi
        self._out_buffer[ol_roi[0]:ol_roi[1], ol_roi[2]:ol_roi[3]] = blended
        return self._out_buffer


def _camera_matrices(cam: Any, work_scale: float) -> Tuple[np.ndarray, np.ndarray]:
    f = cam.focal / work_scale
    cx = cam.ppx / work_scale
    cy = cam.ppy / work_scale
    aspect = getattr(cam, "aspect", 1.0)
    k = np.array([[f, 0, cx], [0, f * aspect, cy], [0, 0, 1]], dtype=np.float64)
    r = np.asarray(cam.R, dtype=np.float64).reshape(3, 3)
    return k, r


def _spherical_bounds(cam: Any, work_scale: float, full_size: Tuple[int, int], warp_scale: float) -> Tuple[int, int, int, int]:
    k, r = _camera_matrices(cam, work_scale)
    r_kinv = r @ np.linalg.inv(k)
    fw, fh = full_size

    xs = np.concatenate([np.arange(fw, dtype=np.float64), np.arange(fw, dtype=np.float64),
                         np.zeros(fh, dtype=np.float64), np.full(fh, fw - 1, dtype=np.float64)])
    ys = np.concatenate([np.zeros(fw, dtype=np.float64), np.full(fw, fh - 1, dtype=np.float64),
                         np.arange(fh, dtype=np.float64), np.arange(fh, dtype=np.float64)])

    p = r_kinv @ np.stack([xs, ys, np.ones_like(xs)])
    x, y, z = p[0], p[1], p[2]
    u = warp_scale * np.arctan2(x, z)
    n = np.sqrt(x * x + y * y + z * z)
    v = warp_scale * (np.pi - np.arccos(np.clip(y / n, -1.0, 1.0)))

    tl_u, tl_v = float(u.min()), float(v.min())
    br_u, br_v = float(u.max()), float(v.max())

    r_inv = r.T
    for sgn in (+1.0, -1.0):
        vx = r_inv[0, 1]
        vy = sgn * r_inv[1, 1]
        vz = r_inv[2, 1]
        if vy > 0:
            x_ = (k[0, 0] * vx + k[0, 1] * vy) / vz + k[0, 2]
            y_ = k[1, 1] * vy / vz + k[1, 2]
            if 0 < x_ < fw and 0 < y_ < fh:
                pole_v = float(np.pi * warp_scale) if sgn > 0 else 0.0
                tl_u = min(tl_u, 0.0)
                tl_v = min(tl_v, pole_v)
                br_u = max(br_u, 0.0)
                br_v = max(br_v, pole_v)

    tl_x, tl_y = int(tl_u), int(tl_v)
    br_x, br_y = int(br_u), int(br_v)
    return tl_x, tl_y, br_x - tl_x + 1, br_y - tl_y + 1


def _spherical_maps(cam: Any, work_scale: float, full_size: Tuple[int, int], scale_k: float,
                    undist_maps: Optional[Tuple[np.ndarray, np.ndarray]], warp_scale: float) -> Tuple[np.ndarray, np.ndarray]:
    k_mtx, r_mtx = _camera_matrices(cam, work_scale)
    k_rinv = k_mtx @ r_mtx.T
    tl_x, tl_y, nat_w, nat_h = _spherical_bounds(cam, work_scale, full_size, warp_scale)

    tw, th = int(round(nat_w * scale_k)) // 2 * 2, int(round(nat_h * scale_k)) // 2 * 2

    cols = (np.arange(tw, dtype=np.float64) / scale_k) + tl_x
    rows = (np.arange(th, dtype=np.float64) / scale_k) + tl_y
    u, v = np.meshgrid(cols, rows)
    u /= warp_scale
    v /= warp_scale

    sin_v = np.sin(np.pi - v)
    x_ = sin_v * np.sin(u)
    y_ = np.cos(np.pi - v)
    z_ = sin_v * np.cos(u)

    x = k_rinv[0, 0] * x_ + k_rinv[0, 1] * y_ + k_rinv[0, 2] * z_
    y = k_rinv[1, 0] * x_ + k_rinv[1, 1] * y_ + k_rinv[1, 2] * z_
    z = k_rinv[2, 0] * x_ + k_rinv[2, 1] * y_ + k_rinv[2, 2] * z_

    valid = z > 0
    xmap = np.where(valid, x / np.maximum(z, 1e-12), -1).astype(np.float32)
    ymap = np.where(valid, y / np.maximum(z, 1e-12), -1).astype(np.float32)

    if undist_maps is not None:
        # Compose with undistortion: the spherical maps point into
        # undistorted image space; undist_map1/2 are indexed by undistorted
        # coords and return raw coords.  Save the spherical coords before
        # the first remap overwrites them.
        um1, um2 = undist_maps
        und_x = xmap
        und_y = ymap
        xmap = cv2.remap(um1, und_x, und_y, cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=-1)
        ymap = cv2.remap(um2, und_x, und_y, cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=-1)

    return xmap, ymap


def _build_uint8_weights(mask0: np.ndarray, mask1: np.ndarray, blend_px: int) -> Tuple[np.ndarray, np.ndarray]:
    ph, pw = mask0.shape
    w0 = np.zeros((ph, pw), dtype=np.uint8)
    w1 = np.zeros((ph, pw), dtype=np.uint8)

    w0[mask0 & ~mask1] = 255
    w1[mask1 & ~mask0] = 255

    overlap = mask0 & mask1
    if blend_px > 0 and overlap.any():
        for y in range(ph):
            cols = np.where(overlap[y])[0]
            if len(cols) < 20:
                continue
            ramp_w = min(blend_px, len(cols))
            ramp_start = cols[len(cols) // 2 - ramp_w // 2]
            ramp = np.linspace(255.0, 0.0, ramp_w)

            for i, x in enumerate(range(ramp_start, ramp_start + ramp_w)):
                w0[y, x] = int(ramp[i])
                w1[y, x] = 255 - int(ramp[i])

            w0[y, cols[cols < ramp_start]] = 255
            w1[y, cols[cols >= ramp_start + ramp_w]] = 255

    return w0, w1


def _ncc_fallback(img0: np.ndarray, img1: np.ndarray, enhance_fn: Callable) -> Optional[Tuple[float, float]]:
    """Dense NCC search for weak-texture scenes where SIFT matching fails.

    Pyramid search: coarse sweep at ~1/8 resolution, then two refinement
    levels that zoom in around the best offset.  Returns the full-resolution
    offset of the best match.
    """

    def _strips(g0, g1, dx, dy):
        ow = g0.shape[1] - dx
        if dy >= 0:
            a = g0[dy:, dx:]
            b = g1[:g0.shape[0] - dy, :ow]
        else:
            a = g0[:g0.shape[0] + dy, dx:]
            b = g1[-dy:, :ow]
        if a.shape[0] < 40 or a.shape[0] != b.shape[0]:
            return None
        a = a - a.mean()
        b = b - b.mean()
        return float((a * b).sum()
                     / max(np.sqrt((a * a).sum() * (b * b).sum()), 1e-9))

    def _gray(img, width):
        # Plain grayscale — CLAHE helps SIFT features but hurts NCC
        # correlation on weak-texture scenes.
        h = int(img.shape[0] * width / img.shape[1]) // 2 * 2
        return cv2.cvtColor(
            cv2.resize(img, (width, h), interpolation=cv2.INTER_AREA),
            cv2.COLOR_BGR2GRAY).astype(np.float32)

    best_dx = best_dy = 0
    best_score = -1.0
    # Level 0: coarse sweep at 480 wide, step 4
    g0 = _gray(img0, 480)
    g1 = _gray(img1, 480)
    for dx in range(g0.shape[1] // 8, g0.shape[1] - g0.shape[1] // 10, 4):
        for dy in range(-g0.shape[0] // 5, g0.shape[0] // 5 + 1, 4):
            score = _strips(g0, g1, dx, dy)
            if score is not None and score > best_score:
                best_dx, best_dy, best_score = dx, dy, score

    # Level 1: refine at 960 wide, step 2, ±16 around the coarse peak
    g0 = _gray(img0, 960)
    g1 = _gray(img1, 960)
    c_dx, c_dy = best_dx * 2, best_dy * 2
    best_dx, best_dy, best_score = c_dx, c_dy, -1.0
    for dx in range(c_dx - 16, c_dx + 17, 2):
        for dy in range(c_dy - 16, c_dy + 17, 2):
            score = _strips(g0, g1, dx, dy)
            if score is not None and score > best_score:
                best_dx, best_dy, best_score = dx, dy, score

    # Level 2: refine at 1920 wide, step 1, ±6 around the refined peak
    g0 = _gray(img0, 1920)
    g1 = _gray(img1, 1920)
    c_dx, c_dy = best_dx * 2, best_dy * 2
    best_dx, best_dy, best_score = c_dx, c_dy, -1.0
    for dx in range(c_dx - 6, c_dx + 7):
        for dy in range(c_dy - 6, c_dy + 7):
            score = _strips(g0, g1, dx, dy)
            if score is not None and score > best_score:
                best_dx, best_dy, best_score = dx, dy, score

    full_dx = best_dx * (img0.shape[1] / 1920)
    full_dy = best_dy * (img0.shape[1] / 1920)
    if best_score < 0.4:
        return None
    log.info("translation fallback: dx=%d dy=%d (NCC=%.3f)",
             int(full_dx), int(full_dy), best_score)
    return float(full_dx), float(full_dy)


def _find_translation(img0: np.ndarray, img1: np.ndarray, enhance_fn: Callable, scale: float = 0.5) -> Optional[Tuple[float, float]]:
    s0 = cv2.resize(img0, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    s1 = cv2.resize(img1, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)

    gray0 = cv2.cvtColor(enhance_fn(s0), cv2.COLOR_BGR2GRAY)
    gray1 = cv2.cvtColor(enhance_fn(s1), cv2.COLOR_BGR2GRAY)

    sift = cv2.SIFT_create(nfeatures=4000, contrastThreshold=0.03)
    kp0, des0 = sift.detectAndCompute(gray0, None)
    kp1, des1 = sift.detectAndCompute(gray1, None)

    if des0 is None or des1 is None or len(kp0) < 10 or len(kp1) < 10:
        return _ncc_fallback(img0, img1, enhance_fn)

    flann = cv2.FlannBasedMatcher(dict(algorithm=1, trees=8), dict(checks=80))
    matches = flann.knnMatch(des0, des1, k=2)
    good = [pair[0] for pair in matches if len(pair) == 2 and pair[0].distance < 0.7 * pair[1].distance]

    if len(good) < 20:
        return _ncc_fallback(img0, img1, enhance_fn)

    pts0 = np.float32([kp0[m.queryIdx].pt for m in good]) / scale
    pts1 = np.float32([kp1[m.trainIdx].pt for m in good]) / scale

    dx = pts0[:, 0] - pts1[:, 0]
    dy = pts0[:, 1] - pts1[:, 1]
    med_dx, med_dy = np.median(dx), np.median(dy)
    mad_dx = np.median(np.abs(dx - med_dx))

    mask = (np.abs(dx - med_dx) < max(3 * mad_dx, 100)) & (np.abs(dy - med_dy) < 40)
    if mask.sum() < 10:
        return _ncc_fallback(img0, img1, enhance_fn)

    return float(np.median(dx[mask])), float(np.median(dy[mask]))


def stitch_pair(left: np.ndarray, right: np.ndarray, calib_dir: Optional[Path] = None) -> Optional[np.ndarray]:
    """One-shot still-image stitch."""
    return Stitcher(calib_dir=calib_dir).stitch(left, right)


class StitchError(RuntimeError):
    """Raised when session stitching fails (alignment, encoding, IO)."""


class _FFmpegVAAPIEncoder:
    """Intel VAAPI H.264 encode (Linux) via an ffmpeg pipe."""

    def __init__(self, out_path: Path, fps: int, width: int, height: int, qp: int = 25):
        self._out_path = out_path
        self._proc = subprocess.Popen([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-vaapi_device", "/dev/dri/renderD128",
            "-f", "rawvideo", "-pix_fmt", "bgr24",
            "-s", f"{width}x{height}", "-framerate", str(fps),
            "-i", "pipe:0", "-vf", "format=nv12,hwupload",
            "-c:v", "h264_vaapi", "-qp", str(qp), str(out_path)
        ], stdin=subprocess.PIPE, stderr=subprocess.PIPE)

    def write(self, frame_bgr: np.ndarray) -> None:
        if self._proc.stdin:
            try:
                self._proc.stdin.write(frame_bgr.tobytes())
            except BrokenPipeError:
                pass

    def close(self) -> Path:
        if self._proc.stdin:
            self._proc.stdin.close()
        rc = self._proc.wait()
        if rc != 0:
            detail = self._proc.stderr.read().decode(errors="replace").strip()
            raise RuntimeError(f"ffmpeg VAAPI encoder exited with code {rc}: {detail}")
        return self._out_path


class _FFmpegVideoToolboxEncoder:
    """Apple VideoToolbox H.264 encode (macOS, M-series hardware) via an ffmpeg pipe."""

    def __init__(self, out_path: Path, fps: int, width: int, height: int, bitrate: str = "30M"):
        self._out_path = out_path
        self._proc = subprocess.Popen([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "rawvideo", "-pix_fmt", "bgr24",
            "-s", f"{width}x{height}", "-framerate", str(fps),
            "-i", "pipe:0", "-vf", "format=nv12",
            "-c:v", "h264_videotoolbox", "-b:v", bitrate, str(out_path)
        ], stdin=subprocess.PIPE, stderr=subprocess.PIPE)

    def write(self, frame_bgr: np.ndarray) -> None:
        if self._proc.stdin:
            try:
                self._proc.stdin.write(frame_bgr.tobytes())
            except BrokenPipeError:
                pass

    def close(self) -> Path:
        if self._proc.stdin:
            self._proc.stdin.close()
        rc = self._proc.wait()
        if rc != 0:
            detail = self._proc.stderr.read().decode(errors="replace").strip()
            raise RuntimeError(f"ffmpeg VideoToolbox encoder exited with code {rc}: {detail}")
        return self._out_path


class _FFmpegMPPEncoder:
    """Rockchip MPP H.264 encode (RK3588) via the ffmpeg-rockchip build."""

    def __init__(self, out_path: Path, fps: int, width: int, height: int, bitrate: str = "30M"):
        self._out_path = out_path
        # The fork's default pix-fmt negotiation picks nv16 and stalls on a
        # software 422 conversion — force nv12 (see orange_pi_5_max_setup.md §5).
        self._proc = subprocess.Popen([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "rawvideo", "-pix_fmt", "bgr24",
            "-s", f"{width}x{height}", "-framerate", str(fps),
            "-i", "pipe:0", "-vf", "format=nv12",
            "-c:v", "h264_rkmpp", "-b:v", bitrate, str(out_path)
        ], stdin=subprocess.PIPE, stderr=subprocess.PIPE)

    def write(self, frame_bgr: np.ndarray) -> None:
        if self._proc.stdin:
            try:
                self._proc.stdin.write(frame_bgr.tobytes())
            except BrokenPipeError:
                pass

    def close(self) -> Path:
        if self._proc.stdin:
            self._proc.stdin.close()
        rc = self._proc.wait()
        if rc != 0:
            detail = self._proc.stderr.read().decode(errors="replace").strip()
            raise RuntimeError(f"ffmpeg MPP encoder exited with code {rc}: {detail}")
        return self._out_path


def _open_encoder(out_path: Path, fps: int, width: int, height: int):
    """One hardware encoder per OS/hardware.

    RK3588 → h264_rkmpp (rockchip ffmpeg build), Intel Linux → VAAPI,
    macOS → VideoToolbox.
    """
    import shutil
    if shutil.which("ffmpeg") is None:
        raise StitchError(
            "ffmpeg not found — install it (sudo apt install ffmpeg, "
            "or brew install ffmpeg on macOS)")
    if sys.platform == "darwin":
        return _FFmpegVideoToolboxEncoder(out_path, fps, width, height)
    if Path("/dev/mpp_service").exists():
        return _FFmpegMPPEncoder(out_path, fps, width, height)
    return _FFmpegVAAPIEncoder(out_path, fps, width, height)


def _detect_clap(wav_path: Path) -> Optional[float]:
    """Find the first sharp transient (clap) in a WAV. Returns seconds or None."""
    import wave

    try:
        with wave.open(str(wav_path), "rb") as wf:
            rate = wf.getframerate()
            nch = wf.getnchannels()
            n = wf.getnframes()
            raw = wf.readframes(n)
    except (wave.Error, EOFError, OSError):
        return None
    if n < rate // 4:
        return None

    data = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
    if data.size == 0:
        return None
    mono = data.reshape(-1, nch).mean(axis=1) if nch > 1 else data
    # High-frequency emphasis: abs frame difference
    diff = np.abs(np.diff(mono))
    win = max(rate // 200, 16)  # 5 ms windows
    rms = np.convolve(diff, np.ones(win, dtype=np.float32) / win, mode="valid")
    noise = float(np.median(rms))
    threshold = max(12.0 * noise, 500.0)
    over = rms > threshold
    # Require a SUSTAINED transient (>= 3 consecutive windows) to reject
    # isolated noise spikes, and skip the first 0.25 s of startup noise.
    start_idx = int(0.25 * rate)
    for i in range(start_idx, len(over) - 2):
        if over[i] and over[i + 1] and over[i + 2]:
            return float(i / rate)
    return None


def _audio_frame_lag(wav0: Path, wav1: Path, fps: int) -> Optional[int]:
    """Frame offset between two recordings from their clap timestamps.

    Returns the number of frames to skip in cam1 so that cam0[i] pairs with
    cam1[i + offset], or None if either clap is not detectable.
    """
    t0 = _detect_clap(wav0)
    t1 = _detect_clap(wav1)
    if t0 is None or t1 is None:
        return None
    lag_frames = int(round((t1 - t0) * fps))
    log.info("clap sync: cam0 clap at %.3fs, cam1 at %.3fs → offset %+d frames",
             t0, t1, lag_frames)
    return lag_frames


def _moving_average(a: np.ndarray, n: int) -> np.ndarray:
    ret = np.cumsum(a, axis=0, dtype=float)
    ret[n:] = ret[n:] - ret[:-n]
    return ret[n - 1:] / n


def _estimate_frame_lag(cap0, cap1, overlap_dx: int, max_lag: int = 45) -> Tuple[int, float]:
    w, h = 256, 144
    sx0 = int(overlap_dx * w / 3840)
    sw = w - sx0
    if sw < 20:
        return 0, 0.0

    def _signature(cap, x0: int, x1: int) -> np.ndarray:
        cap.seek_to(0)
        sig = []
        prev = None
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            g = cv2.cvtColor(cv2.resize(frame, (w, h), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2GRAY)[:, x0:x1]
            bright = float(g.mean())
            motion = float(np.abs(g.astype(np.float32) - prev).mean()) if prev is not None else 0.0
            sig.append((bright, motion))
            prev = g.astype(np.float32)
        return np.asarray(sig, dtype=np.float32)

    s0 = _signature(cap0, sx0, w)
    s1 = _signature(cap1, 0, sw)
    n = min(len(s0), len(s1))
    if n < 10:
        return 0, 0.0

    s0, s1 = s0[:n], s1[:n]
    for s in (s0, s1):
        # _moving_average is a trailing mean of the last 15 samples;
        # ma[j] corresponds to s[j + 14].
        ma = _moving_average(s, 15)
        s[14:] -= ma
        s -= s.mean(axis=0)
        s /= (s.std(axis=0) + 1e-6)

    best_lag, best_score = 0, -1.0
    for lag in range(-max_lag, max_lag + 1):
        a, b = (s0[:n - lag], s1[lag:]) if lag >= 0 else (s0[-lag:], s1[:n + lag])
        if len(a) < 10:
            continue
        score = float((a * b).sum() / len(a))
        if score > best_score:
            best_lag, best_score = lag, score

    return best_lag, best_score


class _CVFrameReader:
    """Uniform reader interface over cv2.VideoCapture (AVI/MJPEG recordings)."""

    def __init__(self, path: Path):
        self._cap = cv2.VideoCapture(str(path))

    def read(self):
        return self._cap.read()

    def seek_to(self, n: int):
        self._cap.set(cv2.CAP_PROP_POS_FRAMES, n)

    def release(self):
        self._cap.release()


class _H264FrameReader:
    """Decode a raw .h264 elementary stream into BGR frames via an ffmpeg pipe.

    Raw ES has no container index, so seek_to(n) restarts the decoder and
    skips n frames — fine for the small clap-sync offsets used here.
    """

    def __init__(self, path: Path):
        self.path = path
        self.width, self.height = self._probe()
        self._frame_bytes = self.width * self.height * 3
        self._proc = None
        self._start(0)

    def _probe(self):
        import subprocess as sp
        try:
            out = sp.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=width,height", "-of", "csv=p=0",
                 str(self.path)],
                capture_output=True, text=True, timeout=15,
            ).stdout.strip().split(",")
            return int(out[0]), int(out[1])
        except (ValueError, IndexError, sp.TimeoutExpired):
            raise StitchError(f"Cannot probe {self.path} — not a decodable video")

    def _stop_proc(self):
        """Stop the decoder process.

        Close the read end first — ffmpeg may be blocked writing into the
        full pipe, and a SIGTERM alone doesn't interrupt that write.
        """
        if not self._proc:
            return
        try:
            self._proc.stdout.close()
        except Exception:
            pass
        if self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait()

    def _start(self, skip: int):
        self._stop_proc()
        self._proc = subprocess.Popen(
            ["ffmpeg", "-hide_banner", "-loglevel", "error",
             "-i", str(self.path), "-f", "rawvideo", "-pix_fmt", "bgr24",
             "pipe:1"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        for _ in range(skip):
            if not self.read()[0]:
                break

    def read(self):
        buf = self._proc.stdout.read(self._frame_bytes)
        if len(buf) < self._frame_bytes:
            return False, None
        return True, np.frombuffer(buf, dtype=np.uint8).reshape(
            self.height, self.width, 3)

    def seek_to(self, n: int):
        self._start(n)

    def release(self):
        self._stop_proc()
        self._proc = None


def _open_video_reader(path: Path):
    """Reader for a recording: ffmpeg pipe for .mp4/.h264, OpenCV for .avi."""
    if path.suffix.lower() in (".h264", ".mp4"):
        return _H264FrameReader(path)
    return _CVFrameReader(path)


def stitch_video_files(cam0_path: Path, cam1_path: Path, out_dir: Path,
                       fps: int = 30, mode: str = "quality",
                       output_width: int = 3840,
                       progress: Optional[Callable[[str], None]] = None) -> dict:
    """Stitch a recorded cam0/cam1 pair (.h264 native or .avi legacy) into out_dir."""

    def _report(msg: str):
        log.info(f"stitch: {msg}")
        if progress:
            progress(msg)

    cap0 = _open_video_reader(cam0_path)
    cap1 = _open_video_reader(cam1_path)

    ok0, f0_raw = cap0.read()
    ok1, f1_raw = cap1.read()
    if not ok0 or not ok1:
        raise StitchError("Could not read first frames from recordings.")

    advance1 = 0
    # Prefer clap-based sync when audio tracks exist — the clap transient is
    # a millisecond-precise marker shared by both recordings.
    wav0 = out_dir / "cam0.wav"
    wav1 = out_dir / "cam1.wav"
    if wav0.exists() and wav1.exists():
        audio_lag = _audio_frame_lag(wav0, wav1, fps)
        if audio_lag is not None and abs(audio_lag) <= 90:
            advance1 = audio_lag
            _report(f"frame sync: clap offset {advance1:+d} frames")
    if advance1 == 0:
        offset = _find_translation(f0_raw, f1_raw, Stitcher()._enhance)
        if offset is not None:
            overlap_dx = int(round(offset[0]))
            if overlap_dx > 200:
                advance1, lag_score = _estimate_frame_lag(cap0, cap1, overlap_dx)
                if lag_score > 0.3:
                    _report(f"frame sync: cam1 offset {advance1:+d} frames (corr {lag_score:.2f})")
                else:
                    _report("frame sync: unreliable, assuming 0 offset")
                    advance1 = 0
        else:
            _report("frame sync: cannot locate overlap, assuming 0 offset")

    cap0.seek_to(max(0, -advance1))
    cap1.seek_to(max(0, advance1))

    ok0, f0 = cap0.read()
    ok1, f1 = cap1.read()
    if not ok0 or not ok1:
        raise StitchError("Could not read synchronised frames.")

    cv2.imwrite(str(out_dir / "cam0.jpg"), f0, [cv2.IMWRITE_JPEG_QUALITY, 92])
    cv2.imwrite(str(out_dir / "cam1.jpg"), f1, [cv2.IMWRITE_JPEG_QUALITY, 92])

    _report("computing alignment")
    vs = VideoStitcher(mode=mode, output_width=output_width)

    if not vs.calibrate(f0, f1):
        offset = _find_translation(f0, f1, Stitcher()._enhance)
        if offset is not None:
            overlap_pct = (f0.shape[1] - offset[0]) / f0.shape[1] * 100
            raise StitchError(
                f"Calibration failed - camera overlap is only {overlap_pct:.0f}%. "
                "Aim the cameras so they overlap by ~25-30% "
                "(run 'afl-cam check-aim' to see the current value).")
        raise StitchError(
            "Calibration failed - the scene has too few matchable features. "
            "Aim the cameras at a textured scene with good lighting and "
            "~25-30% overlap.")

    if vs.reference_pano is not None:
        cv2.imwrite(str(out_dir / "panorama.jpg"), vs.reference_pano, [cv2.IMWRITE_JPEG_QUALITY, 92])

    oh, ow = vs.output_shape
    if ow > output_width:
        scale = output_width / ow
        oh = int(round(oh * scale)) // 2 * 2
        ow = output_width

    _report(f"encoding {ow}x{oh}")
    enc = _open_encoder(out_dir / "stitch_video.mp4", fps, ow, oh)

    idx, t0 = 0, time.time()
    last_report = 0.0

    while True:
        ok0, frame0 = cap0.read()
        ok1, frame1 = cap1.read()
        if not ok0 or not ok1:
            break

        pano = vs.stitch(frame0, frame1)
        if pano.shape[:2] != (oh, ow):
            pano = cv2.resize(pano, (ow, oh))

        enc.write(pano)
        idx += 1

        if time.time() - last_report > 2.5:
            _report(f"frame {idx} ({idx / (time.time() - t0):.1f} fps)")
            last_report = time.time()

    elapsed = time.time() - t0
    cap0.release()
    cap1.release()
    out_path = enc.close()

    if idx == 0:
        raise StitchError("No frames written. Recordings may be empty.")

    return {
        "video_path": out_path,
        "frame_count": idx,
        "elapsed": elapsed,
        "output_shape": (ow, oh),
        "frame_lag": advance1,
    }
