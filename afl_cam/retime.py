"""Mux raw H.264 recordings at the REAL frame rate measured from the .pts sidecar.

The native recorder (cam2h264) stamps every encoded frame with the kernel
monotonic clock (us) into a `.pts` sidecar — the SAME clock for both cameras,
sampled when the frame completes at the xhci controller. At auto-exposure the
sensor's rate varies (e.g. 50 ms exposure → 20 fps), so a fixed-30 fps
container plays the footage too fast.

The sidecar gives the true per-frame timing; the container just needs to
play back at real-life speed. So recordings are muxed as **CFR at the median
measured rate** — honest playback speed, and every player AND cv2 reads CFR
containers reliably.

(VFR containers were tried and REJECTED 2026-08-25: cv2.VideoCapture,
ffmpeg's image2 muxer and ffprobe disagree on VFR mp4s — frame drops/dups —
which silently broke the stitcher's frame pairing. Per-frame alignment stays
in the `.pts` sidecar, where it belongs; containers stay simple.)
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)


def load_pts(path: Path) -> np.ndarray | None:
    """Kernel timestamps from a `.pts` sidecar (us, monotonic), or None."""
    try:
        with open(path) as fh:
            lines = fh.read().split()
        if len(lines) < 10:
            return None
        return np.asarray([int(x) for x in lines], dtype=np.int64)
    except (OSError, ValueError):
        return None


def measured_fps(pts_us: np.ndarray, fallback: float = 30.0) -> float:
    """Median frame rate from kernel timestamps (robust to the startup gap)."""
    if len(pts_us) < 3:
        return fallback
    dt = np.diff(pts_us)
    dt = dt[dt > 1000]  # drop the ~200 ms first-frame startup gap
    return 1e6 / float(np.median(dt)) if len(dt) else fallback


def mux_with_pts_rate(raw: Path, pts_path: Path, out: Path,
                      fallback_fps: float = 30.0) -> bool:
    """Mux the raw .h264 ES into .mp4 as CFR at the measured real rate.

    Stream copy (no re-encode) — the container timestamps are regular at the
    median measured frame rate, so playback speed = real-life speed. The
    `.pts` sidecar remains the exact per-frame timing for the stitcher.
    """
    pts = load_pts(pts_path)
    fps = measured_fps(pts, fallback_fps) if pts is not None else fallback_fps
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "h264", "-framerate", f"{fps:.4f}", "-i", str(raw),
         "-c", "copy", str(out)],
        capture_output=True, text=True, timeout=60)
    if result.returncode == 0 and out.exists() and out.stat().st_size > 0:
        return True
    log.error("mux failed for %s: %s", raw.name, result.stderr.strip())
    return False
