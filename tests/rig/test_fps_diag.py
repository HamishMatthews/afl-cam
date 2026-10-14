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
        d = c if isinstance(c, str) else c.get("device")
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
        # cam2stitch capture timing, averages since start: slot_ms = pipeline
        # behind, dq_ms = waiting on the camera, dec_ms = jpegd, dt_ms = camera
        # timestamp interval, gap50 = intervals over 50 ms, seq_lost = driver drops
        "cam0_t": {k: v for k, v in (b.get("cam0") or {}).items() if k not in ("frames", "errors")},
        "cam1_t": {k: v for k, v in (b.get("cam1") or {}).items() if k not in ("frames", "errors")},
    }


def test_fps_diagnostic(capsys):
    with capsys.disabled():      # pytest hides prints of passing tests; CI logs need these
        _diagnose()


def _diagnose():
    wake_standby()
    devs = _devs()
    print("\n[fps-diag] devices:", devs)
    log = Path("outputs/_live/stitch.log")
    if log.exists():
        lines = [l for l in log.read_text(errors="replace").splitlines()
                 if re.search(r"exposure|fps|frame interval|S_PARM", l)]
        print("[fps-diag] daemon log, exposure lines:\n" + "\n".join(lines[-30:]))
    for k, d in list(devs.items())[:1]:
        # every control the camera offers (is there a gain / brightness / gamma lever?)
        print(f"[fps-diag] {k} controls:\n" + "\n".join(
            l.strip()[:110] for l in _v4l(d, "-L").splitlines() if "(" in l or "value=" in l))
    time.sleep(5)
    base = _rate()
    # which threads of cam2stitch burn CPU, and where the USB IRQs land
    top = subprocess.run("top -H -b -n 1 -w 200 | head -25", shell=True,
                         capture_output=True, text=True).stdout
    print("[fps-diag] busiest threads:\n" + top)
    irq = subprocess.run("grep -E 'xhci|dwc3' /proc/interrupts | cut -c1-120",
                         shell=True, capture_output=True, text=True).stdout
    print("[fps-diag] usb interrupts per core:\n" + irq)
    if log.exists():
        loops = [l for l in log.read_text(errors="replace").splitlines() if l.startswith("loop-ms")]
        print("[fps-diag] main loop (ms per iteration, last 6):\n" + "\n".join(loops[-6:]))
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

    # frame rate against a fixed exposure (100 us units): is the rate
    # exposure-limited? Both cameras at once, then back to auto.
    for exp in (100, 300):
        for d in devs.values():
            _v4l(d, "--set-ctrl=auto_exposure=1", f"--set-ctrl=exposure_time_absolute={exp}")
        time.sleep(3)
        print(f"[fps-diag] manual exposure {exp} ({exp / 10:.0f} ms):", _rate(6))
    for d in devs.values():
        _v4l(d, "--set-ctrl=auto_exposure=3")
    time.sleep(3)
    print("[fps-diag] back to auto:", _rate(6))

    # does the camera's own image processing lift a dim picture at a fixed
    # exposure? mean luma (cam2stitch samples every 30th frame) and fps for
    # each setting, with the exposure at 30 ms
    for d in devs.values():
        _v4l(d, "--set-ctrl=auto_exposure=1", "--set-ctrl=exposure_time_absolute=300")
    for name, vals in (("gamma", (100, 160, 220)), ("brightness", (0, 32, 64))):
        for v in vals:
            for d in devs.values():
                _v4l(d, f"--set-ctrl={name}={v}")
            time.sleep(4)
            r = _rate(5)
            s0, s1 = stitch().get("cam0", {}), stitch().get("cam1", {})
            print(f"[fps-diag] {name}={v}: luma {s0.get('luma')} / {s1.get('luma')}, "
                  f"fps {r['cam0_fps']} / {r['cam1_fps']}")
        for d in devs.values():
            _v4l(d, f"--set-ctrl={name}={0 if name == 'brightness' else 100}")
    for d in devs.values():
        _v4l(d, "--set-ctrl=auto_exposure=3")

    # colour balance: mean U/V of each camera (128 = neutral) with the
    # camera's own auto white balance, then both fixed at the same temperature
    def _uv(tag):
        a, b = stitch().get("cam0", {}), stitch().get("cam1", {})
        print(f"[fps-diag] wb {tag}: cam0 u/v {a.get('u')}/{a.get('v')}  "
              f"cam1 u/v {b.get('u')}/{b.get('v')}")
    time.sleep(3)
    _uv("auto")
    for t in (4000, 5500, 6500):
        for d in devs.values():
            _v4l(d, "--set-ctrl=white_balance_automatic=0",
                 f"--set-ctrl=white_balance_temperature={t}")
        time.sleep(4)
        _uv(f"fixed {t} K")
    for d in devs.values():
        _v4l(d, "--set-ctrl=white_balance_automatic=1")

    for k, d in devs.items():
        print(f"[fps-diag] {k} exposure now:",
              _v4l(d, "--get-ctrl=auto_exposure,exposure_time_absolute"))
    assert base["cam0_fps"] > 0 and base["cam1_fps"] > 0
