"""Shared helpers for the afl-cam rig test suite.

These are INTEGRATION tests against the live board: they drive the real
afl-camd daemon (http://127.0.0.1:80) and MediaMTX (HLS :8888) and use
the real cameras. Close the desktop UI first — the daemon and the UI
can't share the cameras. The suite wakes the daemon itself (every
/api/status touch counts as a phone arrival).
"""

import json
import time
import urllib.request

import pytest

BASE = "http://127.0.0.1:80"


def get(path, timeout=30):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return json.loads(r.read().decode())


def post(path, body=None, timeout=60):
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(
        BASE + path, data=data,
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def status():
    return get("/api/status")


def stitch():
    """The cam2stitch sub-status dict ({} when the pipeline is down)."""
    return status().get("stitch") or {}


def wait_until(fn, timeout=90, interval=1.0, what="condition"):
    """Poll fn() until it returns something truthy; raise with context."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = fn()
        if last:
            return last
        time.sleep(interval)
    raise AssertionError(
        f"timed out after {timeout}s waiting for {what} (last: {last!r})")


def wake_standby(timeout=90):
    """Touch the daemon and wait for the pipeline to reach standby.
    Returns the stitch status dict."""
    get("/api/status")
    wait_until(lambda: stitch().get("state") == "standby",
               timeout, what="stitch standby")
    return stitch()


@pytest.fixture(scope="session", autouse=True)
def daemon_reachable():
    try:
        s = status()
    except OSError as e:
        pytest.exit(
            f"afl-camd not reachable at {BASE} ({e}) — is the service "
            "running? systemctl status afl-camd", returncode=1)
    assert s.get("ok") is True, f"unexpected status reply: {s!r}"
