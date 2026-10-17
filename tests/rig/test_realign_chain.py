"""The re-align chain (bundle, rotation refine, seam refine) on the stills
stored with the live map set, written to a temp dir: no cameras, no
daylight, and calibration/ is never touched (about 35 s of CPU on the rig)."""

import subprocess
from pathlib import Path

import pytest

cv2 = pytest.importorskip("cv2")

from afl_cam import stitch_maps, stitch_score  # noqa: E402
from afl_cam.config import ALIGN_PYTHON, ALIGN_SCRIPT, STITCH_CANVAS_WIDTH  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
MAPS_DIR = REPO / "calibration" / "stitch_live_full"


def test_realign_chain_on_stored_stills(tmp_path, capsys):
    live = stitch_maps.try_load(MAPS_DIR)
    raws = [MAPS_DIR / f"cam{i}.rgb" for i in (0, 1)]
    if live is None or not all(p.exists() for p in raws):
        pytest.skip(f"no live map set + re-align stills in {MAPS_DIR}")
    frames = [stitch_score.load_still(p, live.src) for p in raws]
    stills = [tmp_path / f"cam{i}.png" for i in (0, 1)]
    for frame, png in zip(frames, stills):
        cv2.imwrite(str(png), frame)

    out = tmp_path / "maps"
    run = subprocess.run(
        [ALIGN_PYTHON, str(ALIGN_SCRIPT), str(out), "--canvas-width", str(STITCH_CANVAS_WIDTH),
         "--stills", str(stills[0]), str(stills[1])],
        capture_output=True, text=True, timeout=1500)
    assert run.returncode == 0, run.stdout[-2000:] + run.stderr[-2000:]

    fresh = stitch_maps.load(out)
    assert fresh.src == live.src and fresh.canvas[0] == live.canvas[0]
    before, after = (stitch_score.score(*frames, m) for m in (live, fresh))
    with capsys.disabled():
        print(f"\n[realign-chain] live {before}\n[realign-chain] fresh {after}")
    assert after["misalign_tiles"] >= 10
    # the stored stills made the live set: a fresh fit must land close to it
    assert after["misalign_median"] < 40.0
    assert after["misalign_median"] <= before["misalign_median"] + 1.0
