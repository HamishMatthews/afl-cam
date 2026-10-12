"""The rig's real map set: the offline re-stitch must reproduce the
reference panorama dump_camera_params rendered from the same maps + weights
(the image cam2stitch was validated against). Pure CPU, no cameras."""

from pathlib import Path

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")

from afl_cam import stitch_maps, stitcher  # noqa: E402

MAPS_DIR = Path(__file__).resolve().parents[2] / "calibration" / "stitch_live_full"


def test_offline_restitch_matches_live_reference():
    live = stitch_maps.try_load(MAPS_DIR)
    if live is None:
        pytest.skip(f"no live map set in {MAPS_DIR}")
    ref = cv2.imread(str(MAPS_DIR / "ref_analytic.png"))
    raws = [MAPS_DIR / f"cam{i}.rgb" for i in (0, 1)]
    if ref is None or not all(p.exists() for p in raws):
        pytest.skip("re-align stills/reference missing — run the re-align once")
    sw, sh = live.src
    f0, f1 = (cv2.cvtColor(np.fromfile(p, np.uint8).reshape(sh, sw, 3), cv2.COLOR_RGB2BGR)
              for p in raws)

    vs = stitcher.VideoStitcher(output_width=0, maps_dir=MAPS_DIR)
    assert vs.calibrate(f0, f1)
    out = vs.stitch(f0, f1)
    assert out.shape == ref.shape
    diff = np.abs(out.astype(np.int16) - ref.astype(np.int16))
    assert diff.mean() < 0.5, f"mean diff {diff.mean():.3f}"
    assert np.percentile(diff, 99.9) <= 3
