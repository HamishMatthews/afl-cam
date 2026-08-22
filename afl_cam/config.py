"""Constants and configuration for afl-cam."""

import sys
from pathlib import Path

# Project root
ROOT_DIR = Path(__file__).resolve().parent.parent
CAPTURES_DIR = ROOT_DIR / "captures"   # legacy flat recordings dir
OUTPUTS_DIR = ROOT_DIR / "outputs"     # per-session folders (recordings + stitch)
DB_PATH = ROOT_DIR / "recordings.db"

PLATFORM = sys.platform  # "linux" or "darwin"

# Camera devices (UVC), matched by USB port:
#   Linux (Orange Pi 5 Max) — /dev/video0 = cam0 (Bus 1, xhci.12),
#                              /dev/video2 = cam1 (Bus 3, xhci.11);
#                              video1/video3 are the same cameras' metadata nodes
#   macOS  — AVFoundation device indices (find them with 'afl-cam list-devices')
CAMERAS = {
    "cam0": "/dev/video0" if PLATFORM == "linux" else "0",
    "cam1": "/dev/video2" if PLATFORM == "linux" else "1",
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

# Manual exposure used by the MPP recorder (the cameras' auto-exposure can
# settle at ~50 ms and cap the sensor at ~20 fps; 100 = 10 ms exposure)
EXPOSURE_TIME = 100

# Default recording duration (0 = manual stop)
DEFAULT_DURATION = 0

# SQLite pragmas
DB_PRAGMAS = {
    "journal_mode": "WAL",
    "foreign_keys": "ON",
}
