"""Camera preview and recording backends.

Linux/RK3588 uses the native MPP recorder (V4L2 MJPEG → jpegd → rkvenc → .h264).
Other Linux uses GStreamer (v4l2src, MJPEG passthrough into AVI). macOS uses
OpenCV's AVFoundation backend (VideoCapture + MJPEG VideoWriter). All backends
expose the same interface; make_preview()/make_recorder() select the backend
for the current OS/hardware.
"""

import logging
import time
from datetime import datetime
from pathlib import Path
from threading import Thread, Lock
from typing import Optional, Callable

import cv2

from .config import (
    CAMERAS, RESOLUTIONS, DEFAULT_RESOLUTION,
    PREVIEW_WIDTH, PREVIEW_HEIGHT, PREVIEW_FPS,
    CAPTURES_DIR, CONTAINER, PLATFORM, EXPOSURE_TIME,
)

log = logging.getLogger(__name__)

# GStreamer bindings are only needed by the Linux backend; keep the import
# conditional so macOS works without gst-python installed.
if PLATFORM != "darwin":
    import gi
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst, GLib
    Gst.init(None)


# Utilities

def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _make_filename(camera: str, resolution_key: str) -> str:
    return f"{camera}_{_timestamp()}_{resolution_key}.{CONTAINER}"


# Linux backend — GStreamer pipelines

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


class CameraRecorder:
    """Full-resolution MJPEG→AVI recording pipeline for a single camera (Linux)."""

    def __init__(self, device: str, label: str = "",
                 resolution_key: str = DEFAULT_RESOLUTION):
        self.device = device
        self.label = label or device
        self.resolution_key = resolution_key
        res = RESOLUTIONS[resolution_key]
        self.width = res["width"]
        self.height = res["height"]
        self.fps = res["fps"]

        self._pipeline: Optional[Gst.Pipeline] = None
        self._bus_thread: Optional[Thread] = None
        self._start_time: float = 0
        self._on_done: Optional[Callable] = None
        self._output_path: Optional[Path] = None
        self._running = False
        self._lock = Lock()



    def _build(self, output: Path, num_buffers: int = 0):
        """Build the recording pipeline.

        Args:
            output: file path for the AVI
            num_buffers: 0 = record until EOS sent manually
        """
        buffers_arg = f"num-buffers={num_buffers}" if num_buffers > 0 else ""
        pipe = (
            f"v4l2src device={self.device} {buffers_arg} do-timestamp=true ! "
            f"image/jpeg,width={self.width},height={self.height},framerate={self.fps}/1 ! "
            "jpegparse ! avimux ! "
            f"filesink location={output}"
        )
        self._pipeline = Gst.parse_launch(pipe)
        self._output_path = output



    def start(self, duration_sec: float = 0,
              on_done: Optional[Callable] = None,
              output_dir: Optional[Path] = None) -> Path:
        """Start recording.

        Args:
            duration_sec: 0 = record until stop() is called
            on_done: called as on_done(success: bool, path: Path, duration: float)
            output_dir: session folder to write into (as ``cam0.avi``);
                defaults to the legacy flat captures/ directory
        Returns:
            Path to the output file.
        """
        with self._lock:
            if self._running:
                raise RuntimeError(f"Already recording: {self.label}")

            if output_dir is not None:
                output_dir.mkdir(parents=True, exist_ok=True)
                output = output_dir / f"{self.label}.{CONTAINER}"
            else:
                output = CAPTURES_DIR / _make_filename(self.label, self.resolution_key)
                CAPTURES_DIR.mkdir(parents=True, exist_ok=True)

            buffers = int(duration_sec * self.fps) if duration_sec > 0 else 0
            self._build(output, num_buffers=buffers)

            self._on_done = on_done
            self._start_time = time.time()
            self._running = True

            self._pipeline.set_state(Gst.State.PLAYING)
            log.info("Recording started: %s → %s (%dx%d, %.0fs)",
                     self.label, output.name, self.width, self.height, duration_sec)

            # Start bus watcher thread
            self._bus_thread = Thread(
                target=_bus_watch,
                args=(self._pipeline, self._on_eos, self._on_error),
                daemon=True,
            )
            self._bus_thread.start()

            return output

    def stop(self):
        """Send EOS to stop recording gracefully."""
        with self._lock:
            if not self._running:
                return
            log.info("Stopping recording: %s", self.label)
            self._pipeline.send_event(Gst.Event.new_eos())

    def _on_eos(self):
        elapsed = time.time() - self._start_time
        self._pipeline.set_state(Gst.State.NULL)
        with self._lock:
            self._running = False
        log.info("Recording complete: %s (%.1fs)", self.label, elapsed)
        if self._on_done:
            self._on_done(True, self._output_path, elapsed)

    def _on_error(self, err_msg: str):
        self._pipeline.set_state(Gst.State.NULL)
        with self._lock:
            self._running = False
        log.error("Recording error: %s — %s", self.label, err_msg)
        if self._on_done:
            self._on_done(False, self._output_path, 0)

    @property
    def running(self) -> bool:
        return self._running

    @property
    def output_path(self) -> Optional[Path]:
        return self._output_path


# Rockchip backend — native MPP recorder (V4L2 → jpegd → rkvenc → .h264)

def is_rockchip() -> bool:
    """True on RK3588-class boards (vendor kernel with the MPP service node)."""
    return PLATFORM == "linux" and Path("/dev/mpp_service").exists()


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
        """Start recording (see CameraRecorder.start for the contract)."""
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

            with open(log_path, "w") as logf:
                self._proc = subprocess.Popen(
                    cmd, stdout=logf, stderr=subprocess.STDOUT)

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
        """Mux the raw H.264 ES into .mp4 with per-frame timestamps from the
        `.pts` sidecar (kernel monotonic clock) — VFR, so playback speed is
        real-life speed even when auto-exposure runs below 30 fps."""
        from afl_cam.retime import retime_file

        if retime_file(raw, out.with_suffix(".pts"), out):
            raw.unlink(missing_ok=True)
            return True
        log.error("pts retime failed for %s — falling back to CFR copy", raw.name)
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


# macOS backend — OpenCV (AVFoundation)

class CameraPreviewCV:
    """Low-res live preview for a single camera (macOS).

    Frames are delivered to *callback* as raw RGB bytes + width + height.
    """

    def __init__(self, device: str, label: str = "",
                 width: int = PREVIEW_WIDTH, height: int = PREVIEW_HEIGHT,
                 fps: int = PREVIEW_FPS):
        self.device = device
        self.label = label or str(device)
        self.width = width
        self.height = height
        self.fps = fps

        self._running = False
        self._thread: Optional[Thread] = None
        self._callback: Optional[Callable] = None
        self._lock = Lock()

    def start(self, on_frame: Callable):
        """Start preview. *on_frame* is called with (bytes, width, height)."""
        with self._lock:
            if self._running:
                return
            self._callback = on_frame
            self._running = True
            self._thread = Thread(target=self._run, daemon=True)
            self._thread.start()
            log.info("Preview started: %s (%dx%d)", self.label, self.width, self.height)

    def _run(self):
        cap = cv2.VideoCapture(int(self.device))
        if not cap.isOpened():
            log.error("Preview: cannot open camera %s", self.label)
            return
        while self._running:
            ok, frame = cap.read()
            if not ok:
                time.sleep(0.05)
                continue
            frame = cv2.resize(frame, (self.width, self.height),
                               interpolation=cv2.INTER_AREA)
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            if self._callback:
                self._callback(rgb.tobytes(), self.width, self.height)
        cap.release()

    def stop(self):
        with self._lock:
            if not self._running:
                return
            self._running = False
            self._callback = None
        if self._thread:
            self._thread.join(timeout=2)
        log.info("Preview stopped: %s", self.label)

    @property
    def running(self) -> bool:
        return self._running


class CameraRecorderCV:
    """Full-resolution recording for a single camera (macOS).

    AVFoundation capture → MJPEG AVI via cv2.VideoWriter. The writer is
    sized from the first real frame, so it always matches the stream.
    """

    def __init__(self, device: str, label: str = "",
                 resolution_key: str = DEFAULT_RESOLUTION):
        self.device = device
        self.label = label or str(device)
        self.resolution_key = resolution_key
        res = RESOLUTIONS[resolution_key]
        self.width = res["width"]
        self.height = res["height"]
        self.fps = res["fps"]

        self._running = False
        self._thread: Optional[Thread] = None
        self._on_done: Optional[Callable] = None
        self._output_path: Optional[Path] = None
        self._lock = Lock()

    def start(self, duration_sec: float = 0,
              on_done: Optional[Callable] = None,
              output_dir: Optional[Path] = None) -> Path:
        """Start recording (see CameraRecorder.start for the contract)."""
        with self._lock:
            if self._running:
                raise RuntimeError(f"Already recording: {self.label}")

            if output_dir is not None:
                output_dir.mkdir(parents=True, exist_ok=True)
                output = output_dir / f"{self.label}.{CONTAINER}"
            else:
                output = CAPTURES_DIR / _make_filename(self.label, self.resolution_key)
                CAPTURES_DIR.mkdir(parents=True, exist_ok=True)

            self._output_path = output
            self._on_done = on_done
            self._running = True
            self._thread = Thread(target=self._run, args=(output, duration_sec),
                                  daemon=True)
            self._thread.start()
            log.info("Recording started: %s → %s (%dx%d, %.0fs)",
                     self.label, output.name, self.width, self.height, duration_sec)
            return output

    def _run(self, output: Path, duration_sec: float):
        start = time.time()
        ok = False
        cap = cv2.VideoCapture(int(self.device))
        if cap.isOpened():
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
            ret, first = cap.read()
            if ret:
                h, w = first.shape[:2]
                writer = cv2.VideoWriter(
                    str(output), cv2.VideoWriter_fourcc(*"MJPG"),
                    self.fps, (w, h))
                if writer.isOpened():
                    writer.write(first)
                    deadline = start + duration_sec if duration_sec > 0 else None
                    while self._running:
                        if deadline and time.time() >= deadline:
                            break
                        ret, frame = cap.read()
                        if not ret:
                            break
                        writer.write(frame)
                    writer.release()
                    ok = True
                else:
                    log.error("Recording: cannot open writer for %s", output)
            else:
                log.error("Recording: no frame from camera %s", self.label)
        else:
            log.error("Recording: cannot open camera %s", self.label)
        cap.release()
        elapsed = time.time() - start
        with self._lock:
            self._running = False
        log.info("Recording %s: %s (%.1fs)",
                 "complete" if ok else "error", self.label, elapsed)
        if self._on_done:
            self._on_done(ok, self._output_path, elapsed)

    def stop(self):
        with self._lock:
            self._running = False

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
    """Preview backend for the current OS."""
    if PLATFORM == "darwin":
        return CameraPreviewCV(device, label, width, height, fps)
    return CameraPreview(device, label, width, height, fps)


def make_recorder(device: str, label: str = "",
                  resolution_key: str = DEFAULT_RESOLUTION):
    """Recording backend for the current OS/hardware.

    macOS → OpenCV/AVFoundation; RK3588 Linux → native MPP recorder;
    other Linux → GStreamer MJPEG→AVI.
    """
    if PLATFORM == "darwin":
        return CameraRecorderCV(device, label, resolution_key)
    if is_rockchip():
        return CameraRecorderMPP(device, label, resolution_key)
    return CameraRecorder(device, label, resolution_key)
