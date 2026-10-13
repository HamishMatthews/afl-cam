"""Timing for recordings, from the recorder's kernel-timestamp `.pts` sidecar.

The native recorders (cam2h264, cam2stitch) stamp every encoded frame with
the kernel monotonic clock (us) into a `.pts` sidecar — the SAME clock for
both cameras, sampled when the frame completes at the xhci controller. At
auto-exposure the sensor's rate varies (e.g. 50 ms exposure → 20 fps), so a
fixed-30 fps container plays the footage too fast.

Two jobs, two rules (2026-10-13, extending bfc75b6):

* **Per-camera raws the stitcher PAIRS stay CFR** at the median measured
  rate (`mux_with_pts_rate`): cv2/ffmpeg/ffprobe read VFR mp4s
  inconsistently, which silently broke frame pairing — per-frame alignment
  stays in the sidecar, where the stitcher reads it.
* **Final deliverables a human watches carry the sidecar's real per-frame
  timing** (`retime_file`: stitch.mp4, proxy.mp4). Their duration and
  playback speed then match the game even when the rate changed mid-game,
  and the camera audio muxed in alongside stays in sync instead of drifting
  against a median rate. Nothing pairs frames from them.

Neither path re-encodes: both stream-copy.

The VFR mux needs PyAV, so `retime_file` shells out to `RETIME_PY` (av
13.1.0 in the cv2-new venv works, av 18 segfaults in add_stream, and the
project venv is Python 3.14 with no av wheels). It NEVER falls back to a
fixed rate: a mux that cannot use the sidecar fails and the raw files stay
on disk — falling back to 30 fps once shipped 1.55x-fast footage
(session 213137).
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Iterator, Optional, Sequence

import numpy as np

log = logging.getLogger(__name__)

USEC_PER_SEC = 1_000_000

# The board's interpreter with a working PyAV (13.x). Override in tests/dev
# machines; without it the running interpreter is used (see
# retime_interpreter), so `av` has to be importable there.
RETIME_PY = Path(os.environ.get(
    "AFL_CAM_RETIME_PY", "/home/hamish/venvs/cv2-new/bin/python3"))

# A sidecar shorter than the stream by a few frames is a tail the writer
# missed; extrapolate those and warn. Short by more means the sidecar is
# truncated, so the tail's timing would be fiction — fail the mux instead
# of inventing a duration.
MAX_EXTRAPOLATED_FRAMES = 5

# A master copy is tens of GB (80 Mbps ≈ 36 GB/h): the mux is I/O bound.
MUX_TIMEOUT_S = 3600


def load_pts(path: Path) -> Optional[np.ndarray]:
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
                      fallback_fps: float = 30.0,
                      codec: str = "h264",
                      faststart: bool = False) -> bool:
    """Mux the raw .h264 ES into .mp4 as CFR at the measured real rate.

    Stream copy (no re-encode) — the container timestamps are regular at the
    median measured frame rate, so playback speed = real-life speed. The
    `.pts` sidecar remains the exact per-frame timing for the stitcher.

    ``codec="hevc"`` tags the stream hvc1 (not hev1): iPhone/QuickTime
    refuse to play hev1-tagged HEVC (docs/archive/iphone-control-plan.md §4).

    ``faststart=True`` moves the moov atom to the front — needed for
    HTTP streaming (Safari's Range/206 requirement).
    """
    pts = load_pts(pts_path)
    fps = measured_fps(pts, fallback_fps) if pts is not None else fallback_fps
    args = [
        # NOTE: -r, not -framerate — ffmpeg 8's raw h264 demuxer ignores
        # -framerate and stamps 30 fps regardless
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "hevc" if codec == "hevc" else "h264",
        "-r", f"{fps:.4f}", "-i", str(raw),
        "-c", "copy"]
    if codec == "hevc":
        args += ["-tag:v", "hvc1"]
    if faststart:
        args += ["-movflags", "+faststart"]
    args.append(str(out))
    result = subprocess.run(args, capture_output=True, text=True, timeout=60)
    if result.returncode == 0 and out.exists() and out.stat().st_size > 0:
        return True
    log.error("mux failed for %s: %s", raw.name, result.stderr.strip())
    return False


def _interleaved(*sources: Iterator[tuple[float, object]]) -> Iterator[object]:
    """Merge (time, packet) sources into ascending time order.

    The mp4 muxer only reorders within ~10 s of buffer, so feeding a whole
    track first leaves the other track at the end of mdat: Safari would then
    need a second byte range to play the proxy's audio.
    """
    heads = [[next(src, None), src] for src in sources]
    while True:
        live = [h for h in heads if h[0] is not None]
        if not live:
            return
        head = min(live, key=lambda h: h[0][0])
        yield head[0][1]
        head[0] = next(head[1], None)


def retime_with_pts(src: Path, pts_us: np.ndarray, out: Path,
                    codec: str = "h264",
                    tag: Optional[str] = None,
                    faststart: bool = False,
                    audio: Optional[Path] = None) -> int:
    """Mux `src` (raw .h264/.hevc ES) into `out` with the sidecar's real
    per-frame timing — VFR, so duration and playback speed are real time.

    One timestamp per frame, matching the recorders' streams (no B-frames:
    pts == dts). The first frame lands at t=0 and the rest keep the
    sidecar's spacing. `audio` is an already-encoded AAC file aligned to the
    video's first frame; it is copied in as-is. Returns the number of frames
    muxed, or -1 on failure (nothing is left at `out`).
    """
    import av

    pts_us = np.asarray(pts_us, dtype=np.int64)
    if len(pts_us) == 0:
        log.error("retime: empty .pts sidecar for %s", src.name)
        return -1
    if len(pts_us) > 1 and np.any(np.diff(pts_us) <= 0):
        log.warning("retime: %s pts not strictly increasing — enforcing",
                    src.name)
        pts_us = np.maximum.accumulate(pts_us)
    n_pts = len(pts_us)

    tmp = out.with_name(out.name + ".tmp")
    fmt = "hevc" if codec == "hevc" else "h264"
    opts = {"movflags": "+faststart"} if faststart else {}
    extra_frames = 0
    try:
        with av.open(str(src), mode="r", format=fmt) as in_cont, \
                av.open(str(tmp), mode="w", format="mp4", options=opts) as out_cont:
            v_in = in_cont.streams.video[0]
            v_out = out_cont.add_stream(template=v_in)
            if tag:
                # av 13: the template stream is a decoder context, so the
                # tag goes on the output stream's codec context
                v_out.codec_context.codec_tag = tag
            tb = v_in.time_base
            scale = tb.denominator / (tb.numerator * USEC_PER_SEC)
            origin = int(round(int(pts_us[0]) * scale))
            step = int(pts_us[-1] - pts_us[-2]) if n_pts > 1 else \
                USEC_PER_SEC // 30

            def video():
                nonlocal extra_frames
                i = -1
                for pkt in in_cont.demux(v_in):
                    if pkt.size <= 0:
                        continue
                    i += 1
                    if i < n_pts:
                        t_us = int(pts_us[i])
                    else:  # a few frames past the sidecar: keep them
                        extra_frames += 1
                        t_us = int(pts_us[-1]) + step * (i - n_pts + 1)
                    pkt.stream = v_out
                    pkt.pts = pkt.dts = int(round(t_us * scale)) - origin
                    yield (t_us - int(pts_us[0])) / USEC_PER_SEC, pkt

            if audio is None:
                for pkt in _interleaved(video()):
                    out_cont.mux(pkt)
            else:
                with av.open(str(audio)) as a_cont:
                    a_in = a_cont.streams.audio[0]
                    a_out = out_cont.add_stream(
                        "aac", rate=a_in.codec_context.rate)

                    def sound():
                        for pkt in a_cont.demux(a_in):
                            if pkt.size <= 0:
                                continue
                            if pkt.pts is None:
                                raise ValueError(
                                    f"{audio.name}: audio packet without a "
                                    "timestamp")
                            pkt.stream = a_out
                            yield float(pkt.pts * a_in.time_base), pkt

                    for pkt in _interleaved(video(), sound()):
                        out_cont.mux(pkt)

        if extra_frames > MAX_EXTRAPOLATED_FRAMES:
            raise ValueError(
                f"{src.name} has {extra_frames} frames past the .pts sidecar "
                f"({n_pts} stamps) — the sidecar looks truncated")
        tmp.replace(out)
        return n_pts + extra_frames
    except (av.error.FFmpegError, OSError, ValueError, IndexError) as exc:
        log.error("retime: mux failed for %s: %s", src.name, exc)
        tmp.unlink(missing_ok=True)
        return -1


def retime_interpreter() -> Path:
    """The interpreter the VFR mux runs under.

    The board's PyAV venv when it is installed, else this interpreter —
    which then needs `av` itself (it is a dev dependency, so tests and a
    dev machine can mux without the board).
    """
    return RETIME_PY if RETIME_PY.exists() else Path(sys.executable)


def retime_file(src: Path, pts_path: Path, out: Path,
                codec: str = "h264",
                tag: Optional[str] = None,
                faststart: bool = False,
                audio: Optional[Path] = None) -> bool:
    """Retime `src` using the `.pts` sidecar at `pts_path` → `out`.

    Runs under retime_interpreter() via subprocess so callers in other
    environments never import PyAV in-process. False when the mux could not
    use the sidecar — callers must not treat that as "muxed at a rate".
    """
    if not pts_path.is_file():
        log.error("retime: no .pts sidecar for %s — refusing to guess the "
                  "frame rate", src.name)
        return False
    args = [str(retime_interpreter()), "-m", "afl_cam.retime",
            str(src), str(pts_path), str(out), "--codec", codec]
    if tag:
        args += ["--tag", tag]
    if faststart:
        args.append("--faststart")
    if audio is not None:
        args += ["--audio", str(audio)]
    result = subprocess.run(
        args, cwd=str(Path(__file__).resolve().parent.parent),
        capture_output=True, text=True, timeout=MUX_TIMEOUT_S)
    if result.returncode != 0:
        log.error("retime: %s", result.stderr.strip() or result.stdout.strip())
        return False
    return out.is_file() and out.stat().st_size > 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI: `python -m afl_cam.retime <src> <pts> <out> [options]`."""
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("src", type=Path, help="raw .h264/.hevc elementary stream")
    ap.add_argument("pts", type=Path, help=".pts sidecar (kernel us)")
    ap.add_argument("out", type=Path, help="output .mp4")
    ap.add_argument("--codec", choices=("h264", "hevc"), default="h264")
    ap.add_argument("--tag", help="codec tag for the output stream (hvc1)")
    ap.add_argument("--faststart", action="store_true",
                    help="move the moov atom to the front (HTTP streaming)")
    ap.add_argument("--audio", type=Path,
                    help="AAC file to copy in (already aligned to frame 0)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    pts = load_pts(args.pts)
    if pts is None:
        print(f"retime: cannot read pts from {args.pts}", file=sys.stderr)
        return 1
    n = retime_with_pts(args.src, pts, args.out, codec=args.codec,
                        tag=args.tag, faststart=args.faststart,
                        audio=args.audio)
    if n < 0:
        return 1
    print(f"retime: {args.src.name} -> {args.out.name}: {n} frames")
    return 0


if __name__ == "__main__":
    sys.exit(main())
