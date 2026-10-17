"""The field alignment is locked until the phone unlocks it, so a stray tap
can never replace it."""

from .conftest import get, post


def test_realign_refused_while_locked():
    assert post("/api/align-lock", {"locked": True})["ok"] is True
    j = post("/api/realign")
    assert j["ok"] is False and "locked" in j["error"], j
    assert get("/api/status")["realign"]["locked"] is True
    assert get("/api/status")["state"] != "realigning"
