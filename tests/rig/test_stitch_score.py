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
    # catastrophic-regression guard only; the number itself is the
    # deliverable. First measurement with the accurate ruler (2026-10-12):
    # median 18 px, p95 33 px on the re-align stills.
    assert s["misalign_median"] < 40.0


def test_rig_seam_refine_improves_score(capsys):
    """Seam refinement on the real stills, in memory (the live maps are not
    touched): the refined maps must ghost less than the current ones."""
    from afl_cam import seam_refine

    live = stitch_maps.try_load(MAPS_DIR)
    raws = [MAPS_DIR / f"cam{i}.rgb" for i in (0, 1)]
    if live is None or not all(p.exists() for p in raws):
        pytest.skip(f"no live map set + re-align stills in {MAPS_DIR}")
    f0, f1 = (stitch_score.load_still(p, live.src) for p in raws)

    refined, before, after = seam_refine.refine(f0, f1, live)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "refined_score.json").write_text(json.dumps(after, indent=2) + "\n")
    stitch_score.render_seam_crop(f0, f1, refined, OUT / "seam_refined.jpg")
    with capsys.disabled():
        print(f"\n[seam-refine] before {json.dumps(before)}\n[seam-refine] after  {json.dumps(after)}")
    assert after["misalign_median"] < before["misalign_median"]
    assert abs(after["luma_ratio"] - 1) < abs(before["luma_ratio"] - 1)
    assert after["seam_ghost"] < before["seam_ghost"]
