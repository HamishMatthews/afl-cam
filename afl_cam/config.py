"""Constants and configuration for afl-cam."""

import os
import re
import subprocess
import sys
from pathlib import Path

# Project root
ROOT_DIR = Path(__file__).resolve().parent.parent
CAPTURES_DIR = ROOT_DIR / "captures"   # legacy flat recordings dir
OUTPUTS_DIR = ROOT_DIR / "outputs"     # per-session folders (recordings + stitch)
DB_PATH = ROOT_DIR / "recordings.db"

PLATFORM = sys.platform  # "linux" or "darwin"

# Camera devices (UVC). Each camera exposes TWO video nodes on the same
# interface: a main node (streams MJPG) and a metadata node (no formats).
# Node numbers SHUFFLE ACROSS REBOOTS, so they are resolved at runtime:
# the USB controller port (xhci.12 = cam0, xhci.11 = cam1 — the rig wiring)
# plus MJPG support identify each camera's main node. The cameras are USB
# 2.0 devices, so a camera plugged into a USB 2.0 port enumerates on the
# EHCI companion controller (fc800000.usb) instead of xhci — those are
# matched by their port path too. Cameras on undocumented ports fill the
# remaining slots in sorted order; use the Swap button if the sides come
# out swapped. The static map is a fallback for when discovery fails.
#   macOS — AVFoundation device indices (find them with 'afl-cam list-devices')
def _discover_cameras():
    try:
        found = {}
        for node in sorted(os.listdir("/sys/class/video4linux")):
            link = os.readlink(f"/sys/class/video4linux/{node}")
            m = re.search(r"xhci-hcd\.(\d+)\.auto", link)
            e = re.search(r"fc[0-9a-f]+\.usb/usb\d+/(\d+(?:-\d+)*)/", link)
            key = m.group(1) if m else ("e" + e.group(1)) if e else None
            if key is None or key in found:
                continue
            out = subprocess.run(
                ["v4l2-ctl", "-d", f"/dev/{node}", "--list-formats"],
                capture_output=True, text=True, timeout=5).stdout
            if "MJPG" in out:
                found[key] = f"/dev/{node}"
        by_ctl = {"cam0": "12", "cam1": "11"}
        resolved = {}
        for cam, ctl in by_ctl.items():
            if ctl in found:
                resolved[cam] = found.pop(ctl)
        for cam in sorted(set(by_ctl) - set(resolved)):
            if found:
                key = sorted(found)[0]
                resolved[cam] = found.pop(key)
        if len(resolved) == 2:
            return resolved
    except (OSError, subprocess.SubprocessError):
        pass
    return None

CAMERAS = _discover_cameras() if PLATFORM == "linux" else {
    "cam0": "0",
    "cam1": "1",
}
if CAMERAS is None:
    CAMERAS = {
        "cam0": "/dev/video0",
        "cam1": "/dev/video1",
    }

# Camera → its USB microphone, recorded alongside the video for clap-based
# timecode syncing:
#   Linux (Orange Pi 5 Max) — ALSA cards 3 ("Camera") and 4 ("Camera_1")
#   macOS  — AVFoundation audio device index
AUDIO_DEVICES = {
    "cam0": "hw:3,0" if PLATFORM == "linux" else "0",
    "cam1": "hw:4,0" if PLATFORM == "linux" else "1",
}

# Recording resolutions
RESOLUTIONS = {
    "4K":    {"width": 3840, "height": 2160, "fps": 30, "label": "3840×2160 30fps"},
    "1440p": {"width": 2560, "height": 1440, "fps": 30, "label": "2560×1440 30fps"},
    "1080p": {"width": 1920, "height": 1080, "fps": 30, "label": "1920×1080 30fps"},
    "720p":  {"width": 1280, "height": 720,  "fps": 30, "label": "1280×720 30fps"},
}

DEFAULT_RESOLUTION = "4K"

# Preview settings (lower res to save USB bandwidth)
PREVIEW_WIDTH = 640
PREVIEW_HEIGHT = 360
PREVIEW_FPS = 30

# Container format for recordings
CONTAINER = "avi"
CODEC = "MJPEG"

# Native MPP recorder (RK3588: V4L2 → jpegd → rkvenc; raw .h264 ES is
# stream-copied into .mp4 on completion)
NATIVE_RECORDER_SRC = ROOT_DIR / "afl_cam" / "native" / "cam2h264.c"
NATIVE_RECORDER_BIN = ROOT_DIR / "afl_cam" / "native" / "cam2h264"
NATIVE_CODEC = "H264"
NATIVE_CONTAINER = "mp4"
NATIVE_BITRATE = 20_000_000  # CBR target for rkvenc

# Exposure for the MPP recorder: 0 = auto-settle-then-lock (the recorder
# matches the auto exposure's brightness with a locked manual value after
# ~2s — works day/night/indoor), or a fixed value in 100us units
# (daytime: 100 = 10ms). The cameras' auto-exposure can settle at ~50ms
# and cap the sensor at ~20 fps; the lock keeps the rate consistent.
EXPOSURE_TIME = 0

# Default recording duration (0 = manual stop)
DEFAULT_DURATION = 0

# Live GPU stitching at record time: cam2stitch records BOTH cameras in one
# process and writes the stitched panorama directly (no post step). When the
# probe fails or a camera is missing the UI falls back to per-camera
# recording. Off switch: AFL_CAM_LIVE_STITCH=0.
LIVE_STITCH = os.environ.get("AFL_CAM_LIVE_STITCH", "1") == "1"
STITCH_RECORDER_SRC = ROOT_DIR / "afl_cam" / "native" / "cam2stitch.c"
STITCH_RECORDER_BIN = ROOT_DIR / "afl_cam" / "native" / "cam2stitch"
STITCH_SHADER = ROOT_DIR / "afl_cam" / "native" / "stitch_nv12.spv"
# Native full-resolution canvas (5304x2084 for the current rig aim — the
# combined per-camera resolution the user asked for; verified live at 30fps
# pipeline capacity, d9c220b). Regenerate via:
#   scripts/dump_camera_params.py <cam0.jpg> <cam1.jpg> calibration/stitch_live_full
STITCH_PARAMS_DIR = ROOT_DIR / "calibration" / "stitch_live_full"
STITCH_CANVAS_WIDTH = 5304
# Re-align workflow (UI button): captures two stills and regenerates the
# frozen stitch params. Needs the OpenCV 5 venv (the project venv is py3.14
# with no wheels for the bundle API).
ALIGN_PYTHON = "/home/hamish/venvs/cv2-new/bin/python3"
ALIGN_SCRIPT = ROOT_DIR / "scripts" / "recapture_alignment.py"

# libmali Vulkan env for the cam2stitch subprocess (spawned processes do not
# inherit the interactive shell's env; the ICD lives in a non-standard dir)
VK_ICD_FILENAMES = "/opt/libmali-g610/usr/share/vulkan/icd.d/mali.json"
VK_LD_LIBRARY_PATH = ("/opt/libmali-g610/usr/lib/aarch64-linux-gnu:"
                      "/opt/libmali-g610/usr/lib/aarch64-linux-gnu/mali")

# SQLite pragmas
DB_PRAGMAS = {
    "journal_mode": "WAL",
    "foreign_keys": "ON",
}
