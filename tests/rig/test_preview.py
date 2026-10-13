"""The phone preview chain: cam2stitch preview encoder -> UDP -> ffmpeg ->
MediaMTX -> HLS. Regression guards for the two 2026-10-12 outages (missing
SPS/PPS injection, LL-HLS part-duration errors on iOS)."""

import http.cookiejar
import os
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest

from afl_cam import framing
from afl_cam.config import STITCH_PARAMS_DIR

from .conftest import stitch, wake_standby

HLS = "http://127.0.0.1:8888"
WHEP = "http://127.0.0.1:8889/preview/whep"
# the origin the phone page is served from (the board's AP address)
PAGE_ORIGIN = "http://10.42.0.1"


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


def _preview_height():
    """cam2stitch's preview box: 1600 wide at the canvas aspect, height
    rounded down to even. The canvas height changes with every re-align."""
    params = framing.read_params(STITCH_PARAMS_DIR)
    return (1600 * int(params[1]) // int(params[0])) & ~1


def test_hls_master_playlist_serves():
    st = wake_standby()
    jar = _new_jar()
    master = _hls("/preview/index.m3u8", jar)
    assert "STREAM-INF" in master, "no master playlist"
    assert f"RESOLUTION=1600x{_preview_height()}" in master, "wrong preview resolution"
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


def _offer_sdp():
    """A recvonly H.264 offer, as a browser builds it (host candidates and
    all — the page posts the offer with ICE gathering already finished)."""
    fp = ":".join(f"{b:02X}" for b in os.urandom(32))
    return "\r\n".join([
        "v=0",
        "o=- 1 1 IN IP4 127.0.0.1",
        "s=-",
        "t=0 0",
        "a=group:BUNDLE 0",
        "m=video 9 UDP/TLS/RTP/SAVPF 96",
        "c=IN IP4 0.0.0.0",
        "a=rtcp:9 IN IP4 0.0.0.0",
        "a=ice-ufrag:abcd",
        "a=ice-pwd:abcdefghijklmnopqrstuvwx",
        "a=ice-options:trickle",
        f"a=fingerprint:sha-256 {fp}",
        "a=setup:actpass",
        "a=mid:0",
        "a=recvonly",
        "a=rtcp-mux",
        "a=rtpmap:96 H264/90000",
        "a=fmtp:96 level-asymmetry-allowed=1;packetization-mode=1;"
        "profile-level-id=42e01f",
        "",
    ])


def test_whep_offer_answer_delete():
    """The page's WebRTC client, end to end at the HTTP level.

    Regression guard for 2026-10-13 (issue #40): the page used to GET this
    endpoint, which answers 405 with a JSON body that setRemoteDescription
    rejected — WebRTC never connected, so every load fell back to HLS.
    WHEP is a POSTed offer answered with 201 + SDP + a Location, and the
    session must be DELETEd or the reader lingers.
    """
    # method check first: 405 does not depend on a publisher existing
    with pytest.raises(urllib.error.HTTPError) as err:
        urllib.request.urlopen(WHEP, timeout=10)
    assert err.value.code == 405, \
        f"GET on the WHEP endpoint should be refused, got {err.value.code}"
    err.value.close()

    wake_standby()
    offer = _offer_sdp().encode()

    # mediamtx 404s WHEP while the path has no publisher — the pusher takes
    # a moment after the pipeline reaches standby
    last = None
    for _ in range(30):
        req = urllib.request.Request(
            WHEP, data=offer, method="POST",
            headers={"Content-Type": "application/sdp",
                     "Origin": PAGE_ORIGIN})
        try:
            r = urllib.request.urlopen(req, timeout=15)
            break
        except urllib.error.HTTPError as e:
            last = e
            if e.code != 404:
                raise
        time.sleep(1)
    else:
        raise AssertionError(f"WHEP never accepted an offer (last: {last})")

    with r:
        assert r.status == 201, f"WHEP POST answered {r.status}, want 201"
        answer = r.read().decode()
        loc = r.headers.get("Location")
        exposed = r.headers.get("Access-Control-Expose-Headers", "")
        origin = r.headers.get("Access-Control-Allow-Origin")
    assert "m=video" in answer and "a=sendonly" in answer, \
        f"mediamtx returned no video answer: {answer[:160]!r}"
    assert "H264" in answer, "mediamtx did not pick H.264"
    assert loc, "no Location header — the page cannot address the session"
    assert "Location" in exposed, \
        "mediamtx must expose Location through CORS or the page cannot " \
        f"send its DELETE (Access-Control-Expose-Headers: {exposed!r})"
    assert origin == PAGE_ORIGIN, f"no CORS for the page's origin: {origin!r}"

    url = urllib.parse.urljoin(WHEP, loc)
    req = urllib.request.Request(url, method="DELETE",
                                 headers={"Origin": PAGE_ORIGIN})
    with urllib.request.urlopen(req, timeout=15) as r:
        assert r.status == 200, f"WHEP DELETE answered {r.status}, want 200"
    # the session really went away, rather than the DELETE being a no-op
    with pytest.raises(urllib.error.HTTPError) as err:
        urllib.request.urlopen(
            urllib.request.Request(url, method="DELETE"), timeout=10)
    assert err.value.code == 404, "the session survived its own DELETE"
    err.value.close()
