"""Re-time video containers from the recorder's kernel-timestamp sidecar.

The native recorder (cam2h264) stamps every encoded frame with the kernel
monotonic clock (us) into a `.pts` sidecar — the SAME clock for both cameras,
sampled when the frame completes at the xhci controller. At auto-exposure
the sensor's rate varies (e.g. 50 ms exposure → 20 fps), so a fixed-30 fps
container plays the footage too fast and hides the true durations. This
module re-muxes the stream with per-frame pts/dts taken from the sidecar
(VFR), so playback speed equals real-life speed and both cameras' recordings
display their true (different) durations.

PyAV runs in the cv2-new venv (`RETIME_PY`): its av 13.1.0 works, while
av 18 segfaults in add_stream and the project venv is Python 3.14 (no av 13
wheels). `retime_file` shells out to that interpreter; callers never import
PyAV in-process.
"""

from __future__ import annotations

import fractions
import logging
import os
import subprocess
import sys
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

USEC_PER_SEC = 1_000_000

# Interpreter with a working PyAV (13.x). Fallback: the running interpreter.
RETIME_PY = Path(os.environ.get("AFL_CAM_RETIME_PY", "/home/hamish/venvs/cv2-new/bin/python3"))


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


def retime_with_pts(src: Path, pts_us: np.ndarray, out: Path) -> int:
    """Mux `src` (raw .h264 or .mp4) into `out` with timestamps from `pts_us`.

    One timestamp per frame — matches rkvenc streams, which have no B-frames
    (pts == dts). The first frame lands at t=0; the rest keep the sidecar's
    spacing, so the container duration equals the real capture span.
    Returns the number of frames muxed, or -1 on failure.
    """
    import av

    pts_us = np.asarray(pts_us, dtype=np.int64)
    delta = np.diff(pts_us)
    if len(pts_us) > 1 and np.any(delta <= 0):
        log.warning("retime: %s pts not strictly increasing — enforcing", src.name)
        pts_us = np.maximum.accumulate(pts_us)

    fmt = "h264" if src.suffix == ".h264" else None
    try:
        # Pass 1: count packets so the pts list can be reconciled.
        with av.open(str(src), mode="r", format=fmt) as in_cont:
            stream = in_cont.streams.video[0]
            n_pkt = sum(1 for p in in_cont.demux(stream) if p.size > 0)

        n_pts = len(pts_us)
        if n_pkt != n_pts:
            log.warning("retime: %s has %d packets but %d pts entries — %s",
                        src.name, n_pkt, n_pts,
                        "dropping trailing pts" if n_pts > n_pkt
                        else "extending the last interval")
            if n_pts > n_pkt:
                pts_us = pts_us[:n_pkt]
            else:
                step = np.median(delta) if len(delta) else USEC_PER_SEC // 30
                pts_us = np.append(pts_us,
                                   pts_us[-1] + step * np.arange(1, n_pkt - n_pts + 1))

        origin = int(pts_us[0])
        tmp = out.with_suffix(out.suffix + ".tmp")
        # Pass 2: demux + mux in one pass with the INPUT KEPT OPEN — PyAV
        # packets reference the container's buffers, so closing it first
        # corrupts the data.
        with av.open(str(src), mode="r", format=fmt) as in_cont, \
                av.open(str(tmp), mode="w", format="mp4") as out_cont:
            stream = in_cont.streams.video[0]
            # Stream copy from a template: mirrors the codec params correctly
            # (manual codec_context assignments produce a broken codecpar).
            out_stream = out_cont.add_stream(template=stream)
            # Packet pts are expressed in the DEMUXER's time_base (the muxer
            # rescales from the packet's own time_base to the output's).
            tb = stream.time_base  # seconds per tick
            to_ticks = lambda us: int(round(us * tb.denominator /
                                           (tb.numerator * USEC_PER_SEC)))
            i = 0
            for pkt in in_cont.demux(stream):
                if pkt.size <= 0:
                    continue
                pkt.stream = out_stream
                pkt.pts = to_ticks(int(pts_us[i]) - origin)
                pkt.dts = pkt.pts  # no B-frames: pts == dts
                out_cont.mux(pkt)
                i += 1
        tmp.replace(out)
        return n_pkt
    except av.error.FFmpegError as exc:
        log.error("retime: mux failed for %s: %s", src.name, exc)
        tmp.unlink(missing_ok=True)
        return -1


def retime_file(src: Path, pts_path: Path, out: Path) -> bool:
    """Retime `src` using the `.pts` sidecar at `pts_path` → `out`.

    Runs under the RETIME_PY interpreter (working PyAV) via subprocess so
    callers in other environments never import PyAV in-process.
    """
    if not pts_path.exists():
        log.warning("retime: no sidecar %s — leaving %s at fixed fps", pts_path.name, src.name)
        return False
    if RETIME_PY.exists() and str(RETIME_PY) != sys.executable:
        result = subprocess.run(
            [str(RETIME_PY), "-m", "afl_cam.retime",
             str(src), str(pts_path), str(out)],
            cwd=str(Path(__file__).resolve().parent.parent),
            capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            log.error("retime: %s", result.stderr.strip() or result.stdout.strip())
            return False
        return True
    pts = load_pts(pts_path)
    if pts is None:
        return False
    return retime_with_pts(src, pts, out) >= 0


def main() -> int:
    """CLI: `python -m afl_cam.retime <src> <pts> <out>` (used by retime_file)."""
    if len(sys.argv) != 4:
        print(f"usage: {sys.argv[0]} <src(.h264|.mp4)> <pts> <out.mp4>", file=sys.stderr)
        return 2
    src, pts_path, out = (Path(a) for a in sys.argv[1:])
    pts = load_pts(pts_path)
    if pts is None:
        print(f"retime: cannot read pts from {pts_path}", file=sys.stderr)
        return 1
    n = retime_with_pts(src, pts, out)
    print(f"retime: {src.name} -> {out.name}: {n} frames")
    return 0 if n >= 0 else 1


if __name__ == "__main__":
    sys.exit(main())
