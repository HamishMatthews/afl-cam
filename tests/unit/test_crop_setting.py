"""The crop setting in afl-camd: persisted, refused while recording, picks
the cropped map set for the next pipeline start (falling back to the full
canvas if the crop can't be built), and restarts an idle pipeline."""

import pytest

from afl_cam import daemon as d
from afl_cam import stitch_maps

from .test_crash_resume import FakeSupervisor
from .test_crop_maps import make_live


class StoppableProc:
    def __init__(self):
        self.signals = []

    def poll(self):
        return None

    def send_signal(self, sig):
        self.signals.append(sig)


@pytest.fixture
def env(tmp_path, monkeypatch):
    make_live(tmp_path, "live")
    monkeypatch.setattr(d, "DB_PATH", tmp_path / "recordings.db")
    monkeypatch.setattr(d, "OUTPUTS_DIR", tmp_path)
    monkeypatch.setattr(d, "INTENT_PATH", tmp_path / "_live" / "intent.json")
    monkeypatch.setattr(d, "SETTINGS_PATH", tmp_path / "_live" / "settings.json")
    monkeypatch.setattr(d, "STITCH_PARAMS_DIR", tmp_path / "live")
    monkeypatch.setattr(d, "CROP_PARAMS_DIR", tmp_path / "crop")
    monkeypatch.setattr(d, "AUDIO_DEVICES", {})
    return tmp_path


def make_daemon():
    cam = d.CamDaemon()
    cam.supervisor = FakeSupervisor()
    cam.supervisor.proc = StoppableProc()
    return cam


def test_setting_persists_and_validates(env):
    cam = make_daemon()
    assert cam.crop_status()["enabled"] is False
    assert cam.set_crop("yes")[0] is False
    assert cam.set_crop(True) == (True, "")
    assert d._load_settings() == {"crop": True}
    assert make_daemon().crop_enabled is True          # survives a restart


def test_refused_while_recording(env):
    cam = make_daemon()
    cam.state = "recording"
    ok, msg = cam.set_crop(True)
    assert not ok and "recording" in msg and cam.crop_enabled is False


def test_pipeline_start_uses_the_crop_set_and_falls_back(env):
    cam = make_daemon()
    assert cam.choose_params_dir() == env / "live"
    cam.crop_enabled = True
    assert cam.choose_params_dir() == env / "crop"
    cropped = stitch_maps.load(env / "crop")
    full = stitch_maps.load(env / "live")
    assert cropped.canvas[0] <= full.canvas[0] and cropped.canvas[1] <= full.canvas[1]
    assert cropped.canvas[0] % 4 == 0 and cropped.canvas[1] % 2 == 0
    status = cam.crop_status()
    assert status == {"enabled": True, "active": True, "canvas": list(cropped.canvas)}

    for name in d.MAP_FILES:                            # a broken live set: no crop, no crash
        (env / "live" / name).write_bytes(b"torn")
    assert cam.choose_params_dir() == env / "live"
    assert cam.crop_status()["active"] is False


def test_changing_it_restarts_an_idle_pipeline(env):
    cam = make_daemon()
    cam.state = "standby"
    assert cam.set_crop(True) == (True, "")
    assert cam.state == "idle" and cam._idle_sigterm
    assert cam.supervisor.proc.signals == [15]

    cam2 = make_daemon()                                # not running: just remembered
    cam2.state = "idle"
    cam2.crop_enabled = True
    assert cam2.set_crop(False) == (True, "")
    assert cam2.supervisor.proc.signals == []
