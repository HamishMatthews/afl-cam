"""The rig's real map set: the offline re-stitch must reproduce, per pixel,
what cam2stitch's shader computes from the same maps, weights and gains
(w0*cam0 + w1*cam1 in float, no fixed-point maps). The reference is rendered
here from the live set itself, so it stays valid after refine_seam rewrites
the maps, weights and gains (dump_camera_params' ref_analytic.png is made
before that and goes stale). Pure CPU, no cameras."""

from pathlib import Path

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")

from afl_cam import stitch_maps, stitch_score, stitcher  # noqa: E402

MAPS_DIR = Path(__file__).resolve().parents[2] / "calibration" / "stitch_live_full"


def test_offline_restitch_matches_live_reference():
    live = stitch_maps.try_load(MAPS_DIR)
    raws = [MAPS_DIR / f"cam{i}.rgb" for i in (0, 1)]
    if live is None or not all(p.exists() for p in raws):
        pytest.skip("no live map set + re-align stills — run the re-align once")
    f0, f1 = (stitch_score.load_still(p, live.src) for p in raws)

    vs = stitcher.VideoStitcher(output_width=0, maps_dir=MAPS_DIR)
    assert vs.calibrate(f0, f1)
    out = vs.stitch(f0, f1)

    (v0, v1), _ = stitch_score.warp_views(f0, f1, live)       # float maps, gains applied
    w0, w1 = (c.weight[:, :, None].astype(np.float32) / 255 for c in live.cams)
    ref = np.clip(v0 * w0 + v1 * w1, 0, 255).astype(np.uint8)
    assert out.shape == ref.shape
    diff = np.abs(out.astype(np.int16) - ref.astype(np.int16))
    assert diff.mean() < 0.5, f"mean diff {diff.mean():.3f}"
    assert np.percentile(diff, 99.9) <= 3
