"""The phone's Setup view: pre-game self-test, framing guides, PWA files.
The re-align chain runs on stored stills (test_realign_chain.py), never
from the live cameras."""

import json
import urllib.request

import pytest

from .conftest import BASE, get, wake_standby


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
