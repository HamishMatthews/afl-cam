"""REST API surface: schema + idempotency contracts."""

from .conftest import get, post, status

ALLOWED_STATES = {"idle", "starting", "standby", "recording",
                  "finalizing", "error", "connecting"}


def test_status_schema():
    s = status()
    assert s["ok"] is True
    assert s["state"] in ALLOWED_STATES, s["state"]
    assert "recording" in s
    assert "storage" in s and "minutes_left" in s.get("storage", {})
    assert "stitch" in s


def test_recordings_list():
    j = get("/api/recordings")
    assert j["ok"] is True
    assert isinstance(j["recordings"], list)
    for r in j["recordings"]:
        assert "id" in r and "has_proxy" in r and "created_at" in r


def test_record_start_is_idempotent():
    rid = "test-idem-1"
    a = post("/api/record/start", {"request_id": rid})
    b = post("/api/record/start", {"request_id": rid})
    assert a.get("ok") is True, a
    # the duplicate returns the SAME outcome, not a second start
    assert b.get("ok") == a.get("ok"), b
    post("/api/record/stop", {"request_id": "test-idem-2"})


def test_crop_setting_switches_the_canvas():
    """Crop on restarts the idle pipeline on the cropped map set (smaller
    canvas, no black margins), off brings the full canvas back."""
    import time

    def wait_canvas(want_crop):
        deadline = time.time() + 90
        while time.time() < deadline:
            s = status()
            c = s.get("crop") or {}
            if c.get("active") is want_crop and s.get("state") == "standby" \
                    and (s.get("stitch") or {}).get("cam0", {}).get("frames", 0) > 0:
                return c
            time.sleep(1)
        raise AssertionError(f"crop active={want_crop} never settled: {status().get('crop')}")

    full = wait_canvas(False)["canvas"]
    try:
        assert post("/api/crop", {"enabled": True}).get("ok") is True
        cropped = wait_canvas(True)["canvas"]
        assert cropped[0] <= full[0] and cropped[1] <= full[1] and cropped != full
        assert cropped[0] % 4 == 0 and cropped[1] % 2 == 0
    finally:
        assert post("/api/crop", {"enabled": False}).get("ok") is True
    assert wait_canvas(False)["canvas"] == full
    # leave a settled preview behind: the pusher restarts on a size change
    deadline = time.time() + 60
    while time.time() < deadline and not (status().get("preview") or {}).get("chain"):
        time.sleep(1)
    time.sleep(5)
