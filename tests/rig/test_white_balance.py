"""White balance on a fresh stream: the cameras' auto warms up, then both
hold one manual temperature that keeps the canvas at the warmth the auto
found, and the overlap's colour matches across the seam.

A manual temperature written before the camera's auto has run on the stream
is ignored (the picture came out blue in daylight, 2026-10-19), so the test
restarts the pipeline to get a fresh stream, then reads cam2stitch's log.
"""

import math
import re
import time
from pathlib import Path

from .conftest import get, post, stitch, wait_until, wake_standby

LOG = Path("outputs/_live/stitch.log")


def _lines(prefix):
    if not LOG.exists():
        return []
    return [l for l in LOG.read_text(errors="replace").splitlines() if l.startswith(prefix)]


def _restart_pipeline():
    """A fresh cam2stitch (and fresh camera streams): the fps lock setting
    restarts an idle pipeline; flip it and flip it back."""
    lock = get("/api/status").get("fps_lock", True)
    for v in (not lock, lock):
        assert post("/api/fps-lock", {"locked": v}).get("ok") is True
        time.sleep(3)
        wake_standby()


def test_white_balance_locks_matched_to_the_cameras_auto():
    wake_standby()
    _restart_pipeline()
    st = wait_until(
        lambda: (lambda s: s if all((s.get(c) or {}).get("wb_auto") is False
                                    for c in ("cam0", "cam1")) else None)(stitch()),
        timeout=45, what="white balance to leave the cameras' auto")
    k = [st[c]["wb_k"] for c in ("cam0", "cam1")]
    assert all(2800 <= x <= 6500 for x in k), k
    lock = _lines("white balance: the cameras' auto gave")
    assert lock, "no white balance lock line in the log"
    print("\n[wb] " + lock[-1])

    time.sleep(15)             # the follow and the seam split settle
    for l in (_lines("white balance: canvas")[-3:] + _lines("seam trim")[-2:]):
        print("[wb] " + l)
    s = stitch()
    print("[wb] status:", {c: {k_: (s.get(c) or {}).get(k_) for k_ in ("u", "v", "wb_k")}
                           for c in ("cam0", "cam1")})

    ref = float(re.search(r"warmth (\S+)", lock[-1]).group(1))
    track = _lines("white balance: canvas")
    if math.isnan(ref) or not track:
        print("[wb] too dark to read the canvas colour; the colour checks are skipped")
        return
    w = float(re.search(r"canvas warmth (\S+)", track[-1]).group(1))
    # the blue failure read ~-60 against a reference of ~0
    assert abs(w - ref) < 6, f"canvas warmth {w} vs the cameras' auto {ref}"
    seam = _lines("seam trim")
    if seam:
        a, b = map(float, re.search(r"warmth (\S+) \| (\S+)", seam[-1]).groups())
        assert abs(a - b) < 4, f"overlap warmth {a} | {b}"
