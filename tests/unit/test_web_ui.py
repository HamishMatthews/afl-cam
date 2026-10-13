"""Static checks on the phone page (afl_cam/web/index.html): no browser needed."""

import re
from pathlib import Path

HTML = (Path(__file__).resolve().parents[2] / "afl_cam" / "web" / "index.html").read_text()
TABS = ["record", "recs", "setup"]


def test_every_looked_up_id_exists():
    ids = set(re.findall(r'\bid="([^"]+)"', HTML))
    used = set(re.findall(r'\$\("([^"]+)"\)', HTML))
    used |= {"tab-" + t for t in TABS}
    missing = sorted(i for i in used if i not in ids)
    assert not missing, f"JS looks up ids the markup lacks: {missing}"


def test_works_offline():
    # the rig's AP has no internet: no CDN scripts, styles or fonts
    for url in re.findall(r'(?:src|href)="(https?://[^"]+)"', HTML):
        raise AssertionError(f"external resource: {url}")
    assert "@import" not in HTML


def test_each_view_belongs_to_a_tab():
    views = set(re.findall(r'data-view="([^"]+)"', HTML))
    assert views <= set(TABS) | {"all"}
    assert set(TABS) <= views


def test_shell_has_one_status_indicator_and_tab_bar():
    # a single pill, top right: no second state bar, no separate live dot
    assert 'id="chip"' in HTML and 'class="tabbar"' in HTML
    assert 'id="statebar"' not in HTML and 'id="link"' not in HTML
    assert 'id="hint"' in HTML
    assert "safe-area-inset-bottom" in HTML
    assert "prefers-reduced-motion" in HTML


def test_crop_toggle_survives_the_layout():
    for i in ("crop-off", "crop-on", "crop-note", "mode-live", "mode-separate"):
        assert f'id="{i}"' in HTML


def _verdict(samples, last):
    """Run the page's readinessVerdict() in node (skips where node is absent)."""
    import json
    import shutil
    import subprocess

    import pytest
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    start = HTML.index("const RD_SECS")
    end = HTML.index("let rdSession")
    prog = (HTML[start:end]
            + f"\nconsole.log(JSON.stringify(readinessVerdict({json.dumps(samples)}, {json.dumps(last)})));")
    out = subprocess.run([node, "-e", prog], capture_output=True, text=True, check=True).stdout
    return {r["name"]: r["level"] for r in json.loads(out)}


def _samples(fps=30, skip=0, err=0, skew=10, mbps=80):
    mk = lambda t: {"t": t, "c0": int(fps * t), "c1": int(fps * t), "bytes": int(mbps * 1e6 / 8 * t),
                    "skip": int(skip * t / 10), "err": int(err * t / 10), "skew_ms": skew}
    return [mk(t) for t in range(0, 11)]


GOOD_LAST = {"ok": True, "has_proxy": True, "duration_s": 10.2}


def test_readiness_all_good():
    levels = _verdict(_samples(), GOOD_LAST)
    assert set(levels.values()) == {"ok"}, levels


def test_readiness_flags_each_problem():
    assert _verdict(_samples(fps=15), GOOD_LAST)["frame rate"] == "warn"
    assert _verdict(_samples(fps=5), GOOD_LAST)["frame rate"] == "fail"
    assert _verdict(_samples(skew=100), GOOD_LAST)["pair skew"] == "fail"
    # the cam2stitch skip counter is a pairing statistic, not lost frames (#55)
    assert "dropped frames" not in _verdict(_samples(skip=100), GOOD_LAST)
    assert set(_verdict(_samples(skip=100), GOOD_LAST).values()) == {"ok"}
    assert _verdict(_samples(err=3), GOOD_LAST)["camera errors"] == "warn"
    assert _verdict(_samples(mbps=0), GOOD_LAST)["bitrate"] == "fail"
    assert _verdict(_samples(), None)["saved"] == "fail"
    assert _verdict(_samples(), {**GOOD_LAST, "ok": False})["saved"] == "fail"
    assert _verdict(_samples(), {**GOOD_LAST, "duration_s": 3})["duration"] == "warn"


def test_readiness_without_samples_fails():
    assert _verdict([], GOOD_LAST) == {"recording": "fail"}


def test_schedule_takes_minutes_and_seconds_together():
    for i in ("sched-time", "sched-min", "sched-sec", "sched-set", "sched-clear"):
        assert f'id="{i}"' in HTML
    assert 'id="unit-s"' not in HTML and 'id="unit-m"' not in HTML
    assert 'data-sec="30"' in HTML       # the 30 s test recording preset
    assert 'data-sec="8100"' not in HTML  # no 135 minute preset


def _copy_text():
    """Visible copy: the page's text nodes plus the tooltips, scripts and styles removed."""
    body = re.sub(r"<(script|style)\b.*?</\1>", "", HTML, flags=re.S)
    return re.sub(r"<[^>]+>", " ", body)


def test_copy_has_no_em_dashes_or_semicolons():
    text = _copy_text()
    assert "\u2014" not in text and ";" not in text.replace("&nbsp;", "")
    code = re.sub(r"/\*.*?\*/|(?<![:\"'])//[^\n]*", "", HTML, flags=re.S)
    assert "\u2014" not in code, "em dash in a script or style string"


def test_explanations_sit_behind_info_controls():
    assert HTML.count('class="info"') >= 2
    assert 'id="mode-note"' not in HTML and "dim light" not in HTML


def test_separate_mode_has_one_pane_per_camera():
    for i in ("pane0", "pane1", "snap0", "snap1"):
        assert f'id="{i}"' in HTML


def test_preview_has_full_screen_controls():
    for i in ("pv-full", "pv-close", "pv-hud", "pv-reset", "stage"):
        assert f'id="{i}"' in HTML
    assert "touch-action: none" in HTML       # pinch must not scroll the page


