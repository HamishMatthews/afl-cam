"""Stitch score on the rig's real map set + re-align stills: prints the
ghosting / exposure-step numbers into the CI log and uploads a seam crop
(rig-artifacts/stitch-score/). Pure CPU, no cameras."""

import json
from pathlib import Path

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")

from afl_cam import stitch_maps, stitch_score, stitcher  # noqa: E402

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


def _unrefined(live):
    """The live set as dump_camera_params wrote it, before refine_seam: the
    live set itself when it was never refined (gains unset), otherwise its
    .prev maps with the dump's blend weights (one owner where only one
    camera has a pixel, a 300 px ramp where both do) and no gains. None when
    a refined set has no .prev maps."""
    if not (live.params[5] or live.params[6]):
        return live
    prev = [MAPS_DIR / f"map{i}.bin.prev" for i in (0, 1)]
    if not all(p.exists() for p in prev):
        return None
    maps = [np.fromfile(p, np.float32) for p in prev]
    if any(m.size != c.map.size for m, c in zip(maps, live.cams)):
        return None
    maps = [m.reshape(c.map.shape) for m, c in zip(maps, live.cams)]
    valid = [m[:, :, 0] >= 0 for m in maps]
    (ax, ay), (bx, by) = (c.off for c in live.cams)
    oy0, oy1 = max(ay, by), min(ay + maps[0].shape[0], by + maps[1].shape[0])
    ox0, ox1 = max(ax, bx), min(ax + maps[0].shape[1], bx + maps[1].shape[1])
    ramps = stitcher._build_uint8_weights(valid[0][oy0 - ay:oy1 - ay, ox0 - ax:ox1 - ax],
                                          valid[1][oy0 - by:oy1 - by, ox0 - bx:ox1 - bx], 300)
    cw, ch = live.canvas
    cams = []
    for (x, y), m, v, ramp in zip((c.off for c in live.cams), maps, valid, ramps):
        w = np.zeros((ch, cw), np.uint8)
        w[y:y + m.shape[0], x:x + m.shape[1]] = v.astype(np.uint8) * 255
        w[oy0:oy1, ox0:ox1] = ramp
        cams.append(stitch_maps.CameraMap(m, (x, y), w))
    params = live.params.copy()
    params[5:7] = 0
    return stitch_maps.LiveMaps(canvas=live.canvas, src=live.src, params=params, cams=cams)


def test_rig_seam_refine_improves_score(capsys):
    """Seam refinement on the real stills, in memory (the live maps are not
    touched): the refined maps must ghost less than the unrefined ones. A
    re-align refines a fresh set once, so a live set that is already refined
    starts from its pre-refine maps instead of being refined twice."""
    from afl_cam import seam_refine

    live = stitch_maps.try_load(MAPS_DIR)
    raws = [MAPS_DIR / f"cam{i}.rgb" for i in (0, 1)]
    if live is None or not all(p.exists() for p in raws):
        pytest.skip(f"no live map set + re-align stills in {MAPS_DIR}")
    start = _unrefined(live)
    if start is None:
        pytest.skip("live set is seam-refined and its map*.bin.prev are missing or stale")
    f0, f1 = (stitch_score.load_still(p, live.src) for p in raws)

    refined, before, after = seam_refine.refine(f0, f1, start)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "refined_score.json").write_text(json.dumps(after, indent=2) + "\n")
    stitch_score.render_seam_crop(f0, f1, refined, OUT / "seam_refined.jpg")
    with capsys.disabled():
        print(f"\n[seam-refine] before {json.dumps(before)}\n[seam-refine] after  {json.dumps(after)}")
    assert after["misalign_median"] < before["misalign_median"]
    assert abs(after["luma_ratio"] - 1) < abs(before["luma_ratio"] - 1)
    assert after["seam_ghost"] < before["seam_ghost"]
