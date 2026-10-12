"""UI <-> phone service handover: the desktop UI's "Take over" calls
POST /api/release; the pipeline must close, stay down during the hold,
and resume on the next wake (the phone touching the daemon)."""

import time

from .conftest import get, post, stitch, wait_until, wake_standby


def test_release_closes_pipeline_and_wake_resumes():
    wake_standby()
    j = post("/api/release")
    assert j.get("ok") is True, j
    # the pipeline must actually close (cameras free for the UI)
    wait_until(lambda: not stitch(), 15, what="pipeline closed after release")
    # no immediate respawn: the hold keeps it down for ~15s
    time.sleep(3)
    assert not stitch(), "pipeline respawned during the hold"

    # hold expiry + a wake (phone or UI-close) brings it back
    time.sleep(13)
    get("/api/status")
    wait_until(lambda: stitch().get("state") == "standby",
               120, what="standby after wake")
