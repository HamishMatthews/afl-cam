"""Re-align from the phone: the daemon closes the pipeline, runs the
re-align chain with its output kept for the page, and scores the new
stills against the old and the new map set. The chain and the scorer are
stand-in scripts here (the real ones need the board's cameras + OpenCV 5)."""

import json
import sys
import textwrap
import time

import pytest

from afl_cam import daemon as d

FAKE_ALIGN = """
import sys
from pathlib import Path
out = Path(sys.argv[1])
print("cam0: captured 900 KB")
print("cam1: captured 880 KB")
if "{fail}" == "1":
    sys.exit(3)
out.mkdir(parents=True, exist_ok=True)
for name in ("cam0.rgb", "cam1.rgb", "params.bin"):
    (out / name).write_bytes(b"new")
print("alignment frozen")
"""

FAKE_SCORE = """
import json, sys
from pathlib import Path
args = sys.argv[1:]
maps = Path(args[args.index("--maps") + 1])
new = (maps / "params.bin").read_bytes() == b"new"
if "--crop" in args:
    Path(args[args.index("--crop") + 1]).write_bytes(b"jpeg")
print(json.dumps({"misalign_median": 2.0 if new else 9.0,
                  "misalign_p95": 5.0 if new else 30.0,
                  "misalign_tiles": 40, "luma_ratio": 1.1, "luma_step": 3.0,
                  "canvas": [96, 48]}, indent=2))
"""


@pytest.fixture
def rig(tmp_path, monkeypatch):
    live = tmp_path / "live"
    live.mkdir()
    for name in d.MAP_FILES:
        (live / name).write_bytes(b"old")
    out = tmp_path / "out"
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "score.py").write_text(textwrap.dedent(FAKE_SCORE))
    monkeypatch.setattr(d, "ALIGN_PYTHON", sys.executable)
    monkeypatch.setattr(d, "SCORE_SCRIPT", scripts / "score.py")
    monkeypatch.setattr(d, "STITCH_PARAMS_DIR", live)
    monkeypatch.setattr(d, "LIVE_SNAP_DIR", tmp_path / "snap")

    def use_align(fail=False):
        path = scripts / "align.py"
        path.write_text(textwrap.dedent(FAKE_ALIGN.replace("{fail}", "1" if fail else "0")))
        monkeypatch.setattr(d, "ALIGN_SCRIPT", path)
    use_align()
    return out, use_align


class StubDaemon:
    def __init__(self):
        self.done = []

    def on_realign_done(self, job):
        self.done.append(job)


def run_job(out):
    stub = StubDaemon()
    job = d.RealignJob(stub, out)
    job.start()
    job.join(30)
    assert stub.done == [job]
    return job


def test_realign_scores_before_and_after(rig):
    out, _ = rig
    job = run_job(out)
    assert job.ok is True and job.error is None
    lines = job.lines()
    assert "cam0: captured 900 KB" in lines and "alignment frozen" in lines
    assert job.before["misalign_median"] == 9.0      # new stills, old maps
    assert job.after["misalign_median"] == 2.0       # new stills, new maps
    assert set(job.after) == set(d.RealignJob.SCORE_KEYS)
    s = job.summary()
    assert s["running"] is False and s["has_crop"] is True
    assert s["log_tail"][-1].startswith("✓")
    json.dumps(s)                                     # goes out over the WS


def test_realign_failure_keeps_old_alignment(rig):
    out, use_align = rig
    use_align(fail=True)
    job = run_job(out)
    assert job.ok is False
    assert "exit 3" in job.error and "previous alignment" in job.error
    assert job.after is None and not job.summary()["has_crop"]


@pytest.fixture
def cam_daemon(tmp_path, monkeypatch):
    monkeypatch.setattr(d, "DB_PATH", tmp_path / "rec.db")
    monkeypatch.setattr(d, "OUTPUTS_DIR", tmp_path)
    monkeypatch.setattr(d, "SETTINGS_PATH", tmp_path / "settings.json")
    daemon = d.CamDaemon()
    daemon._last_http = 0.0          # no phone: idle stays idle
    daemon.align_locked = False      # locked is the default; tests re-align
    yield daemon
    daemon.db.close()


def test_realign_intent_rules(cam_daemon):
    cam_daemon.state = "recording"
    ok, msg = cam_daemon.intent_realign()
    assert not ok and "stop the recording" in msg
    cam_daemon.state = "standby"
    assert cam_daemon.intent_realign() == (True, "")
    assert cam_daemon.intent_realign()[0] is False          # already queued
    assert cam_daemon.realign_status()["running"] is True
    # while queued or running, nothing else may grab the cameras
    ok, msg = cam_daemon.intent_start()
    assert not ok and "re-aligning" in msg
    cam_daemon.state = "realigning"
    assert cam_daemon.pipeline_wanted() is False


def test_realign_runs_and_returns_to_idle(cam_daemon, rig, monkeypatch):
    out, _ = rig
    monkeypatch.setattr(d, "ALIGN_OUT", out)
    cam_daemon.state = "standby"
    cam_daemon.intent_realign()
    cam_daemon._begin_realign()
    assert cam_daemon.state == "realigning"
    cam_daemon.realign.join(30)
    assert cam_daemon.realign.ok is True
    # the tick loop's transition (one iteration, by hand)
    tick = threading_tick(cam_daemon)
    assert tick == "idle"
    snap = cam_daemon.snapshot()
    assert snap["realign"]["ok"] is True and snap["realign"]["running"] is False


def threading_tick(daemon):
    """Run the tick loop just long enough for one pass, then stop it."""
    import threading
    t = threading.Thread(target=daemon._tick_loop, daemon=True)
    t.start()
    deadline = time.time() + 5
    while daemon.state == "realigning" and time.time() < deadline:
        time.sleep(0.05)
    daemon.stopping = True
    t.join(5)
    return daemon.state


def test_alignment_is_locked_by_default_and_the_lock_persists(cam_daemon, tmp_path):
    fresh = d.CamDaemon()                       # no settings file: locked
    try:
        assert fresh.align_locked is True
        fresh.state = "standby"
        ok, msg = fresh.intent_realign()
        assert not ok and "locked" in msg and fresh._pending_realign is False
        assert fresh.realign_status()["locked"] is True
        assert fresh.set_align_locked("no")[0] is False
        assert fresh.set_align_locked(False) == (True, "")
        assert fresh.intent_realign() == (True, "")
        assert d._load_settings()["align_locked"] is False
        assert d.CamDaemon().align_locked is False   # survives a restart
        fresh.set_align_locked(True)
        assert d.CamDaemon().align_locked is True
    finally:
        fresh.db.close()


def test_fps_lock_is_on_by_default_and_persists(cam_daemon):
    fresh = d.CamDaemon()
    try:
        assert fresh.fps_lock is True
        assert fresh.set_fps_lock("no")[0] is False
        fresh.state = "recording"
        assert fresh.set_fps_lock(False)[0] is False     # not while recording
        fresh.state = "standby"
        assert fresh.set_fps_lock(False) == (True, "")
        assert d._load_settings()["fps_lock"] is False
        assert d.CamDaemon().fps_lock is False           # survives a restart
        assert fresh.set_fps_lock(True) == (True, "")
        assert d.CamDaemon().fps_lock is True
    finally:
        fresh.db.close()
