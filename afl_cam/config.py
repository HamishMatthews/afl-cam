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
# Node numbers SHUFFLE ACROSS REBOOTS, so they are resolved at runtime.
#   The two cameras carry IDENTICAL USB descriptors (serial "01.00.00",
#   same mfr/firmware rev) — the PHYSICAL USB SOCKET is the only stable
#   identity. The rig's sockets are PHYSICALLY LABELED (2026-10-09):
#   the left camera's plug always goes in the LEFT socket, the right
#   camera's in the RIGHT socket, and this map is the hardcoded wiring:
#       cam0 (left)  = xhci.11 USB3 socket (usb1; USB2 aliases below)
#       cam1 (right) = xhci.12 USB3 socket (usb3)
#   Each USB2 socket is backed by an EHCI (480M) + OHCI (12M) companion
#   pair, listed as aliases in case the cables move back to the USB2
#   sockets. If the rig wiring EVER changes, edit this one map — there is
#   no Swap button (removed 2026-10-09 by design: labels, not software).
#   If a camera's high-speed connect chirp fails (its USB firmware can
#   wedge after a hard crash mid-stream) it re-enumerates at full speed on
#   the OHCI side, where uvcvideo's probe FAILS — the camera then has no
#   video node at all and its panel sits frozen. scripts/usb_kick_cameras.sh
#   soft-resets the socket (no replug needed); the daemon runs it
#   automatically when a camera is missing.
#   macOS — AVFoundation device indices (find them with 'afl-cam list-devices')
_CAMERA_PORTS = {
    # Physical socket controller aliases: (USB3 xhci, USB2 EHCI, USB2 OHCI)
    "cam0": ("xhci-hcd.11.auto", "fc800000.usb", "fc840000.usb"),
    "cam1": ("xhci-hcd.12.auto", "fc880000.usb", "fc8c0000.usb"),
}

def _warn_wedged_cameras():
    """Print a diagnostic when a camera is stuck at full speed (12 Mbps)."""
    try:
        for dev in os.listdir("/sys/bus/usb/devices"):
            d = Path("/sys/bus/usb/devices", dev)
            if not (d / "idProduct").exists():
                continue
            if (d / "idProduct").read_text().strip() != "28c4":
                continue
            speed = (d / "speed").read_text().strip()
            if speed != "480":
                print(f"afl-cam: WARNING: camera {dev} enumerated at "
                      f"{speed} Mbps (wedged) — run "
                      f"scripts/usb_kick_cameras.sh or replug it",
                      file=sys.stderr)
    except OSError:
        pass

def _scan_cameras():
    """All MJPG-capable video nodes, keyed by physical USB socket.

    dict[(controller, port_path), node]. The controller segment is the one
    IMMEDIATELY before /usbN/: an xhci path carries an extra segment
    (.../fc000000.usb/xhci-hcd.11.auto/usb1/...).
    """
    found = {}
    for node in sorted(os.listdir("/sys/class/video4linux")):
        link = os.readlink(f"/sys/class/video4linux/{node}")
        m = re.search(
            r"/(xhci-hcd\.\d+\.auto|[0-9a-f]+\.usb)"
            r"/usb\d+/(\d+(?:-\d+)*)/", link)
        if not m:
            continue
        key = (m.group(1), m.group(2))  # (controller, port path)
        if key in found:
            continue
        out = subprocess.run(
            ["v4l2-ctl", "-d", f"/dev/{node}", "--list-formats"],
            capture_output=True, text=True, timeout=5).stdout
        if "MJPG" in out:
            found[key] = f"/dev/{node}"
    return found


def _discover_cameras():
    try:
        found = _scan_cameras()
        resolved = {}
        for cam, ctl_names in _CAMERA_PORTS.items():
            for key, dev in list(found.items()):
                if key[0] in ctl_names:
                    resolved[cam] = found.pop(key)
                    break
        # Cameras on unexpected sockets fill the remaining slots by sorted
        # controller+port order (best effort).
        for cam in sorted(set(_CAMERA_PORTS) - set(resolved)):
            if found:
                key = sorted(found)[0]
                resolved[cam] = found.pop(key)
        if resolved:
            _warn_wedged_cameras()
            return resolved
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def rediscover_cameras():
    """Re-run discovery, updating the module CAMERAS dict IN PLACE (same
    object — callers holding a reference see the new assignment)."""
    resolved = _discover_cameras()
    if resolved is None:
        resolved = {"cam0": "/dev/video0", "cam1": "/dev/video1"}
    CAMERAS.clear()
    CAMERAS.update(resolved)
    return dict(CAMERAS)

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

# Native MPP recorder (RK3588: V4L2 → jpegd → rkvenc; raw .h264 ES is
# stream-copied into .mp4 on completion)
NATIVE_RECORDER_SRC = ROOT_DIR / "afl_cam" / "native" / "cam2h264.c"
NATIVE_RECORDER_BIN = ROOT_DIR / "afl_cam" / "native" / "cam2h264"
# rkvenc encode settings. Codec: HEVC default — measured (cam2h264 --bench)
# same-or-better throughput than H.264 on this silicon, ~2x quality per bit,
# and dual-concurrent 4K30 HEVC@60M sustains ~49 fps each (1.6x headroom).
# H.264 via AFL_CAM_CODEC=h264 (drop bitrate to ~45M — 60M H.264 is marginal).
# Bitrate: the 4K30 UVC source MJPEG runs ~125 Mbps; 60/80 Mbps HEVC ≈
# 90/120 Mbps H.264 quality (~27 GB/hr per camera at 60 Mbps — the NVMe has
# ~875 GB free). Tune via env or here.
ENCODE_CODEC = os.environ.get("AFL_CAM_CODEC", "hevc")   # "hevc" | "h264"
NATIVE_BITRATE_KBPS = int(os.environ.get("AFL_CAM_BITRATE_KBPS", "60000"))
STITCH_BITRATE_KBPS = int(os.environ.get("AFL_CAM_STITCH_BITRATE_KBPS", "80000"))
# The native recorders write the raw UVC JPEG payload to
# <session>/camN_preview.jpg this often; the phone page polls those files
# for a live preview while recording in separate mode. 1 fps: the
# payloads are full 4K (~600 KB).
PREVIEW_INTERVAL_SECS = float(os.environ.get("AFL_CAM_PREVIEW_SECS", "1.0"))

# Exposure for the MPP recorder: 0 = auto-settle-then-lock (the recorder
# matches the auto exposure's brightness with a locked manual value after
# ~2s — works day/night/indoor), or a fixed value in 100us units
# (daytime: 100 = 10ms). The cameras' auto-exposure can settle at ~50ms
# and cap the sensor at ~20 fps; the lock keeps the rate consistent.
EXPOSURE_TIME = 0

# Default recording duration (0 = manual stop)
DEFAULT_DURATION = 0

# Live GPU stitching (cam2stitch under afl-camd): one process records BOTH
# cameras and writes the panorama directly (no post step).
STITCH_RECORDER_SRC = ROOT_DIR / "afl_cam" / "native" / "cam2stitch.c"
STITCH_RECORDER_BIN = ROOT_DIR / "afl_cam" / "native" / "cam2stitch"
STITCH_SHADER = ROOT_DIR / "afl_cam" / "native" / "stitch_nv12.spv"
# Native full-resolution canvas: width is STITCH_CANVAS_WIDTH, height comes
# from the frozen maps (currently 5304×1876). Regenerate via:
#   scripts/dump_camera_params.py <cam0.jpg> <cam1.jpg> calibration/stitch_live_full
STITCH_PARAMS_DIR = ROOT_DIR / "calibration" / "stitch_live_full"
# The same maps cropped to the largest fully covered rectangle (no black
# margins), derived from STITCH_PARAMS_DIR by afl_cam/crop_maps.py when the
# crop setting is on. Never written by hand. The rig tests point it at a
# scratch dir (calibration/ is shared by every test run).
CROP_PARAMS_DIR = Path(os.environ.get("AFL_CAM_CROP_DIR")
                       or ROOT_DIR / "calibration" / "stitch_live_full_crop")
STITCH_CANVAS_WIDTH = 5304
# Re-align workflow (phone Setup → Re-align): captures two stills and regenerates the
# frozen stitch params. Needs the OpenCV 5 venv (the project venv is py3.14
# with no wheels for the bundle API).
ALIGN_PYTHON = "/home/hamish/venvs/cv2-new/bin/python3"
ALIGN_SCRIPT = ROOT_DIR / "scripts" / "recapture_alignment.py"

# libmali Vulkan env for the cam2stitch subprocess (spawned processes do not
# inherit the interactive shell's env; the ICD lives in a non-standard dir)
VK_ICD_FILENAMES = "/opt/libmali-g610/usr/share/vulkan/icd.d/mali.json"
VK_LD_LIBRARY_PATH = ("/opt/libmali-g610/usr/lib/aarch64-linux-gnu:"
                      "/opt/libmali-g610/usr/lib/aarch64-linux-gnu/mali")


def vk_env(base_env=None) -> dict:
    """Env for spawning cam2stitch: libmali ICD + LD_LIBRARY_PATH."""
    env = dict(os.environ if base_env is None else base_env)
    env["VK_DRIVER_FILES"] = VK_ICD_FILENAMES
    env["LD_LIBRARY_PATH"] = (VK_LD_LIBRARY_PATH + ":"
                              + env.get("LD_LIBRARY_PATH", ""))
    return env


def build_cam2stitch() -> Path | None:
    """Compile the live-stitch recorder if missing or stale (stdlib-only —
    the afl-camd daemon needs this without importing engine/cv2)."""
    import subprocess

    if STITCH_RECORDER_BIN.exists() and STITCH_RECORDER_SRC.exists() and \
            STITCH_RECORDER_BIN.stat().st_mtime >= STITCH_RECORDER_SRC.stat().st_mtime:
        return STITCH_RECORDER_BIN
    cmd = [
        "gcc", "-O2", "-I/usr/local/include/rockchip",
        "-o", str(STITCH_RECORDER_BIN), str(STITCH_RECORDER_SRC),
        "-L/usr/local/lib", "-lrockchip_mpp", "-lvulkan", "-lpthread",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        print(f"afl-cam: cam2stitch build failed: {e}", file=sys.stderr)
        return None
    if result.returncode != 0:
        print(f"afl-cam: cam2stitch build failed: "
              f"{result.stderr.strip()[-400:]}", file=sys.stderr)
        return None
    return STITCH_RECORDER_BIN

def build_cam2h264() -> Path | None:
    """Compile the per-camera recorder (separate mode) if missing or stale
    (stdlib-only, like build_cam2stitch — the daemon needs it too)."""
    import subprocess

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
        print(f"afl-cam: cam2h264 build failed: {e}", file=sys.stderr)
        return None
    if result.returncode != 0:
        print(f"afl-cam: cam2h264 build failed: "
              f"{result.stderr.strip()[-400:]}", file=sys.stderr)
        return None
    return NATIVE_RECORDER_BIN

# SQLite pragmas
DB_PRAGMAS = {
    "journal_mode": "WAL",
    "foreign_keys": "ON",
}
