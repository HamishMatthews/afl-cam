"""Daemon clock sync from the phone (offline AP = no NTP)."""

import time

import afl_cam.daemon as daemon

EPOCH_MS = 1_700_000_000_000
EPOCH_S = 1_700_000_000.0


def _daemon(synced=False):
    d = object.__new__(daemon.CamDaemon)
    d._clock_synced = synced
    return d


def test_skips_when_synced(monkeypatch):
    calls = []
    monkeypatch.setattr(daemon.time, "clock_settime", lambda *a: calls.append(a))
    assert _daemon(synced=True).set_clock(EPOCH_MS) == (True, "already synced")
    assert calls == []


def test_sets_realtime_clock(monkeypatch):
    calls = []
    monkeypatch.setattr(daemon.time, "clock_settime", lambda *a: calls.append(a))
    d = _daemon()
    assert d.set_clock(EPOCH_MS) == (True, "")
    assert calls == [(time.CLOCK_REALTIME, EPOCH_S)]
    assert d._clock_synced is True


def test_reports_failure_and_stays_unsynced(monkeypatch):
    def boom(*_a):
        raise OSError("EPERM")

    monkeypatch.setattr(daemon.time, "clock_settime", boom)
    d = _daemon()
    ok, msg = d.set_clock(EPOCH_MS)
    assert ok is False and "EPERM" in msg
    assert d._clock_synced is False
