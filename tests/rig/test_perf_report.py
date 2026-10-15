"""Perf report (#7): a fixed window of numbers from the running pipeline,
written to rig-artifacts/perf/perf.json (+ summary.md for the job summary) so
a PR's performance claim comes from the rig. The standby window always runs;
the recording window only on the slow suite. Light level (mean luma) is in
the report so a dim scene's lower fps isn't read as a regression."""

import glob
import json
import os
import re
import subprocess
import time
from pathlib import Path

import pytest

from .conftest import post, status, stitch, wait_until, wake_standby

OUT = Path("rig-artifacts/perf")
WINDOW_S = 20


def _cpu_ticks():
    """utime+stime of every cam2stitch thread group, in clock ticks."""
    pids = subprocess.run(["pgrep", "-x", "cam2stitch"], capture_output=True,
                          text=True).stdout.split()
    total = 0
    for pid in pids:
        try:
            f = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
            total += int(f[11]) + int(f[12])
        except (OSError, IndexError, ValueError):
            pass
    return total


def _temps_c():
    out = {}
    for p in glob.glob("/sys/class/thermal/thermal_zone*/temp"):
        try:
            name = Path(p).parent.joinpath("type").read_text().strip()
            out[name] = round(int(Path(p).read_text()) / 1000, 1)
        except (OSError, ValueError):
            pass
    return out


def _loop_ms():
    """Averages of the last loop-ms lines cam2stitch logged (stage timers)."""
    log = Path("outputs/_live/stitch.log")
    if not log.exists():
        return {}
    rows = [l for l in log.read_text(errors="replace").splitlines()
            if l.startswith("loop-ms")][-6:]
    acc = {}
    for l in rows:
        for k, v in re.findall(r"(\w+) ([0-9.]+)", l.replace("loop-ms", "")):
            acc.setdefault(k, []).append(float(v))
    return {k: round(sum(v) / len(v), 1) for k, v in acc.items()}


def _window(label):
    a, t0, c0 = stitch(), time.time(), _cpu_ticks()
    time.sleep(WINDOW_S)
    b, t1, c1 = stitch(), time.time(), _cpu_ticks()
    # the pipeline's own clock when it reports one, else wall time
    dt = ((b.get("t_us", 0) - a.get("t_us", 0)) / 1e6) or (t1 - t0)

    def d(path):
        def pick(s):
            for k in path:
                s = (s or {}).get(k) or {}
            return s if isinstance(s, (int, float)) else 0
        return pick(b) - pick(a)

    ticks = os.sysconf("SC_CLK_TCK")
    return {
        "window": label,
        "seconds": round(dt, 1),
        "cam0_fps": round(d(["cam0", "frames"]) / dt, 1),
        "cam1_fps": round(d(["cam1", "frames"]) / dt, 1),
        "paired_fps": round(d(["paired"]) / dt, 1),
        "dup": d(["dup"]), "skip": d(["skip"]),
        "errors": [d(["cam0", "errors"]), d(["cam1", "errors"])],
        "seq_lost": [(b.get(k) or {}).get("seq_lost") for k in ("cam0", "cam1")],
        "skew_ms": round((b.get("skew_avg_us") or 0) / 1000, 1),
        "enc_put": d(["enc", "put"]), "enc_drained": d(["enc", "drained"]),
        "preview_drops": d(["preview", "drops"]),
        "loop_ms": _loop_ms(),
        "cam2stitch_cpu_pct": round((c1 - c0) / ticks / dt * 100),
        "luma": [(b.get(k) or {}).get("luma") for k in ("cam0", "cam1")],
        "temps_c": _temps_c(),
    }


def _write(windows):
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "perf.json").write_text(json.dumps(
        {"sha": os.environ.get("GITHUB_SHA", ""), "windows": windows}, indent=1))
    rows = ["| window | cam0/cam1 fps | paired fps | dup/skip | skew ms | enc put/drained "
            "| loop iter ms | cam2stitch CPU % | luma |", "|---|---|---|---|---|---|---|---|---|"]
    for w in windows:
        rows.append(
            f"| {w['window']} | {w['cam0_fps']} / {w['cam1_fps']} | {w['paired_fps']} "
            f"| {w['dup']} / {w['skip']} | {w['skew_ms']} | {w['enc_put']} / {w['enc_drained']} "
            f"| {w['loop_ms'].get('iter', '-')} | {w['cam2stitch_cpu_pct']} "
            f"| {w['luma'][0]} / {w['luma'][1]} |")
    (OUT / "summary.md").write_text("### Rig perf\n\n" + "\n".join(rows) + "\n")


def test_perf_report_standby():
    wake_standby()
    time.sleep(5)                       # let exposure settle
    w = _window("standby")
    _write([w])
    print("[perf]", json.dumps(w))
    assert w["cam0_fps"] > 0 and w["cam1_fps"] > 0


@pytest.mark.slow
def test_perf_report_recording():
    wake_standby()
    standby = _window("standby")
    assert post("/api/record/start", {"request_id": "perf-report"}).get("ok")
    wait_until(lambda: status().get("state") == "recording", 30, what="recording")
    time.sleep(5)
    rec = _window("recording")
    assert post("/api/record/stop", {"request_id": "perf-report-stop"}).get("ok")
    wait_until(lambda: status().get("state") == "standby", 180, what="standby after finalize")
    _write([standby, rec])
    print("[perf]", json.dumps(rec))
    assert rec["cam0_fps"] > 0 and rec["enc_drained"] > 0
