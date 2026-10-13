"""Framing options on the rig's real calibration: prints the comparison
table into the CI log and uploads the picture alongside the stitch score
(rig-artifacts/stitch-score/framing.png). Pure CPU, no cameras."""

from pathlib import Path

import pytest

from afl_cam import framing_options

REPO = Path(__file__).resolve().parents[2]
CAMS = REPO / "calibration" / "stitch_live_full" / "cams.txt"
OUT = REPO / "rig-artifacts" / "stitch-score"


def test_rig_framing_options(capsys):
    if not CAMS.exists():
        pytest.skip(f"no {CAMS}")
    rows, panels = framing_options.evaluate(framing_options.load_cams(CAMS))
    OUT.mkdir(parents=True, exist_ok=True)
    framing_options.montage(panels, OUT / "framing.png")
    (OUT / "framing.md").write_text(framing_options.table(rows) + "\n")
    with capsys.disabled():
        print("\n[framing-options]\n" + framing_options.table(rows))
    assert len(rows) == 9
