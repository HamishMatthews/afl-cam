"""Seam refinement removes depth-dependent ghosting from the maps.

The synthetic rig (test_stitch_score) gets a parallax-like field: cam1's
view is displaced more toward the bottom of the frame (near ground) than
the top, as on the real rig. Refining must bring the score near zero and
leave each camera's far, non-overlap side untouched."""

import numpy as np

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
