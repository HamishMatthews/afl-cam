"""The phone page: served, self-contained (no CDN — the ground AP has no
internet), and the player/proxy plumbing present."""

import urllib.request

from .conftest import BASE


def test_page_serves_with_expected_markers():
    with urllib.request.urlopen(BASE + "/", timeout=10) as r:
        html = r.read().decode()
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


def test_hls_js_bundled_and_served():
    with urllib.request.urlopen(BASE + "/hls.min.js", timeout=10) as r:
        ctype = r.headers.get("Content-Type", "")
        body = r.read()
    assert ctype.startswith("text/javascript"), ctype
    assert len(body) > 100_000, "hls.min.js looks empty/truncated"
