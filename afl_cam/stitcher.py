"""Panorama stitching for side-by-side 4K camera pairs."""

import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple, Any

import cv2
import numpy as np

from . import stitch_maps
from .pairing import pair_frames

log = logging.getLogger(__name__)

_PKG_DIR = Path(__file__).resolve().parent
_PROJECT_DIR = _PKG_DIR.parent
_CALIB_DIR = _PROJECT_DIR / "calibration"
_OUTPUTS_DIR = _PROJECT_DIR / "outputs"
# Frozen live-stitch map set (measured K + refined R from the re-align
# workflow, written by dump_camera_params.py and read by cam2stitch). The rig
# is static, so re-stitching uses these exact maps + weights instead of
# re-bundling per session — the 2-view bundle is degenerate on feature-poor
# scenes (focal/R entangled) and leaves 10-30px ghosting in the overlap.
_LIVE_MAPS_DIR = _CALIB_DIR / "stitch_live_full"


class Stitcher:
    """Per-camera calibration, undistortion and contrast enhancement shared
    by the video stitcher and the alignment scripts."""

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


class VideoStitcher:
    """Panorama stitching for video frame pairs."""

    def __init__(self, calib_dir: Optional[Path] = None, *, mode: str = "quality",
                 blend_px: int = 300, output_width: int = 3840,
                 maps_dir: Optional[Path] = None):
        if mode not in ("quality", "fast"):
            raise ValueError(f"mode must be 'quality' or 'fast', got '{mode}'")
        self._mode = mode
        self._stills = Stitcher(calib_dir=calib_dir)
        self._blend_px = blend_px
        self._output_width = output_width
        self._maps_dir = maps_dir if maps_dir is not None else _LIVE_MAPS_DIR
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
        """Set up the warp: the live map set when it exists, otherwise a
        per-session bundle estimate from these frames (needs a matchable
        scene and roughly 20%+ overlap, else calibration fails)."""
        # Static rig: re-stitch with the live map set itself, so a separate-
        # mode recording stitches exactly as cam2stitch would have (same
        # geometry, same weights, distortion zeroed). The per-session
        # bundle below is the fallback for a missing/torn re-align.
        live = None if os.environ.get("AFL_CAM_REBUNDLE") == "1" else stitch_maps.try_load(self._maps_dir)
        if live is not None:
            if (left.shape[1], left.shape[0]) != live.src or right.shape[:2] != left.shape[:2]:
                raise StitchError(
                    f"frames are {left.shape[1]}x{left.shape[0]} but the live maps in "
                    f"{self._maps_dir} expect {live.src[0]}x{live.src[1]} raw camera frames")
            log.info("using the live map set from %s (rig is static)", self._maps_dir)
            self._use_live_maps(live)
            self._ref_pano = self._stitch_quality(left, right).copy()
            return True

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

    def _use_live_maps(self, live: "stitch_maps.LiveMaps") -> None:
        """Set up the remap/blend state from a live map set. Outside the two
        ROIs' intersection the weights are 0/255 (dump_camera_params builds
        them that way), so blending only that intersection reproduces the
        GPU's per-pixel w0*cam0 + w1*cam1 exactly."""
        cw, ch = live.canvas
        rois, masks, maps = [], [], []
        for cam in live.cams:
            mh, mw = cam.map.shape[:2]
            x0, y0 = cam.off
            roi = (y0, y0 + mh, x0, x0 + mw)
            rois.append(roi)
            masks.append(((cam.weight[roi[0]:roi[1], roi[2]:roi[3]] > 0)
                          & (cam.map[:, :, 0] >= 0)).astype(np.uint8) * 255)
            maps.append(cv2.convertMaps(cam.map[:, :, 0], cam.map[:, :, 1], cv2.CV_16SC2))
        self._map0, self._map1 = maps
        self._roi0, self._roi1 = rois
        self._mask0, self._mask1 = masks
        self._output_shape = (ch, cw)
        self._out_buffer = np.zeros((ch, cw, 3), dtype=np.uint8)

        r0, r1 = self._roi0, self._roi1
        oy0, oy1 = max(r0[0], r1[0]), min(r0[1], r1[1])
        ox0, ox1 = max(r0[2], r1[2]), min(r0[3], r1[3])
        self._overlap_roi = (oy0, oy1, ox0, ox1)
        self._overlap_c0 = (oy0 - r0[0], oy1 - r0[0], ox0 - r0[2], ox1 - r0[2])
        self._overlap_c1 = (oy0 - r1[0], oy1 - r1[0], ox0 - r1[2], ox1 - r1[2])
        w0, w1 = (cam.weight[oy0:oy1, ox0:ox1] for cam in live.cams)
        self._w0_color = np.repeat(w0[:, :, np.newaxis], 3, axis=2).astype(np.float32) / 255.0
        self._w1_color = np.repeat(w1[:, :, np.newaxis], 3, axis=2).astype(np.float32) / 255.0

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


class StitchError(RuntimeError):
    """Raised when session stitching fails (alignment, encoding, IO)."""


class _FFmpegEncoder:
    """BGR frames → H.264 via an ffmpeg pipe: h264_rkmpp on the RK3588
    (ffmpeg-rockchip build), libx264 elsewhere (re-stitching off the board)."""

    def __init__(self, out_path: Path, fps: int, width: int, height: int, bitrate: str = "30M"):
        self._out_path = out_path
        # The rockchip fork's default pix-fmt negotiation picks nv16 and
        # stalls on a software 422 conversion — force nv12
        # (orange_pi_5_max_setup.md §5).
        codec = (["-c:v", "h264_rkmpp", "-b:v", bitrate]
                 if Path("/dev/mpp_service").exists()
                 else ["-c:v", "libx264", "-preset", "fast", "-crf", "18"])
        self._proc = subprocess.Popen([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "rawvideo", "-pix_fmt", "bgr24",
            "-s", f"{width}x{height}", "-framerate", str(fps),
            "-i", "pipe:0", "-vf", "format=nv12", *codec, str(out_path)
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
            raise RuntimeError(f"ffmpeg encoder exited with code {rc}: {detail}")
        return self._out_path


def _open_encoder(out_path: Path, fps: int, width: int, height: int):
    import shutil
    if shutil.which("ffmpeg") is None:
        raise StitchError("ffmpeg not found — install it (sudo apt install ffmpeg)")
    return _FFmpegEncoder(out_path, fps, width, height)


class _H264FrameReader:
    """Decode a raw .h264 elementary stream into BGR frames via an ffmpeg pipe.

    Raw ES has no container index, so seek_to(n) restarts the decoder and
    skips n frames (the stitcher only reads forward).
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
    """Reader for a recording (.h264 elementary stream or .mp4)."""
    if path.suffix.lower() not in (".h264", ".mp4"):
        raise StitchError(f"unsupported recording format: {path.name}")
    return _H264FrameReader(path)


def _load_pts(path: Path) -> Optional[np.ndarray]:
    """Kernel-timestamp sidecar (us, monotonic) written by cam2h264, or None."""
    try:
        with open(path) as fh:
            lines = fh.read().split()
        if len(lines) < 10:
            return None
        return np.asarray([int(x) for x in lines], dtype=np.int64)
    except (OSError, ValueError):
        return None


def stitch_video_files(cam0_path: Path, cam1_path: Path, out_dir: Path,
                       fps: int = 30, mode: str = "quality",
                       output_width: int = 3840,
                       progress: Optional[Callable[[str], None]] = None) -> dict:
    """Stitch a separate-mode cam0/cam1 recording pair into out_dir.

    Frames are paired by the .pts sidecars cam2h264 writes next to each
    recording (kernel-monotonic capture timestamps — the USB-camera
    equivalent of hardware sync), with the same nearest-of-two pairing the
    live recorder uses.
    """

    def _report(msg: str):
        log.info(f"stitch: {msg}")
        if progress:
            progress(msg)

    pts0 = _load_pts(cam0_path.with_suffix(".pts"))
    pts1 = _load_pts(cam1_path.with_suffix(".pts"))
    if pts0 is None or pts1 is None:
        raise StitchError("Missing .pts timestamp sidecars — only recordings "
                          "made by the native recorder can be stitched.")

    # pair every cam0 frame with the nearest cam1 frame, trimmed to the
    # window where BOTH cameras are live (each runs its own rate under
    # auto-exposure) — no frozen duplicated frames at either end.
    s0, e_cut, pair = pair_frames(pts0, pts1)
    n_out = max(0, e_cut - s0)
    if n_out < 1:
        raise StitchError("The two recordings do not overlap in time.")
    _report(f"frame sync: pts sidecar — trimmed {s0} frames at start "
            f"({(pts1[0] - pts0[s0]) / 1e6:+.2f}s between first frames)")
    # The output container is CFR at the MEDIAN paired rate — real-life
    # playback speed in a plain regular container.
    out_fps = fps
    if n_out > 1:
        paired_ts = (pts0[s0:e_cut] + pts1[pair[s0:e_cut]]) // 2
        out_fps = int(max(1, min(60, round(
            1e6 / float(np.median(np.diff(paired_ts)))))))
        _report(f"frame sync: output CFR at measured {out_fps} fps "
                f"({n_out} frames in the overlap window)")

    cap0 = _open_video_reader(cam0_path)
    cap1 = _open_video_reader(cam1_path)

    # skip cam0 to the overlap start; cam1 is streamed forward on demand
    for _ in range(s0):
        if not cap0.read()[0]:
            raise StitchError("cam0 recording is shorter than its .pts sidecar.")

    j_cur = -1
    cam1_last = None

    def cam1_frame(j: int):
        nonlocal j_cur, cam1_last
        while j_cur < j:
            ok1, f1 = cap1.read()
            if not ok1 or f1 is None:
                break  # cam1 ended: reuse its last frame
            cam1_last = f1
            j_cur += 1
        return cam1_last

    ok0, f0 = cap0.read()
    f1 = cam1_frame(int(pair[s0]))
    if not ok0 or f1 is None:
        raise StitchError("Could not read the first synchronised frames.")

    cv2.imwrite(str(out_dir / "cam0.jpg"), f0, [cv2.IMWRITE_JPEG_QUALITY, 92])
    cv2.imwrite(str(out_dir / "cam1.jpg"), f1, [cv2.IMWRITE_JPEG_QUALITY, 92])

    _report("computing alignment")
    vs = VideoStitcher(mode=mode, output_width=output_width)
    if not vs.calibrate(f0, f1):
        raise StitchError(
            "Calibration failed — no frozen alignment and the scene has too "
            "few matchable features. Run the re-align workflow first.")

    if vs.reference_pano is not None:
        cv2.imwrite(str(out_dir / "panorama.jpg"), vs.reference_pano, [cv2.IMWRITE_JPEG_QUALITY, 92])

    oh, ow = vs.output_shape
    if ow > output_width:
        scale = output_width / ow
        oh = int(round(oh * scale)) // 2 * 2
        ow = output_width

    _report(f"encoding {ow}x{oh}")
    enc = _open_encoder(out_dir / "stitch_video.mp4", out_fps, ow, oh)

    idx, t0 = 0, time.time()
    last_report = 0.0
    out_pts = []  # kernel timestamp of each output frame (midpoint of the pair)
    frame0 = f0
    for i in range(s0, e_cut):
        if i > s0:
            ok0, frame0 = cap0.read()
            if not ok0 or frame0 is None:
                break
        j = int(pair[i])
        frame1 = cam1_frame(j)

        pano = vs.stitch(frame0, frame1)
        if pano.shape[:2] != (oh, ow):
            pano = cv2.resize(pano, (ow, oh))
        enc.write(pano)
        idx += 1
        out_pts.append(int((pts0[i] + pts1[j]) // 2))

        if time.time() - last_report > 2.5:
            _report(f"frame {idx} ({idx / (time.time() - t0):.1f} fps)")
            last_report = time.time()

    elapsed = time.time() - t0
    cap0.release()
    cap1.release()
    out_path = enc.close()

    if idx == 0:
        raise StitchError("No frames written. Recordings may be empty.")

    # Diagnostic sidecar: the kernel timestamp of each output frame
    # (midpoint of the source pair).
    with open(out_dir / "stitch_video.pts", "w") as fh:
        fh.write("\n".join(str(t) for t in out_pts) + "\n")
    _report(f"output: {idx} frames at {out_fps} fps "
            f"({(out_pts[-1] - out_pts[0]) / 1e6:.2f}s real span)")

    return {
        "video_path": out_path,
        "frame_count": idx,
        "elapsed": elapsed,
        "output_shape": (ow, oh),
        "frame_lag": s0,
    }
