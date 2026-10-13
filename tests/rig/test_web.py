"""The phone page: served, self-contained (no CDN — the ground AP has no
internet), and the player/proxy plumbing present."""

import urllib.request

from .conftest import BASE


def _page():
    with urllib.request.urlopen(BASE + "/", timeout=10) as r:
        return r.read().decode()


def test_page_serves_with_expected_markers():
    html = _page()
    assert "afl-cam" in html
    # bundled hls.js (Chrome/Firefox MSE fallback) — NOT a CDN reference
    assert "/hls.min.js" in html
    assert "cdn" not in html.lower().split("hls.min.js")[0][-200:], \
        "page must not reference a CDN for the player"
    # per-recording proxy generation for desktop-UI recordings
    assert "mkproxy" in html
    # button font consistency (anchors and buttons share .mini-btn styles)
    assert "font: inherit" in html
    # Setup view + Add to Home Screen
    assert 'id="tab-setup"' in html and "/api/realign" in html
    assert 'rel="manifest"' in html and 'rel="apple-touch-icon"' in html


def test_whep_client_shape():
    """Issue #40: offer then delete, and never two players at once.

    The old client GET the WHEP endpoint and fed the 405 JSON body to
    setRemoteDescription; every load then fell through to HLS.
    """
    html = _page()
    assert 'method: "POST"' in html and "/preview/whep" in html, \
        "the page must POST its own offer to the WHEP endpoint"
    assert '{type: "offer"' not in html, \
        "the page must not treat a GET body as the server's offer"
    assert "createOffer" in html and "iceGatheringState" in html, \
        "the offer must be gathered before it is posted (no trickle)"
    assert "pagehide" in html and 'method: "DELETE"' in html, \
        "the WHEP session must be DELETEd when the page goes away"
    assert html.count("new Hls(") == 1, \
        "a retry must not be able to stack a second Hls instance"


def test_hls_js_bundled_and_served():
    with urllib.request.urlopen(BASE + "/hls.min.js", timeout=10) as r:
        ctype = r.headers.get("Content-Type", "")
        body = r.read()
    assert ctype.startswith("text/javascript"), ctype
    assert len(body) > 100_000, "hls.min.js looks empty/truncated"
