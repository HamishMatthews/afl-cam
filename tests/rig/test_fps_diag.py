"""Diagnostic for #55: why do the cameras run at ~18 fps in a bright scene?

Prints (run with -s or read the pytest log): the cameras' V4L2 controls, the
exposure decision cam2stitch logged, per-second frame rates and pairing
counters, and the frame rate with the UVC "dynamic framerate" priority off.
It only asserts that frames flow; the numbers are the point.
"""

import re
import subprocess
import time
from pathlib import Path

from afl_cam.config import CAMERAS

from .conftest import stitch, wake_standby


def _devs():
    out = {}
    for k, c in (CAMERAS or {}).items():
        d = c.get("device") if isinstance(c, dict) else c
        if d:
            out[k] = d
    return out


def _v4l(dev, *args):
    r = subprocess.run(["v4l2-ctl", "-d", str(dev), *args], capture_output=True, text=True)
    return (r.stdout + r.stderr).strip()


def _rate(seconds=10):
    """Per-camera fps and the pairing counters over `seconds`."""
    a = stitch()
    t0 = time.time()
    time.sleep(seconds)
    b = stitch()
    dt = time.time() - t0
    f = lambda s, k: (s.get(k) or {}).get("frames", 0)
    return {
        "cam0_fps": round((f(b, "cam0") - f(a, "cam0")) / dt, 1),
        "cam1_fps": round((f(b, "cam1") - f(a, "cam1")) / dt, 1),
        "paired": b.get("paired", 0) - a.get("paired", 0),
        "dup": b.get("dup", 0) - a.get("dup", 0),
        "skip": b.get("skip", 0) - a.get("skip", 0),
        "skew_ms": round(b.get("skew_avg_us", -1) / 1000, 1),
        "errors": [(b.get(k) or {}).get("errors") for k in ("cam0", "cam1")],
    }


def test_fps_diagnostic():
    wake_standby()
    devs = _devs()
    print("\n[fps-diag] devices:", devs)
    for k, d in devs.items():
        print(f"[fps-diag] {k} controls:\n" + _v4l(d, "-L"))
        print(f"[fps-diag] {k} format/fps:\n" + _v4l(d, "--get-fmt-video", "--get-parm"))
    log = Path("rig-daemon.log")
    if log.exists():
        lines = [l for l in log.read_text(errors="replace").splitlines()
                 if re.search(r"exposure|fps|frame interval|S_PARM", l)]
        print("[fps-diag] daemon log, exposure lines:\n" + "\n".join(lines[-30:]))
    time.sleep(5)
    base = _rate()
    print("[fps-diag] baseline (auto exposure as the daemon runs it):", base)

    changed = {}
    for k, d in devs.items():
        if "exposure_dynamic_framerate" in _v4l(d, "-L"):
            changed[k] = d
            print(f"[fps-diag] {k}:", _v4l(d, "--set-ctrl=exposure_dynamic_framerate=0"))
    if changed:
        time.sleep(3)
        print("[fps-diag] dynamic framerate OFF:", _rate())
        for d in changed.values():
            _v4l(d, "--set-ctrl=exposure_dynamic_framerate=1")
    else:
        print("[fps-diag] no exposure_dynamic_framerate control on these cameras")

    for k, d in devs.items():
        print(f"[fps-diag] {k} exposure now:",
              _v4l(d, "--get-ctrl=auto_exposure,exposure_time_absolute"))
    assert base["cam0_fps"] > 0 and base["cam1_fps"] > 0
