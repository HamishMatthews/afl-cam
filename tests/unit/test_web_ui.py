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


def test_shell_has_state_banner_and_tab_bar():
    assert 'id="statebar"' in HTML and 'class="tabbar"' in HTML
    assert "safe-area-inset-bottom" in HTML
    assert "prefers-reduced-motion" in HTML


def test_crop_toggle_survives_the_layout():
    for i in ("crop-off", "crop-on", "crop-note", "mode-live", "mode-separate"):
        assert f'id="{i}"' in HTML


def test_schedule_takes_seconds():
    for i in ("sched-time", "sched-dur", "unit-s", "unit-m", "sched-set", "sched-clear"):
        assert f'id="{i}"' in HTML
    assert 'data-sec="30"' in HTML       # the 30 s test recording preset
