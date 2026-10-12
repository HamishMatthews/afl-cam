"""The stitch score reads known ghosting and exposure steps correctly.

Synthetic rig: two cameras see adjacent windows of one textured "world",
overlapping by half a frame. Identity maps place them on the canvas so the
overlap lines up exactly; offsetting cam1's map by (dx, dy) injects exactly
that much ghosting, and scaling cam1's brightness injects an exposure step."""

import cv2
import numpy as np
import pytest

from afl_cam import stitch_maps, stitch_score

SW, SH = 512, 384
OFF1 = 256               # cam1 starts half a frame right → 256 px overlap
CW = OFF1 + SW


def world():
    rng = np.random.default_rng(7)
    w = np.zeros((SH + 32, CW + 32), np.float32)
    for sigma, amp in ((24, 90), (10, 50), (4, 25)):   # survives the sigma-8 blur
        w += amp * cv2.GaussianBlur(rng.standard_normal(w.shape).astype(np.float32), (0, 0), sigma) * sigma
    w = (w - w.min()) / (w.max() - w.min()) * 180 + 40
    return cv2.cvtColor(w.astype(np.uint8), cv2.COLOR_GRAY2BGR)


def rig(dx=0.0, dy=0.0, gain=1.0):
    wd = world()
    f0 = wd[16:16 + SH, 16:16 + SW]
    f1 = np.clip(wd[16:16 + SH, 16 + OFF1:16 + OFF1 + SW].astype(np.float32) * gain,
                 0, 255).astype(np.uint8)
    xs, ys = np.meshgrid(np.arange(SW, dtype=np.float32), np.arange(SH, dtype=np.float32))
    m0 = np.stack([xs, ys], -1)
    m1 = np.stack([xs + dx, ys + dy], -1)
    w0 = np.zeros((SH, CW), np.uint8)
    w0[:, :OFF1 + SW // 4] = 255
    w1 = 255 - w0
    live = stitch_maps.LiveMaps(
        canvas=(CW, SH), src=(SW, SH), params=np.zeros(64, np.float32),
        cams=[stitch_maps.CameraMap(m0, (0, 0), w0), stitch_maps.CameraMap(m1, (OFF1, 0), w1)])
    return f0, f1, live


def test_aligned_rig_scores_near_zero():
    s = stitch_score.score(*rig())
    assert s["overlap_box"] == [0, SH, OFF1, SW]
    assert s["misalign_tiles"] >= 10
    assert s["misalign_median"] < 0.3
    assert abs(s["luma_step"]) < 1 and abs(s["luma_ratio"] - 1) < 0.01


@pytest.mark.parametrize("dx,dy", [(0.5, 0.0), (0.0, 1.0), (2.0, 0.0), (0.0, -2.0), (4.0, -3.0), (12.0, 5.0)])
def test_injected_ghosting_is_measured(dx, dy):
    s = stitch_score.score(*rig(dx, dy))
    assert s["misalign_median"] == pytest.approx(np.hypot(dx, dy), abs=0.15)


def test_exposure_step_is_measured():
    s = stitch_score.score(*rig(gain=1.2))
    assert s["luma_ratio"] == pytest.approx(1.2, abs=0.03)
    assert s["luma_step"] > 10
    assert s["misalign_median"] < 0.5    # a gain step alone is not ghosting


def test_seam_crop_written(tmp_path):
    f0, f1, live = rig(3.0)
    out = tmp_path / "seam.jpg"
    stitch_score.render_seam_crop(f0, f1, live, out)
    img = cv2.imread(str(out))
    assert img.shape == (2 * SH, SW - OFF1, 3)
