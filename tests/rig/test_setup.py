"""The phone's Setup view: pre-game self-test, framing guides, PWA files,
and (slow) a full re-align from the phone into a scratch map dir
(rig_test.sh sets AFL_CAM_ALIGN_OUT; calibration/ is never written)."""

import json
import os
import urllib.request

import pytest

from .conftest import BASE, get, post, stitch, wait_until, wake_standby


def test_selftest_reports_every_check():
    wake_standby()
    j = get("/api/selftest")
    assert j["ok"] is True
    names = [c["name"] for c in j["checks"]]
    assert names == ["cameras", "alignment", "storage", "temperature", "clock"]
    cams = j["checks"][0]
    assert cams["level"] == "ok", cams
    assert j["level"] in ("ok", "warn", "fail")


def test_framing_guides_from_the_live_maps():
    f = get("/api/framing")["framing"]
    assert f is not None, "no live map set on the rig"
    assert f["canvas"][0] > 0 and f["canvas"][1] > 0
    lo, hi = f["overlap"]       # where both cameras have pixels at the horizon row
    assert 0 < lo <= f["seam"] <= hi < 1, f
    assert f["horizon"] is None or 0 <= f["horizon"] < 1


@pytest.mark.parametrize("path, ctype", [
    ("/manifest.webmanifest", "application/manifest+json"),
    ("/icon-180.png", "image/png"),
    ("/icon-512.png", "image/png"),
])
def test_pwa_files_served(path, ctype):
    with urllib.request.urlopen(BASE + path, timeout=10) as r:
        assert r.headers.get("Content-Type", "").startswith(ctype)
        body = r.read()
    if path.endswith(".webmanifest"):
        assert json.loads(body)["start_url"] == "/"
    else:
        assert body[:8] == b"\x89PNG\r\n\x1a\n"


@pytest.mark.slow
def test_realign_from_the_phone():
    assert os.environ.get("AFL_CAM_TEST_BASE"), \
        "run via rig_test.sh (it points the re-align at a scratch dir)"
    wake_standby()
    j = post("/api/realign")
    assert j["ok"] is True, j
    # the pipeline closes so the chain can open the cameras
    wait_until(lambda: get("/api/status")["state"] == "realigning",
               15, what="realigning state")
    wait_until(lambda: not stitch(), 15, what="pipeline closed")
    assert post("/api/record/start", timeout=30)["ok"] is False
    ra = wait_until(lambda: (lambda r: r if not r["running"] else None)(
        get("/api/realign")), 900, interval=5, what="re-align finished")
    assert ra["ok"] is True, "\n".join(ra.get("log", [])[-40:])
    assert ra["after"] and ra["after"]["misalign_median"] is not None
    assert ra["before"] and ra["before"]["misalign_median"] is not None
    print(f"[realign] before {ra['before']} after {ra['after']}")
    with urllib.request.urlopen(BASE + "/api/realign/seam.jpg", timeout=10) as r:
        assert r.read()[:2] == b"\xff\xd8"
    # and the pipeline comes back on the next wake
    wake_standby(120)
