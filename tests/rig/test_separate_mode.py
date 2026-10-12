"""Separate recording mode from the phone: the pipeline hands the cameras
to two per-camera recorders, stop muxes both views, and the session
re-stitches from its files with the live map set."""

import time
import urllib.request

import pytest

from .conftest import BASE, get, post, status, wait_until, wake_standby


@pytest.mark.slow
def test_separate_record_and_restitch():
    wake_standby()
    assert post("/api/mode", {"mode": "separate"})["ok"] is True
    sid = None
    try:
        start = post("/api/record/start", {"request_id": "sep-start"})
        assert start.get("ok"), start
        assert status()["session_kind"] == "separate"
        time.sleep(3)
        with urllib.request.urlopen(BASE + "/api/live/cam0.jpg", timeout=10) as r:
            assert r.read(2) == b"\xff\xd8", "no 4K snapshot from cam0"
        time.sleep(5)
        assert post("/api/record/stop", {"request_id": "sep-stop"})["ok"]
        wait_until(lambda: status()["state"] in ("idle", "starting", "standby"),
                   180, what="finalized")
        rec = get("/api/recordings")["recordings"][0]
        sid = rec["id"]
        assert rec["kind"] == "separate" and rec["has_proxy"], rec

        assert post(f"/api/recordings/{sid}/stitch")["ok"] is True
        wait_until(lambda: not (status().get("restitch") or {}).get("running"),
                   900, interval=5, what="re-stitch finished")
        rs = status()["restitch"]
        assert rs["ok"] is True, rs
        rec = next(r for r in get("/api/recordings")["recordings"] if r["id"] == sid)
        assert rec["has_master"] and rec["stitch_status"] == "stitched", rec
        with urllib.request.urlopen(
                BASE + f"/api/recordings/{sid}/master.mp4", timeout=30) as r:
            assert r.read(16)[4:8] == b"ftyp"
        # and live mode comes back on the next wake
        assert post("/api/mode", {"mode": "live"})["ok"] is True
        wake_standby(120)
    finally:
        post("/api/mode", {"mode": "live"})
        if sid is not None:
            req = urllib.request.Request(BASE + f"/api/recordings/{sid}",
                                         method="DELETE")
            urllib.request.urlopen(req, timeout=30).read()
