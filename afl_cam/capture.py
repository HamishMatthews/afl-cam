"""Camera and microphone control.

The single place that knows about OS capture tools: V4L2/ALSA on Linux,
AVFoundation on macOS (via OpenCV and ffmpeg). Everything else in the
package talks to cameras through this module.
"""

import logging
import os
import subprocess
from pathlib import Path
from typing import List, Optional

import cv2

from .config import AUDIO_DEVICES, CAMERAS, PLATFORM

log = logging.getLogger("afl_cam.capture")


def camera_device(cam_key: str) -> Optional[str]:
    """The OS device spec for a camera label (path on Linux, index on macOS)."""
    return CAMERAS.get(cam_key)


def camera_present(cam_key: str) -> bool:
    """Whether the camera can be opened right now."""
    dev = CAMERAS.get(cam_key)
    if not dev:
        return False
    if PLATFORM == "darwin":
        cap = cv2.VideoCapture(int(dev))
        ok = cap.isOpened()
        cap.release()
        return ok
    return os.path.exists(dev)


def capture_frame(cam_key: str, width: int = 3840, height: int = 2160):
    """Grab one frame from a camera, or None if it can't be opened.

    Reads three frames and returns the last — right after a stream opens,
    the first frames can be dark while auto-exposure settles.
    """
    dev = CAMERAS.get(cam_key)
    if not dev:
        return None
    cap = cv2.VideoCapture(int(dev) if PLATFORM == "darwin" else dev)
    if not cap.isOpened():
        cap.release()
        return None
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
    frame = None
    for _ in range(3):
        ok, frame = cap.read()
        if not ok:
            frame = None
    cap.release()
    return frame


def start_audio_capture(cam_key: str, out_path: Path) -> Optional[subprocess.Popen]:
    """Record a camera's USB microphone to a WAV (for clap-based sync).

    Returns the running process, or None if the tool or device is missing.
    """
    dev = AUDIO_DEVICES.get(cam_key)
    if not dev:
        return None
    if PLATFORM == "darwin":
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
               "-f", "avfoundation", "-i", f":{dev}",
               "-ar", "44100", "-ac", "2", "-c:a", "pcm_s16le",
               str(out_path)]
    else:
        cmd = ["arecord", "-q", "-D", dev, "-f", "S16_LE",
               "-c", "2", "-r", "44100", str(out_path)]
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        log.warning("%s not found — no audio for %s", cmd[0], cam_key)
        return None
    return proc


def stop_audio_procs(procs: List[subprocess.Popen]):
    for proc in procs:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()


def set_auto_exposure(cam_key: str):
    """Set aperture-priority exposure AFTER the stream opens.

    Mode 3 (Aperture Priority) is the working mode; opening a 4K stream can
    reset controls, and mode 1 (Manual) records almost-black frames. Linux
    sets it via v4l2-ctl; AVFoundation has no exposure control, so on macOS
    the camera default (auto) applies.
    """
    if PLATFORM == "darwin":
        return
    dev = CAMERAS.get(cam_key)
    if dev and os.path.exists(dev):
        subprocess.run(["v4l2-ctl", "-d", dev, "--set-ctrl=auto_exposure=3"],
                       capture_output=True)


def list_devices() -> int:
    """Enumerate cameras and microphones. Returns a process exit code."""
    if PLATFORM == "darwin":
        try:
            result = subprocess.run(
                ["ffmpeg", "-hide_banner", "-f", "avfoundation",
                 "-list_devices", "true", "-i", ""],
                capture_output=True, text=True, timeout=15)
        except FileNotFoundError:
            print("ffmpeg not found — install it with: brew install ffmpeg")
            return 1
        print(result.stderr.strip())
        print("\nSet the indices in afl_cam/config.py (CAMERAS and AUDIO_DEVICES).")
        return 0

    print("Cameras (v4l2):")
    subprocess.run(["v4l2-ctl", "--list-devices"], check=False)
    print("\nAudio capture devices (ALSA):")
    subprocess.run(["arecord", "-l"], check=False)
    print("\nSet the device paths in afl_cam/config.py (CAMERAS and AUDIO_DEVICES).")
    return 0
