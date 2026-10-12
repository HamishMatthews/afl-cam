"""Stitch score on the rig's real map set + re-align stills: prints the
ghosting / exposure-step numbers into the CI log and uploads a seam crop
(rig-artifacts/stitch-score/). Pure CPU, no cameras."""

import json
from pathlib import Path

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")

from afl_cam import stitch_maps, stitch_score  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
MAPS_DIR = REPO / "calibration" / "stitch_live_full"
OUT = REPO / "rig-artifacts" / "stitch-score"


def test_rig_stitch_score(capsys):
    live = stitch_maps.try_load(MAPS_DIR)
    raws = [MAPS_DIR / f"cam{i}.rgb" for i in (0, 1)]
    if live is None or not all(p.exists() for p in raws):
        pytest.skip(f"no live map set + re-align stills in {MAPS_DIR}")
    sw, sh = live.src
    f0, f1 = (cv2.cvtColor(np.fromfile(p, np.uint8).reshape(sh, sw, 3), cv2.COLOR_RGB2BGR)
              for p in raws)

    s = stitch_score.score(f0, f1, live)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "score.json").write_text(json.dumps(s, indent=2) + "\n")
    stitch_score.render_seam_crop(f0, f1, live, OUT / "seam.jpg")
    with capsys.disabled():
        print(f"\n[stitch-score] {json.dumps(s)}")

    assert s["misalign_tiles"] >= 10, "overlap too textureless to score"
    # catastrophic-regression guard only (the pre-refinement bundle was
    # 13-70 px); the number itself is the deliverable
    assert s["misalign_median"] < 5.0
