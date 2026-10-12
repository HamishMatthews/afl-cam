"""Pre-game self-test verdicts (Setup view)."""

from afl_cam import selftest

BOTH = {"cam0": "/dev/video0", "cam1": "/dev/video2"}


def stitch(f0=100, f1=100, e0=0, e1=0):
    return {"cam0": {"frames": f0, "errors": e0},
            "cam1": {"frames": f1, "errors": e1}}


def test_cameras():
    assert selftest.cameras({"cam0": "/dev/video0"}, None)["level"] == "fail"
    assert "right" in selftest.cameras({"cam0": "/dev/video0"}, None)["detail"]
    assert selftest.cameras(BOTH, None)["level"] == "ok"
    assert selftest.cameras(BOTH, stitch())["level"] == "ok"
    assert selftest.cameras(BOTH, stitch(f1=0))["level"] == "fail"
    assert selftest.cameras(BOTH, stitch(e0=3))["level"] == "warn"


def test_alignment_age():
    now = 1_000_000_000
    assert selftest.alignment(None, now)["level"] == "fail"
    assert selftest.alignment(now - 3600, now)["level"] == "ok"
    assert selftest.alignment(now - 40 * 86400, now)["level"] == "warn"


def test_storage_needs_a_full_game():
    assert selftest.storage(149)["level"] == "fail"
    assert selftest.storage(150)["level"] == "ok"
    assert selftest.storage(None)["level"] == "warn"


def test_temperature():
    assert selftest.temperature(55, 40, 80)["level"] == "ok"
    assert selftest.temperature(55, 72, 80)["level"] == "warn"
    assert selftest.temperature(81, None, 80)["level"] == "fail"
    assert selftest.temperature(None, None, 80)["level"] == "warn"


def test_overall_is_the_worst():
    checks = [selftest.clock(True), selftest.storage(10), selftest.clock(False)]
    assert selftest.overall(checks) == "fail"
    assert selftest.overall([selftest.clock(True)]) == "ok"
