"""Build the 30→15 fps elementary-stream fixtures the finalize tests mux.

Run under the PyAV interpreter, which is the only one that has `av`:

    $AFL_CAM_RETIME_PY tests/unit/es_fixture.py <out_dir>

Writes `h264_30to15.264`, `hevc_30to15.h265` and a `.pts` sidecar each: 45
frames, one second at 30 fps then one second at 15 fps — 2.00 s of real
time. That rate change is what auto-exposure does on the rig, and it is
exactly what a median frame rate gets wrong: the median interval is 33333
us (30 fps), so a CFR mux of this stream would claim 1.50 s for 2.00 s.

Like the rig, the ENCODER runs at a fixed rate — only the capture spacing
in the sidecar changes — so the elementary stream is encoded at a steady
30 fps and the 15 fps second lives in the timestamps.

The stamps start at a plausible kernel-monotonic reading (the board's are
weeks of uptime in us), so the offset arithmetic is exercised on realistic
magnitudes rather than from zero.
"""

from __future__ import annotations

import sys
from fractions import Fraction
from pathlib import Path

import av
import numpy as np

WIDTH, HEIGHT = 320, 240
BASE_US = 1_700_000_000_000        # a plausible monotonic clock reading
ENCODE_FPS = 30
RATES = ((30, 30), (15, 15))       # (fps, frames) — a second of each

# No B-frames: the recorders (rkvenc) emit none, so pts == dts on every
# packet. A fixture with reordering would not be the stream the muxer gets,
# and would hide a retimer that assumes decode order == display order.
FIXTURES = (("h264", "libx264", {}),
            ("hevc", "libx265", {"x265-params": "log-level=error"}))


def _frame(i: int) -> av.VideoFrame:
    """A distinguishable frame (flat Y planes all compress to nothing)."""
    row = ((np.arange(WIDTH, dtype=np.uint16) + i * 4) % 224 + 16).astype(
        np.uint8)
    buf = np.full((HEIGHT * 3 // 2, WIDTH), 128, dtype=np.uint8)
    buf[:HEIGHT] = row              # broadcast over the rows
    return av.VideoFrame.from_ndarray(buf, format="yuv420p")


def timestamps() -> list[int]:
    """One stamp per frame: a 30 fps second then a 15 fps second."""
    pts, t = [], BASE_US
    for fps, count in RATES:
        step = 1_000_000 // fps
        pts += [t + k * step for k in range(count)]
        t += count * step
    return pts


def _no_reordering(name: str, pkt: av.Packet) -> None:
    """The muxer assigns each packet the next sidecar stamp, so a stream
    with reordering (pts != dts) would come out shuffled."""
    if pkt.pts != pkt.dts:
        raise SystemExit(
            f"{name}: encoder emitted pts {pkt.pts} != dts {pkt.dts} "
            "(B-frames?) — the fixture must match rkvenc, which has none")


def build(out_dir: Path) -> None:
    """Write the fixtures and their shared sidecar into `out_dir`."""
    out_dir.mkdir(parents=True, exist_ok=True)
    pts = timestamps()
    for name, encoder, options in FIXTURES:
        es = out_dir / f"{name}_30to15.{'264' if name == 'h264' else 'h265'}"
        with av.open(str(es), "w", format=name) as out:
            stream = out.add_stream(encoder, rate=ENCODE_FPS,
                                    options={"bf": "0", **options})
            stream.width, stream.height = WIDTH, HEIGHT
            stream.pix_fmt = "yuv420p"
            for i in range(len(pts)):
                frame = _frame(i)
                frame.pts = i
                frame.time_base = Fraction(1, ENCODE_FPS)
                for pkt in stream.encode(frame):
                    _no_reordering(name, pkt)
                    out.mux(pkt)
            for pkt in stream.encode(None):     # flush the encoder
                _no_reordering(name, pkt)
                out.mux(pkt)
        (out_dir / f"{name}_30to15.pts").write_text(
            "".join(f"{t}\n" for t in pts))


def main() -> int:
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} <out_dir>", file=sys.stderr)
        return 2
    build(Path(sys.argv[1]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
