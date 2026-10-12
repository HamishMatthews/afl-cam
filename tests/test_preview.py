"""The phone preview chain: cam2stitch preview encoder -> UDP -> ffmpeg ->
MediaMTX -> HLS. Regression guards for the two 2026-10-12 outages (missing
SPS/PPS injection, LL-HLS part-duration errors on iOS)."""

import http.cookiejar
import time
import urllib.request

from .conftest import stitch, wake_standby

HLS = "http://127.0.0.1:8888"


class _AcceptAll(http.cookiejar.DefaultCookiePolicy):
    """mediamtx's session cookie is marked Secure; over plain http:// the
    default policy drops it and the server 401s. Tests accept everything."""

    def set_ok(self, cookie, request):
        return True

    def return_ok(self, cookie, request):
        return True


def _new_jar():
    return http.cookiejar.CookieJar(policy=_AcceptAll())


def _hls(path, jar, retries=30):
    """Fetch an HLS resource, retrying while the publisher reconnects
    (after a handover/release mediamtx 404s the path for a few seconds)."""
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(jar))
    last = None
    for _ in range(retries):
        try:
            with opener.open(HLS + path, timeout=10) as r:
                return r.read().decode()
        except urllib.error.HTTPError as e:
            last = e
            if e.code != 404:
                raise
        except OSError as e:
            last = e
        time.sleep(1)
    raise last or AssertionError(f"{path} never served")


def test_preview_encoder_healthy():
    st = wake_standby()
    pv = st.get("preview") or {}
    assert pv.get("on") == 1, "preview encoder off"
    assert pv.get("packets", 0) > 0, "preview encoder produced nothing"
    assert pv.get("hdr_sent", 0) > 0, \
        "no SPS/PPS injected — IDRs will not be decodable"
    assert pv.get("drops", 999) == 0, "preview packets dropped"


def test_hls_master_playlist_serves():
    st = wake_standby()
    jar = _new_jar()
    master = _hls("/preview/index.m3u8", jar)
    assert "STREAM-INF" in master, "no master playlist"
    assert "RESOLUTION=1600x564" in master, "wrong preview resolution"
    assert "FRAME-RATE=30" in master, "wrong preview frame rate"


def test_hls_segments_accumulate():
    wake_standby()

    def segments():
        # mediamtx occasionally 401s a repeated session (cookie dance) —
        # rebuild the session like a fresh browser tab would
        for _ in range(2):
            jar = _new_jar()
            try:
                _hls("/preview/index.m3u8", jar)
                n1 = _hls("/preview/main_stream.m3u8", jar).count(".ts")
                time.sleep(5)
                n2 = _hls("/preview/main_stream.m3u8", jar).count(".ts")
                return n1, n2
            except urllib.error.HTTPError as e:
                if e.code != 401:
                    raise
        raise AssertionError("HLS session keeps 401ing")

    n1, n2 = segments()
    assert n2 > n1, f"HLS segments not growing: {n1} -> {n2}"
