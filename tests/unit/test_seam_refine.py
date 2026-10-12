"""Seam refinement removes depth-dependent ghosting from the maps.

The synthetic rig (test_stitch_score) gets a parallax-like field: cam1's
view is displaced more toward the bottom of the frame (near ground) than
the top, as on the real rig. Refining must bring the score near zero and
leave each camera's far, non-overlap side untouched."""

import numpy as np
import pytest

from afl_cam import seam_refine, stitch_score

from .test_stitch_score import OFF1, SH, SW, rig


def parallax_rig():
    f0, f1, live = rig()
    m1 = live.cams[1].map
    ys = np.arange(SH, dtype=np.float32)[:, None]
    m1[:, :, 0] += 3 + 9 * ys / SH          # 3 px at the top → 12 px at the bottom
    m1[:, :, 1] += 1.5 * ys / SH
    return f0, f1, live


def test_refine_removes_parallax_ghosting():
    f0, f1, live = parallax_rig()
    before = stitch_score.score(f0, f1, live)
    assert before["misalign_median"] > 6
    refined, b, after = seam_refine.refine(f0, f1, live)
    assert b == before
    assert after["misalign_median"] < 0.5
    assert after["misalign_p95"] < 1.0


def test_refine_leaves_far_sides_alone():
    f0, f1, live = parallax_rig()
    refined, *_ = seam_refine.refine(f0, f1, live)
    fade = SW - OFF1                       # overlap width
    # cam0's left edge is a full overlap-width away from the overlap → untouched
    np.testing.assert_array_equal(refined.cams[0].map[:, :OFF1 - fade + 1],
                                  live.cams[0].map[:, :OFF1 - fade + 1])
    np.testing.assert_allclose(refined.cams[1].map[:, -1], live.cams[1].map[:, -1], atol=0.1)


def test_aligned_rig_is_left_aligned():
    f0, f1, live = rig()
    _, before, after = seam_refine.refine(f0, f1, live)
    assert after["misalign_median"] < 0.3


def test_exposure_matched_in_the_middle(tmp_path):
    from afl_cam import stitch_maps, stitcher

    f0, f1, live = rig(gain=1.2)
    refined, before, after = seam_refine.refine(f0, f1, live)
    assert before["luma_ratio"] > 1.15
    assert abs(after["luma_ratio"] - 1) < 0.02
    assert refined.gain(0) == pytest.approx(1.2 ** 0.5, abs=0.02)
    assert refined.gain(0) * refined.gain(1) == pytest.approx(1.0, abs=1e-5)
    # the gains travel in params.bin to the shader and the offline stitcher
    stitch_maps.write(tmp_path, refined)
    loaded = stitch_maps.load(tmp_path)
    assert (loaded.gain(0), loaded.gain(1)) == pytest.approx((refined.gain(0), refined.gain(1)))
    vs = stitcher.VideoStitcher(output_width=0, maps_dir=tmp_path)
    assert vs.calibrate(f0, f1)
    out = vs.stitch(f0, f1).astype(float)
    # just either side of the blend: equal brightness now
    w = live.cams[0].weight[0]
    left = out[:, np.flatnonzero(w == 255)[-20:]].mean()
    right = out[:, np.flatnonzero((w == 0) & (np.arange(len(w)) >= OFF1))[:20]].mean()
    assert abs(left - right) < 0.05 * left


def test_old_params_without_gain_mean_unity():
    _, _, live = rig()
    assert live.gain(0) == live.gain(1) == 1.0


def wide_ramp(live, width=200):
    """The old weights: a wide linear ramp down the middle of the overlap."""
    from afl_cam import stitch_maps
    mid = (OFF1 + SW) / 2
    xs = np.arange(live.canvas[0], dtype=np.float32)
    a = np.clip((mid + width / 2 - xs) / width, 0, 1)
    w0 = np.tile(np.round(a * 255).astype(np.uint8), (SH, 1))
    w0[:, SW:] = 0
    w1 = np.where(np.arange(live.canvas[0]) >= OFF1, 255 - w0, 0).astype(np.uint8)
    cams = [stitch_maps.CameraMap(c.map, c.off, w) for c, w in zip(live.cams, (w0, w1))]
    return stitch_maps.LiveMaps(canvas=live.canvas, src=live.src, params=live.params, cams=cams)


def test_narrow_seam_cuts_visible_ghosting():
    f0, f1, live = rig(3.0, 0.0)               # 3 px residual everywhere
    live = wide_ramp(live)
    before = stitch_score.score(f0, f1, live)["seam_ghost"]
    placed = seam_refine.place_seam(f0, f1, live)
    after = stitch_score.score(f0, f1, placed)["seam_ghost"]
    assert after < 0.5 * before
    w0, w1 = (c.weight.astype(int) for c in placed.cams)
    ov = (slice(None), slice(OFF1, SW))
    assert np.all(w0[ov] + w1[ov] == 255)       # still a partition of unity
    ramp = ((w0[ov] > 0) & (w0[ov] < 255)).sum(axis=1)
    assert ramp.max() <= 64


def test_seam_routes_around_a_disagreement():
    f0, f1, live = rig()
    # an object only cam1 sees in the middle of the overlap (a player in
    # parallax / moving between exposures)
    f1 = f1.copy()
    f1[100:300, 150:190] = 255                 # cam1 frame x 150..190 = canvas 406..446
    placed = seam_refine.place_seam(f0, f1, wide_ramp(live))
    w0 = placed.cams[0].weight[100:300, 406:446].astype(int)
    w1 = placed.cams[1].weight[100:300, 406:446].astype(int)
    assert (np.minimum(w0, w1) == 0).mean() > 0.95   # never mixed across it
