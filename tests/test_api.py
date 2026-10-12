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
