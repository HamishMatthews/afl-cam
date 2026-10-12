"""A SIGKILLed pipeline mid-recording resumes into the same session and
finalize joins the segments (issue #8). Slow: a real record cycle."""

import os
import subprocess
import time

import pytest

from .conftest import get, post, status, wait_until, wake_standby


@pytest.mark.slow
def test_sigkill_mid_recording_resumes_same_session():
    wake_standby()
    assert post("/api/record/start", {"request_id": "test-resume-start"}).get("ok")
    wait_until(lambda: status().get("state") == "recording", 20, what="recording")
    assert status()["resume"]["armed"]
    time.sleep(4)

    # crash the pipeline the daemon under test spawned (ours, same user)
    subprocess.run(["pkill", "-KILL", "-u", str(os.getuid()), "-x", "cam2stitch"],
                   check=True)
    wait_until(lambda: (lambda s: s.get("state") == "recording"
                        and s["resume"]["restarts"] == 1)(status()),
               90, what="recording resumed after SIGKILL")
    time.sleep(4)

    assert post("/api/record/stop", {"request_id": "test-resume-stop"}).get("ok")
    wait_until(lambda: status().get("state") == "standby", 180,
               what="standby after finalize")
    latest = get("/api/recordings")["recordings"][0]
    assert latest.get("has_master") and latest.get("has_proxy"), latest
    assert latest.get("resumed", {}).get("restarts") == 1, latest
    print(f"[crash-resume] gap {latest['resumed']['lost_s']} s")
