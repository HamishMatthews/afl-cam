"""Camera preview and recording backends (RK3588 only).

Recording uses the native MPP recorders: cam2h264 per camera (separate mode)
or cam2stitch for both cameras (live stitch). The GStreamer preview feeds the
GTK desk UI.
"""

import logging
import os
import time
from datetime import datetime
from pathlib import Path
from threading import Thread, Lock
from typing import Optional, Callable

from .config import (
    RESOLUTIONS, DEFAULT_RESOLUTION,
    PREVIEW_WIDTH, PREVIEW_HEIGHT, PREVIEW_FPS,
    CONTAINER, PLATFORM, EXPOSURE_TIME,
    ENCODE_CODEC, NATIVE_BITRATE_KBPS, STITCH_BITRATE_KBPS,
    PREVIEW_INTERVAL_SECS,
)

log = logging.getLogger(__name__)

import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402
Gst.init(None)


# Utilities

def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _make_filename(camera: str, resolution_key: str) -> str:
    return f"{camera}_{_timestamp()}_{resolution_key}.{CONTAINER}"


# GStreamer preview

def _bus_watch(pipeline: Gst.Pipeline, on_eos: Callable, on_error: Callable):
    """GLib-free bus poller — runs in a thread, calls back on EOS/error."""
    bus = pipeline.get_bus()
    while True:
        msg = bus.timed_pop_filtered(
            Gst.CLOCK_TIME_NONE,
            Gst.MessageType.EOS | Gst.MessageType.ERROR,
        )
        if msg is None:
            continue
        if msg.type == Gst.MessageType.EOS:
            on_eos()
            return
        elif msg.type == Gst.MessageType.ERROR:
            err, debug = msg.parse_error()
            log.error("GStreamer error on pipeline: %s (%s)", err, debug)
            on_error(str(err))
            return
        elif msg.type == Gst.MessageType.EOS:
            break


class CameraPreview:
    """Low-resolution MJPEG preview pipeline for a single camera (Linux).

    Frames are delivered to *callback* as raw RGB bytes + width + height.
    """

    def __init__(self, device: str, label: str = "",
                 width: int = PREVIEW_WIDTH, height: int = PREVIEW_HEIGHT,
                 fps: int = PREVIEW_FPS):
        self.device = device
        self.label = label or device
        self.width = width
        self.height = height
        self.fps = fps

        self._pipeline: Optional[Gst.Pipeline] = None
        self._running = False
        self._thread: Optional[Thread] = None
        self._callback: Optional[Callable] = None
        self._lock = Lock()



    def _build(self):
        pipe = (
            f"v4l2src device={self.device} do-timestamp=true ! "
            f"image/jpeg,width={self.width},height={self.height},framerate={self.fps}/1 ! "
            "jpegdec ! videoconvert ! "
            "video/x-raw,format=RGB ! "
            "appsink name=sink emit-signals=true max-buffers=2 drop=true sync=false"
        )
        self._pipeline = Gst.parse_launch(pipe)
        self._sink = self._pipeline.get_by_name("sink")



    def start(self, on_frame: Callable):
        """Start preview. *on_frame* is called with (bytes, width, height)."""
        with self._lock:
            if self._running:
                return
            self._callback = on_frame
            self._build()
            self._sink.connect("new-sample", self._on_new_sample)
            self._pipeline.set_state(Gst.State.PLAYING)
            self._running = True
            log.info("Preview started: %s (%dx%d)", self.label, self.width, self.height)

    def stop(self):
        """Stop the preview pipeline."""
        with self._lock:
            if not self._running:
                return
            self._running = False
            if self._pipeline:
                self._pipeline.set_state(Gst.State.NULL)
                self._pipeline = None
            self._sink = None
            self._callback = None
            log.info("Preview stopped: %s", self.label)

    @property
    def running(self) -> bool:
        return self._running



    def _on_new_sample(self, sink) -> Gst.FlowReturn:
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.ERROR
        buf = sample.get_buffer()
        ok, map_info = buf.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.FlowReturn.ERROR
        try:
            if self._callback:
                self._callback(map_info.data, self.width, self.height)
        finally:
            buf.unmap(map_info)
        return Gst.FlowReturn.OK


# Rockchip backend — native MPP recorder (V4L2 → jpegd → rkvenc → .h264)

def is_rockchip() -> bool:
    """True on RK3588-class boards (vendor kernel with the MPP service node)."""
    return PLATFORM == "linux" and Path("/dev/mpp_service").exists()


def ensure_cam2stitch() -> Optional[Path]:
    """Compile the live-stitch recorder if missing or stale."""
    from .config import build_cam2stitch
    return build_cam2stitch()


def can_live_stitch() -> bool:
    """Probe gate: Vulkan + shader + params init succeeds (--probe).

    Runs before each live recording; the UI falls back to per-camera
    recording when this is False.
    """
    import subprocess

    from .config import (STITCH_PARAMS_DIR, STITCH_RECORDER_BIN,
                         STITCH_SHADER, VK_ICD_FILENAMES, VK_LD_LIBRARY_PATH)

    if not STITCH_RECORDER_BIN.exists() or not STITCH_PARAMS_DIR.exists():
        return False
    env = dict(os.environ,
               VK_DRIVER_FILES=VK_ICD_FILENAMES,
               LD_LIBRARY_PATH=VK_LD_LIBRARY_PATH + ":"
               + os.environ.get("LD_LIBRARY_PATH", ""))
    try:
        result = subprocess.run(
            [str(STITCH_RECORDER_BIN), "--probe", str(STITCH_PARAMS_DIR),
             str(STITCH_SHADER)],
            capture_output=True, text=True, timeout=60, env=env)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and "OK" in result.stdout


def ensure_cam2h264() -> Optional[Path]:
    """Compile the native recorder if missing or older than its source.

    Returns the binary path, or None if the build fails (gcc/librockchip_mpp
    missing — see orange_pi_5_max_setup.md).
    """
    import subprocess

    from .config import NATIVE_RECORDER_BIN, NATIVE_RECORDER_SRC

    if NATIVE_RECORDER_BIN.exists() and NATIVE_RECORDER_SRC.exists() and \
            NATIVE_RECORDER_BIN.stat().st_mtime >= NATIVE_RECORDER_SRC.stat().st_mtime:
        return NATIVE_RECORDER_BIN

    cmd = [
        "gcc", "-O2", "-I/usr/local/include/rockchip",
        "-o", str(NATIVE_RECORDER_BIN), str(NATIVE_RECORDER_SRC),
        "-L/usr/local/lib", "-lrockchip_mpp", "-lpthread",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        log.error("cam2h264 build failed: %s", e)
        return None
    if result.returncode != 0:
        log.error("cam2h264 build failed: %s", result.stderr.strip())
        return None
    log.info("Built native recorder: %s", NATIVE_RECORDER_BIN)
    return NATIVE_RECORDER_BIN


class CameraRecorderMPP:
    """Native hardware recorder for a single camera (RK3588).

    Runs the cam2h264 binary: USB MJPEG capture → jpegd decode (NV12, hardware)
    → rkvenc H.264 encode (hardware) → raw .h264 elementary stream. No ffmpeg,
    no format conversions; ~0.2 cores per stream at 4K30.

    Exposes the same interface as CameraRecorder.
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


class StitchRecorderMPP:
    """Live dual-camera stitched recorder (RK3588, cam2stitch).

    One process opens BOTH UVC cameras (MJPEG → jpegd NV12), pairs frames
    by kernel timestamp, stitches on the Mali GPU and encodes the composite
    panorama — no post-stitch step. Same interface as CameraRecorderMPP.
    """

    codec = "H264"
    container = "mp4"

    def __init__(self, device0: str, device1: str, label: str = "stitch",
                 resolution_key: str = DEFAULT_RESOLUTION):
        self.device0 = device0
        self.device1 = device1
        self.label = label
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
        """Start a live-stitched recording (contract as CameraRecorderMPP)."""
        import signal
        import subprocess

        from .config import (CAPTURES_DIR, EXPOSURE_TIME, STITCH_PARAMS_DIR,
                             STITCH_RECORDER_BIN, STITCH_SHADER,
                             VK_ICD_FILENAMES, VK_LD_LIBRARY_PATH)

        with self._lock:
            if self._running:
                raise RuntimeError(f"Already recording: {self.label}")

            recorder = ensure_cam2stitch()
            if recorder is None:
                raise RuntimeError("Live-stitch recorder unavailable — build failed")

            if output_dir is not None:
                output_dir.mkdir(parents=True, exist_ok=True)
                output = output_dir / f"{self.label}.mp4"
            else:
                output = CAPTURES_DIR / _make_filename(self.label, self.resolution_key)
                CAPTURES_DIR.mkdir(parents=True, exist_ok=True)

            raw = output.with_suffix(".raw.h264")
            frames = int(duration_sec * self.fps) if duration_sec > 0 else 0
            log_path = output.with_suffix(".rec.log")
            cmd = [str(recorder), self.device0, self.device1,
                   str(STITCH_PARAMS_DIR), str(raw), str(frames),
                   str(self.width), str(self.height), str(self.fps),
                   str(EXPOSURE_TIME),
                   str(int(duration_sec)) if duration_sec > 0 else "0",
                   str(STITCH_SHADER)]

            self._output_path = output
            self._raw_path = raw
            self._on_done = on_done
            self._start_time = time.time()
            self._running = True

            # libmali env — subprocesses don't inherit the shell's env
            env = dict(os.environ,
                       VK_DRIVER_FILES=VK_ICD_FILENAMES,
                       LD_LIBRARY_PATH=VK_LD_LIBRARY_PATH + ":"
                       + os.environ.get("LD_LIBRARY_PATH", ""),
                       AFL_CAM_CODEC=ENCODE_CODEC,
                       AFL_CAM_STITCH_BITRATE_KBPS=str(STITCH_BITRATE_KBPS),
                       AFL_CAM_PREVIEW_SECS=str(PREVIEW_INTERVAL_SECS))
            with open(log_path, "w") as logf:
                self._proc = subprocess.Popen(
                    cmd, stdout=logf, stderr=subprocess.STDOUT, env=env)

            log.info("Live stitch started: %s → %s (%.0fs, GPU)",
                     self.label, output.name, duration_sec)

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
            CameraRecorderMPP._remux(raw, output)
        log.info("Live stitch %s: %s (%.1fs, rc=%d)",
                 "complete" if ok else "error", self.label, elapsed, rc)
        if on_done:
            on_done(ok, output, elapsed)

    def stop(self):
        """Signal the recorder to finish (SIGINT → graceful flush)."""
        import signal

        with self._lock:
            if not self._running:
                return
            log.info("Stopping live stitch: %s", self.label)
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


# Backend selection

def make_preview(device: str, label: str = "",
                 width: int = PREVIEW_WIDTH, height: int = PREVIEW_HEIGHT,
                 fps: int = PREVIEW_FPS):
    """GStreamer preview (GTK desk UI)."""
    return CameraPreview(device, label, width, height, fps)


def make_recorder(device: str, label: str = "",
                  resolution_key: str = DEFAULT_RESOLUTION):
    """Per-camera native MPP recorder (separate mode). RK3588 only."""
    if not is_rockchip():
        raise RuntimeError("per-camera recording needs the RK3588 MPP "
                           "encoder (/dev/mpp_service)")
    return CameraRecorderMPP(device, label, resolution_key)
