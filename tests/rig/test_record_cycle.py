"""End-to-end record cycle: start -> frames -> stop -> finalize -> an
iPhone-playable proxy (hvc1/faststart) appears in the recordings list."""

import time
import urllib.request

import pytest

from .conftest import BASE, get, post, status, wait_until, wake_standby


@pytest.mark.slow
def test_record_cycle_produces_faststart_proxy():
    # Retry the start if the pipeline is churning (a restart from an
    # earlier test) instead of reaching "recording".
    start = None
    for attempt in range(3):
        wake_standby()
        rid = f"test-cycle-start-{attempt}"
        start = post("/api/record/start", {"request_id": rid})
        assert start.get("ok"), start
        try:
            wait_until(lambda: status().get("state") == "recording",
                       15, what=f"recording state (attempt {attempt})")
            break
        except AssertionError:
            post("/api/record/stop", {"request_id": f"test-cycle-stop-{attempt}"})
            continue
    else:
        raise AssertionError("record start never reached 'recording' — "
                             "pipeline churn or external handover")
    time.sleep(5)                       # a few seconds of real frames
    stop = post("/api/record/stop", {"request_id": "test-cycle-stop"})
    assert stop.get("ok"), stop
    wait_until(lambda: status().get("state") == "standby",
               180, what="standby after finalize")

    recs = get("/api/recordings")["recordings"]
    assert recs, "no recordings listed after a record cycle"
    latest = recs[0]
    assert latest.get("has_proxy"), \
        f"latest session {latest.get('id')} has no proxy"

    url = BASE + f"/api/recordings/{latest['id']}/proxy.mp4"
    with urllib.request.urlopen(url, timeout=30) as r:
        head = r.read(512 * 1024)
    assert head[4:8] == b"ftyp", "proxy is not an mp4"
    assert head.find(b"moov") > 0, \
        "moov atom not near the front — faststart missing, iOS will not stream it"
