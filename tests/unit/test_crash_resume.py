"""Recording survives a crash (issue #8): the daemon keeps the record intent
across a pipeline crash, a daemon restart and a reboot, resumes into the
next segment of the same session, and finalize joins the segments.

The pipeline is a fake (no cameras): it answers REC_START/REC_STOP and
writes the fixed-name files cam2stitch would."""

import json
import os
import shutil
import subprocess
import threading
import time

import pytest

from afl_cam import daemon as d
from afl_cam import resume


class FakeProc:
    def poll(self):
        return None


class FakeSupervisor:
    kills = 0

    def kill_pipeline(self):
        self.kills += 1
        self.status = None

    def __init__(self):
        self.proc = FakeProc()
        self._adopted = False
        self.status = {"state": "standby"}
        self.status_ts = time.monotonic()
        self.sent = []

    def get_status(self):
        return self.status

    def command(self, line, timeout=5.0):
        self.sent.append(line)
        if line.startswith("REC_START "):
            rec_dir = line.split(" ", 1)[1]
            (d.Path(rec_dir) / "stitch.raw.h264").write_bytes(b"\0\0\0\1fake")
            self.status = {"state": "recording", "rec_dir": rec_dir}
        elif line == "REC_STOP":
            self.status = {"state": "standby"}
        return "OK"

    def crash(self):
        self.status = None


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(d, "ROOT_DIR", tmp_path)
    monkeypatch.setattr(d, "DB_PATH", tmp_path / "recordings.db")
    monkeypatch.setattr(d, "INTENT_PATH", tmp_path / "_live" / "intent.json")
    monkeypatch.setattr(d, "AUDIO_DEVICES", {})
    monkeypatch.setattr(d, "OUTPUTS_DIR", tmp_path)
    monkeypatch.setattr(d, "_rediscover_with_kick", lambda: {})
    monkeypatch.setattr(d, "session_dir_is_safe", lambda p: True)
    sess = tmp_path / "outputs" / "s1"

    def new_dir():
        sess.mkdir(parents=True)
        return sess
    monkeypatch.setattr(d, "new_session_dir", new_dir)
    finalized = []

    class FakeFinalizer:
        def __init__(self, daemon, session):
            self.session = session

        def start(self):
            finalized.append(self.session)
    monkeypatch.setattr(d, "Finalizer", FakeFinalizer)
    return tmp_path, sess, finalized


def make_daemon():
    cam = d.CamDaemon()
    cam.supervisor = FakeSupervisor()
    return cam


def test_crash_resumes_into_the_same_session(env):
    _, sess, finalized = env
    cam = make_daemon()
    cam.state = "standby"
    cam._execute_start()
    assert cam.state == "recording"
    assert resume.load_intent(d.INTENT_PATH)["dir"] == str(sess)

    cam.supervisor.crash()
    cam.on_stitch_exit()                      # pipeline died mid-recording
    assert cam.state == "starting" and cam._resume
    assert not finalized, "a crash must not finalize the recording"
    assert cam.snapshot()["resume"] == {"armed": True, "pending": True, "restarts": 0}
    cam.on_stitch_exit()                      # the supervisor's own EOF call
    assert cam._resume and cam.state == "starting"

    cam.supervisor.status = {"state": "standby"}   # pipeline back up
    cam.state = "standby"
    cam._execute_resume()
    assert cam.state == "recording" and not cam._resume
    assert cam.supervisor.sent[-1] == f"REC_START {sess}"
    assert (sess / "stitch.000.raw.h264").exists()   # dead run sealed
    assert (sess / "stitch.raw.h264").exists()       # new run writing
    assert cam.session.restarts == 1 and len(cam.session.gaps) == 1

    cam._execute_stop()
    assert resume.load_intent(d.INTENT_PATH) is None   # explicit stop clears
    assert finalized and finalized[0].dir == sess and finalized[0].restarts == 1


def test_daemon_restart_resumes_and_stale_intent_finalizes(env):
    _, sess, finalized = env
    cam = make_daemon()
    cam.state = "standby"
    cam._execute_start()
    del cam                                  # daemon killed mid-recording

    cam2 = make_daemon()
    cam2._recover_intent()
    assert cam2._resume and cam2.session.dir == sess
    assert cam2.pipeline_wanted()            # wakes the cameras by itself
    cam2.state = "standby"
    cam2._execute_resume()
    assert cam2.state == "recording" and cam2.session.restarts == 1

    # power cut after the game: the intent is old by the next boot
    intent = resume.load_intent(d.INTENT_PATH)
    intent["alive_at"] -= d.RESUME_MAX_GAP_S + 60
    resume.save_intent(d.INTENT_PATH, intent)
    cam3 = make_daemon()
    cam3._recover_intent()
    assert not cam3._resume and cam3.session is None
    assert finalized and finalized[-1].dir == sess
    assert resume.load_intent(d.INTENT_PATH) is None


def power_cut(sess, age_s=0.0):
    """What a power cut mid-heartbeat left on the board (2026-10-14): the
    intent and its .tmp read back as NUL bytes; the video files, the pts
    sidecar and the DB row survive."""
    d.INTENT_PATH.write_bytes(b"\0" * 260)
    d.INTENT_PATH.with_name("intent.json.tmp").write_bytes(b"\0" * 260)
    (sess / "stitch.pts").write_text("1000000\n2000000\n61000000\n")
    stamp = time.time() - age_s
    for name in ("stitch.pts", "stitch.raw.h264"):
        os.utime(sess / name, (stamp, stamp))


def test_power_cut_wiped_intent_resumes_from_the_db(env):
    _, sess, finalized = env
    cam = make_daemon()
    cam.state = "standby"
    cam._execute_start()
    rec_id = cam.session.recording_id
    power_cut(sess)

    cam2 = make_daemon()
    cam2._recover_intent()
    assert cam2._resume and cam2.session.dir == sess
    assert cam2.session.recording_id == rec_id
    assert cam2.session.elapsed_before == pytest.approx(60.0)
    assert not finalized
    cam2.state = "standby"
    cam2._execute_resume()
    assert cam2.state == "recording"
    assert (sess / "stitch.000.raw.h264").exists()
    assert resume.load_intent(d.INTENT_PATH)["dir"] == str(sess)


def test_power_cut_wiped_intent_long_ago_finalizes(env):
    _, sess, finalized = env
    cam = make_daemon()
    cam.state = "standby"
    cam._execute_start()
    power_cut(sess, age_s=d.RESUME_MAX_GAP_S + 60)

    cam2 = make_daemon()
    cam2._recover_intent()
    assert not cam2._resume and cam2.session is None
    assert finalized and finalized[-1].dir == sess
    assert finalized[-1].duration == pytest.approx(60.0)
    assert not d.INTENT_PATH.exists()
    assert not d.INTENT_PATH.with_name("intent.json.tmp").exists()


def test_wiped_intent_without_a_recording_in_the_db_is_cleared(env):
    cam = make_daemon()
    d.INTENT_PATH.parent.mkdir(parents=True, exist_ok=True)
    d.INTENT_PATH.write_bytes(b"\0" * 260)
    cam._recover_intent()
    assert not cam._resume and cam.session is None
    assert not d.INTENT_PATH.exists()


def test_finalize_cut_short_by_a_restart_runs_again(env):
    """Stop clears the intent, then the daemon is restarted mid-mux (a rig
    test stopped afl-camd, 2026-10-14): the DB row is still 'recording'.
    The next start finalizes it again and never resumes it."""
    _, sess, finalized = env
    cam = make_daemon()
    cam.state = "standby"
    cam._execute_start()
    cam._execute_stop()
    assert len(finalized) == 1 and not d.INTENT_PATH.exists()

    cam2 = make_daemon()
    cam2._recover_intent()
    assert not cam2._resume and cam2.session is None
    assert cam2.state == "finalizing"
    assert len(finalized) == 2 and finalized[-1].dir == sess


def test_nothing_to_recover_on_a_clean_start(env):
    cam = make_daemon()
    cam._recover_intent()
    assert cam.state == "idle" and cam.session is None


def test_last_heartbeat_tmp_is_used_when_the_intent_is_torn(env):
    _, sess, _ = env
    cam = make_daemon()
    cam.state = "standby"
    cam._execute_start()
    intent = resume.load_intent(d.INTENT_PATH)
    d.INTENT_PATH.with_name("intent.json.tmp").write_text(json.dumps(intent))
    d.INTENT_PATH.write_bytes(b"\0" * 260)
    assert resume.load_intent(d.INTENT_PATH)["dir"] == str(sess)


def test_clock_jump_is_not_a_stalled_pipeline(env, monkeypatch):
    """The phone (or NTP) sets the clock hours forward: the watchdog must
    not read that as a frozen pipeline and cut the recording (2026-10-14:
    'stitch status stalled 12684s' on an NTP sync)."""
    cam = make_daemon()
    cam.state = "standby"
    cam._execute_start()
    real = time.time
    monkeypatch.setattr(d.time, "time", lambda: real() + 12684)
    cam.supervisor.status_ts = time.monotonic()
    t = threading.Thread(target=cam._tick_loop, daemon=True)
    t.start()
    time.sleep(0.5)
    cam.stopping = True
    t.join(5)
    assert cam.supervisor.kills == 0
    assert cam.state == "recording" and not cam._resume


def test_stop_while_waiting_to_resume_finalizes(env):
    _, sess, finalized = env
    cam = make_daemon()
    cam.state = "standby"
    cam._execute_start()
    cam.supervisor.crash()
    cam.on_stitch_exit()
    cam._pending_stop = True
    cam._abandon_resume()
    assert cam.state == "finalizing" and not cam._resume
    assert finalized[-1].dir == sess
    assert resume.load_intent(d.INTENT_PATH) is None


def test_seal_numbers_segments_in_order(tmp_path):
    for i in range(3):
        for name in ("stitch.raw.h264", "stitch.pts", "preview.raw.h264", "cam0.wav"):
            (tmp_path / name).write_text(f"run{i}")
        if i < 2:
            assert resume.seal_segment(tmp_path) == i
    assert resume.seal_segment(tmp_path) == 2
    assert resume.seal_segment(tmp_path) is None     # nothing left to seal
    pairs = resume.segments(tmp_path, "stitch.raw.h264")
    assert [r.read_text() for r, _ in pairs] == ["run0", "run1", "run2"]
    assert (tmp_path / "cam0.001.wav").read_text() == "run1"


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")
def test_finalize_joins_segments(tmp_path, monkeypatch):
    monkeypatch.setattr(d, "HEVC", False)
    monkeypatch.setattr(d, "DB_PATH", tmp_path / "recordings.db")
    sess = tmp_path / "s1"
    sess.mkdir()
    for name, secs in (("stitch.000", 2), ("stitch", 3), ("preview.000", 2), ("preview", 3)):
        subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i",
             f"testsrc=size=160x96:rate=10:duration={secs}",
             "-c:v", "libx264", "-g", "10", "-bf", "0", "-f", "h264",
             str(sess / f"{name}.raw.h264")], check=True)
        (sess / f"{name}.pts").write_text(
            "\n".join(str(i * 100000) for i in range(secs * 10)))
    db = d.RecordingDB(d.DB_PATH)
    sid = db.add_session(created_at="2026-10-12 12:00:00", folder="s1")
    rid = db.add(camera="stitch", device_path="x", resolution="live stitch",
                 file_path="s1/stitch.mp4", session_id=sid,
                 created_at="2026-10-12 12:00:00")
    db.close()
    s = d.Session(sess, sid, rid, [])
    s.restarts, s.gaps, s.duration = 1, [{"at_s": 2.0, "gap_s": 4.5}], 5.0

    done = []

    class Owner:
        def on_finalized(self, session, master_ok, proxy_ok):
            done.append((master_ok, proxy_ok))
    d.Finalizer(Owner(), s).run()

    assert done == [(True, True)]
    for out in ("stitch.mp4", "proxy.mp4"):
        dur = float(subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(sess / out)],
            capture_output=True, text=True, check=True).stdout)
        assert dur == pytest.approx(5.0, abs=0.3), out
    assert not list(sess.glob("*.raw.h264"))       # raws gone once joined
    assert not list(sess.glob("*.part*"))
    report = json.loads((sess / "segments.json").read_text())
    assert report["restarts"] == 1 and report["lost_s"] == 4.5
