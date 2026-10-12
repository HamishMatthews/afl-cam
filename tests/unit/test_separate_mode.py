"""Separate recording mode in the daemon: the pipeline closes, one
recorder per camera writes the raw views, stop muxes both, the proxy is a
hard link to cam0.mp4 and the session lists as re-stitchable. cam2h264 is
a stand-in script that writes a real H.264 stream + .pts until SIGINT."""

import os
import shutil
import subprocess
import sys
import textwrap
import time

import pytest

from afl_cam import daemon as d
from afl_cam import sessions

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None,
                                reason="needs ffmpeg")

FAKE_RECORDER = """
import shutil, signal, sys, time
from pathlib import Path
raw = Path(sys.argv[2])
stop = []
signal.signal(signal.SIGINT, lambda *_: stop.append(1))
shutil.copy({sample!r}, raw)
raw.with_name(raw.name.replace(".raw.h264", ".pts")).write_text(
    "\\n".join(str(1_000_000 + i * 33_333) for i in range(60)) + "\\n")
raw.with_name(raw.name.replace(".raw.h264", "_preview.jpg")).write_bytes(b"\\xff\\xd8jpeg")
while not stop:
    time.sleep(0.05)
"""


@pytest.fixture
def separate(tmp_path, monkeypatch):
    sample = tmp_path / "sample.h264"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc=size=160x90:rate=30",
                    "-frames:v", "60", "-c:v", "libx264", "-f", "h264",
                    str(sample)], check=True)
    fake = tmp_path / "cam2h264"
    fake.write_text(f"#!{sys.executable}\n"
                    + textwrap.dedent(FAKE_RECORDER.format(sample=str(sample))))
    fake.chmod(0o755)
    outputs = tmp_path / "outputs"
    monkeypatch.setattr(d, "build_cam2h264", lambda: fake)
    monkeypatch.setattr(d, "_rediscover_with_kick",
                        lambda: {"cam0": "/dev/video0", "cam1": "/dev/video2"})
    monkeypatch.setattr(d, "AUDIO_DEVICES", {})
    monkeypatch.setattr(d, "HEVC", False)
    monkeypatch.setattr(d, "ROOT_DIR", tmp_path)
    monkeypatch.setattr(d, "OUTPUTS_DIR", outputs)
    monkeypatch.setattr(d, "DB_PATH", tmp_path / "rec.db")
    monkeypatch.setattr(d, "INTENT_PATH", tmp_path / "_live" / "intent.json")
    monkeypatch.setattr(sessions, "OUTPUTS_DIR", outputs)
    daemon = d.CamDaemon()
    daemon._last_http = 0.0
    yield daemon
    if daemon.session and daemon.session.recorder:
        daemon.session.recorder.stop(timeout=2)
    daemon.db.close()


def wait_state(daemon, state, timeout=30):
    deadline = time.time() + timeout
    while daemon.state != state and time.time() < deadline:
        time.sleep(0.05)
    assert daemon.state == state


def test_mode_switch_rules(separate):
    assert separate.set_mode("bogus")[0] is False
    assert separate.set_mode("separate") == (True, "")
    separate.state = "recording"
    ok, msg = separate.set_mode("live")
    assert not ok and "recording" in msg


def test_separate_record_cycle(separate):
    separate.set_mode("separate")
    separate._pending_start = True
    separate._execute_start()
    assert separate.state == "recording", separate.error
    s = separate.session
    assert s.kind == "separate" and set(s.recording_ids) == {"cam0", "cam1"}
    assert separate.pipeline_wanted() is False       # recorders own the cameras
    assert separate.live_snapshot("cam1").name == "cam1_preview.jpg"
    assert separate.snapshot()["session_kind"] == "separate"
    separate._save_intent()                  # crash resume is live-only
    assert not d.INTENT_PATH.exists()

    separate._execute_stop()
    wait_state(separate, "idle")
    assert separate._cameras_busy is None             # cameras back to the pipeline
    folder = s.dir
    for cam in ("cam0", "cam1"):
        assert (folder / f"{cam}.mp4").stat().st_size > 0
        assert not (folder / f"{cam}.raw.h264").exists()
    assert os.path.samefile(folder / "proxy.mp4", folder / "cam0.mp4")
    assert (folder / "thumb.jpg").is_file()
    rec = separate.recordings()[0]
    assert rec["kind"] == "separate" and rec["has_proxy"] and not rec["has_master"]
    rows = separate.db.session_recordings(s.session_id)
    assert {r["status"] for r in rows} == {"complete"}, rows


def test_recorder_crash_finalizes(separate):
    separate.set_mode("separate")
    separate._pending_start = True
    separate._execute_start()
    assert separate.state == "recording"
    separate.session.recorder.procs["cam1"].kill()
    import threading
    t = threading.Thread(target=separate._tick_loop, daemon=True)
    t.start()
    try:
        wait_state(separate, "idle")
    finally:
        separate.stopping = True
        t.join(5)
    assert "cam1" in (separate.error or "")


def test_restitch_refuses_live_sessions(separate, tmp_path):
    sid = separate.db.add_session(created_at="2026-10-12 10:00:00",
                                  folder="outputs/20261012_100000")
    (tmp_path / "outputs" / "20261012_100000").mkdir(parents=True)
    ok, msg = separate.start_restitch(sid)
    assert not ok and "separate" in msg
    assert separate.start_restitch(9999)[0] is False


def test_restitch_cli_reports_missing_camera(tmp_path, monkeypatch, capsys):
    from afl_cam import db as dbmod
    from afl_cam import restitch
    monkeypatch.setattr(restitch, "DB_PATH", tmp_path / "rec.db")
    monkeypatch.setattr(restitch, "ROOT_DIR", tmp_path)
    folder = tmp_path / "outputs" / "s1"
    folder.mkdir(parents=True)
    (folder / "cam0.mp4").write_bytes(b"x")
    db = dbmod.RecordingDB(tmp_path / "rec.db")
    sid = db.add_session(created_at="2026-10-12 10:00:00", folder="outputs/s1")
    db.close()
    assert restitch.main([str(sid)]) == 1
    assert "missing cam1" in capsys.readouterr().out
    db = dbmod.RecordingDB(tmp_path / "rec.db")
    assert db.get_session(sid)["stitch_status"] == "failed"
    db.close()
    assert restitch.main(["nope"]) == 2
