"""Per-camera recording backend for the CLI (`afl-cam record`), RK3588 only.

cam2h264 per camera (separate mode): V4L2 MJPEG → jpegd → rkvenc → raw ES
+ `.pts` sidecar, muxed to .mp4 on completion. The field daemon runs the
same binary itself (daemon.SeparateRecorder); live stitching is cam2stitch
under the daemon.
"""

import logging
import os
import time
from datetime import datetime
from pathlib import Path
from threading import Thread, Lock
from typing import Optional, Callable

from .config import (
    RESOLUTIONS, DEFAULT_RESOLUTION, PLATFORM, EXPOSURE_TIME,
    ENCODE_CODEC, NATIVE_BITRATE_KBPS, PREVIEW_INTERVAL_SECS,
)

log = logging.getLogger(__name__)


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _make_filename(camera: str, resolution_key: str) -> str:
    return f"{camera}_{_timestamp()}_{resolution_key}.mp4"


def is_rockchip() -> bool:
    """True on RK3588-class boards (vendor kernel with the MPP service node)."""
    return PLATFORM == "linux" and Path("/dev/mpp_service").exists()


def ensure_cam2h264() -> Optional[Path]:
    """Compile the native recorder if missing or older than its source.

    Returns the binary path, or None if the build fails (gcc/librockchip_mpp
    missing — see orange_pi_5_max_setup.md).
    """
    from .config import build_cam2h264

    return build_cam2h264()


class CameraRecorderMPP:
    """Native hardware recorder for a single camera (RK3588).

    Runs the cam2h264 binary: USB MJPEG capture → jpegd decode (NV12, hardware)
    → rkvenc H.264 encode (hardware) → raw .h264 elementary stream. No ffmpeg,
    no format conversions; ~0.2 cores per stream at 4K30.
    """

    codec = "H264"
    container = "mp4"

    def __init__(self, device: str, label: str = "",
                 resolution_key: str = DEFAULT_RESOLUTION):
        self.device = device
        self.label = label or device
        self.resolution_key = resolution_key
        res = RESOLUTIONS[resolution_key]
        self.width = res["width"]
        self.height = res["height"]
        self.fps = res["fps"]

        self._proc = None
        self._monitor: Optional[Thread] = None
        self._start_time: float = 0
        self._on_done: Optional[Callable] = None
        self._output_path: Optional[Path] = None
        self._raw_path: Optional[Path] = None
        self._running = False
        self._lock = Lock()



    def start(self, duration_sec: float = 0,
              on_done: Optional[Callable] = None,
              output_dir: Optional[Path] = None) -> Path:
        """Start recording; returns the output path. on_done(ok, path,
        duration) fires from a thread when the recorder exits."""
        import signal
        import subprocess

        from .config import CAPTURES_DIR

        with self._lock:
            if self._running:
                raise RuntimeError(f"Already recording: {self.label}")

            recorder = ensure_cam2h264()
            if recorder is None:
                raise RuntimeError(
                    "Native recorder unavailable — build it first "
                    "(see orange_pi_5_max_setup.md §8)")

            if output_dir is not None:
                output_dir.mkdir(parents=True, exist_ok=True)
                output = output_dir / f"{self.label}.mp4"
            else:
                output = CAPTURES_DIR / _make_filename(self.label, self.resolution_key)
                CAPTURES_DIR.mkdir(parents=True, exist_ok=True)

            # The recorder writes a raw H.264 elementary stream; it gets
            # remuxed (stream copy, no re-encode) to .mp4 on completion.
            raw = output.with_suffix(".raw.h264")

            # 0 = record until stop(); otherwise record for the requested
            # wall-clock duration — the frame count is only a 30 fps backstop
            # (auto-exposure can run the sensor slower than 30 fps, and the
            # recording must still cover duration_sec of REAL time).
            frames = int(duration_sec * self.fps) if duration_sec > 0 else 0

            log_path = output.with_suffix(".rec.log")
            cmd = [str(recorder), self.device, str(raw), str(frames),
                   str(self.width), str(self.height), str(self.fps),
                   str(EXPOSURE_TIME),
                   str(int(duration_sec)) if duration_sec > 0 else "0"]

            self._output_path = output
            self._raw_path = raw
            self._on_done = on_done
            self._start_time = time.time()
            self._running = True

            env = dict(os.environ,
                       AFL_CAM_CODEC=ENCODE_CODEC,
                       AFL_CAM_BITRATE_KBPS=str(NATIVE_BITRATE_KBPS),
                       AFL_CAM_PREVIEW_SECS=str(PREVIEW_INTERVAL_SECS))
            with open(log_path, "w") as logf:
                self._proc = subprocess.Popen(
                    cmd, stdout=logf, stderr=subprocess.STDOUT, env=env)

            log.info("Recording started: %s → %s (%dx%d, %.0fs, MPP native)",
                     self.label, output.name, self.width, self.height, duration_sec)

            self._monitor = Thread(target=self._wait_proc, daemon=True)
            self._monitor.start()

            return output

    def _wait_proc(self):
        rc = self._proc.wait() if self._proc else 1
        elapsed = time.time() - self._start_time
        with self._lock:
            self._running = False
            output = self._output_path
            raw = self._raw_path
            on_done = self._on_done
        ok = rc == 0 and raw is not None and raw.exists() and \
            self._remux(raw, output)
        log.info("Recording %s: %s (%.1fs, rc=%d)",
                 "complete" if ok else "error", self.label, elapsed, rc)
        if on_done:
            on_done(ok, output, elapsed)

    @staticmethod
    def _remux(raw: Path, out: Path) -> bool:
        """Mux the raw H.264 ES into .mp4 as CFR at the REAL rate measured
        from the `.pts` sidecar (kernel monotonic clock) — so playback speed
        is real-life speed even when auto-exposure runs below 30 fps."""
        from afl_cam.retime import mux_with_pts_rate

        if mux_with_pts_rate(raw, out.with_suffix(".pts"), out,
                             fallback_fps=30.0, codec=ENCODE_CODEC):
            raw.unlink(missing_ok=True)
            return True
        import subprocess

        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-i", str(raw), "-c", "copy", str(out)],
            capture_output=True, text=True, timeout=60)
        if result.returncode == 0 and out.exists() and out.stat().st_size > 0:
            raw.unlink(missing_ok=True)
            return True
        log.error("remux failed for %s: %s", raw.name, result.stderr.strip())
        return False

    def stop(self):
        """Signal the recorder to finish (SIGINT → graceful flush)."""
        import signal

        with self._lock:
            if not self._running:
                return
            log.info("Stopping recording: %s", self.label)
            proc = self._proc
        if proc and proc.poll() is None:
            try:
                proc.send_signal(signal.SIGINT)
            except ProcessLookupError:
                pass

    @property
    def running(self) -> bool:
        return self._running

    @property
    def output_path(self) -> Optional[Path]:
        return self._output_path


def make_recorder(device: str, label: str = "",
                  resolution_key: str = DEFAULT_RESOLUTION):
    """Per-camera native MPP recorder (separate mode). RK3588 only."""
    if not is_rockchip():
        raise RuntimeError("per-camera recording needs the RK3588 MPP "
                           "encoder (/dev/mpp_service)")
    return CameraRecorderMPP(device, label, resolution_key)
