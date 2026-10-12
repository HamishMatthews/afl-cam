"""The lens model: distortion maths against OpenCV, the maps that bake it in,
the committed intrinsics, and a fit that recovers a known lens."""

import types

import cv2
import numpy as np
import pytest

from afl_cam import lens, stitcher

K = np.array([[2237.0, 0, 1950.0], [0, 2236.0, 1090.0], [0, 0, 1]])
DIST = np.array([0.0712, -0.1037, 0.0004, -0.0003, 0.0436])    # tangential on, to cover the full model


def random_pixels(n=500, seed=0):
    rng = np.random.default_rng(seed)
    return np.stack([rng.uniform(0, 3840, n), rng.uniform(0, 2160, n)], axis=1)


def test_distort_matches_opencv():
    pts = random_pixels()
    xn, yn = (pts[:, 0] - K[0, 2]) / K[0, 0], (pts[:, 1] - K[1, 2]) / K[1, 1]
    ref, _ = cv2.projectPoints(np.stack([xn, yn, np.ones_like(xn)], 1), np.zeros(3), np.zeros(3), K, DIST)
    x, y = lens.distort_pixels(pts[:, 0], pts[:, 1], K, DIST)
    np.testing.assert_allclose(np.stack([x, y], 1), ref.reshape(-1, 2), atol=1e-6)


def test_undistort_inverts_distort_and_matches_opencv():
    pts = random_pixels(seed=1)
    x, y = lens.undistort_pixels(pts[:, 0], pts[:, 1], K, DIST)
    bx, by = lens.distort_pixels(x, y, K, DIST)
    np.testing.assert_allclose(np.stack([bx, by], 1), pts, atol=1e-6)
    ref = cv2.undistortPoints(pts.reshape(-1, 1, 2), K, DIST, None, K)
    np.testing.assert_allclose(np.stack([x, y], 1), ref.reshape(-1, 2), atol=1e-3)


def test_unproject_gives_unit_depth_rays():
    pts = random_pixels(seed=2)
    rays = lens.unproject(pts, K, DIST)
    assert np.all(rays[:, 2] == 1)
    x, y = lens.distort_pixels(K[0, 0] * rays[:, 0] + K[0, 2], K[1, 1] * rays[:, 1] + K[1, 2], K, DIST)
    np.testing.assert_allclose(np.stack([x, y], 1), pts, atol=1e-6)


@pytest.mark.parametrize("dist", [None, np.zeros(5)])
def test_no_distortion_is_the_exact_identity(dist):
    x, y = np.array([1.5, 3000.25]), np.array([7.0, 2000.125])
    assert lens.distort_pixels(x, y, K, dist) == (x, y)
    assert lens.undistort_pixels(x, y, K, dist) == (x, y)


def test_far_outside_the_frame_fails_loudly():
    with pytest.raises(ValueError, match="did not converge"):
        lens.undistort_pixels(np.array([1e5]), np.array([1e5]), K, DIST)


def test_a_folding_distortion_is_refused():
    lens.check_monotonic(DIST)
    with pytest.raises(ValueError, match="folds back"):
        lens.check_monotonic(np.array([0.0, -0.5, 0, 0, 0]))


def fake_cam(yaw_deg=0.0):
    a = np.radians(yaw_deg)
    yaw = np.array([[np.cos(a), 0, np.sin(a)], [0, 1, 0], [-np.sin(a), 0, np.cos(a)]])
    pitch = cv2.Rodrigues(np.array([np.radians(2.0), 0, 0]))[0]      # level rigs still sit a little off
    return types.SimpleNamespace(focal=K[0, 0], ppx=K[0, 2], ppy=K[1, 2], aspect=K[1, 1] / K[0, 0], R=yaw @ pitch)


def test_spherical_maps_hold_raw_pixels_through_the_lens():
    cam, size, ws = fake_cam(20.0), (3840, 2160), K[0, 0]
    plain = stitcher._spherical_maps(cam, 1.0, size, 1.0, None, ws)
    zero = stitcher._spherical_maps(cam, 1.0, size, 1.0, None, ws, np.zeros(5))
    np.testing.assert_array_equal(plain[0], zero[0])      # no lens: the old maps, bit for bit
    np.testing.assert_array_equal(plain[1], zero[1])

    mx, my = stitcher._spherical_maps(cam, 1.0, size, 1.0, None, ws, DIST)
    tl_x, tl_y, _, _ = stitcher._spherical_bounds(cam, 1.0, size, ws, DIST)
    for r, c in ((700, 900), (500, 2500), (300, 1500)):          # canvas pixels, away from the border
        u, v = (c + tl_x) / ws, (r + tl_y) / ws
        world = np.array([np.sin(np.pi - v) * np.sin(u), np.cos(np.pi - v), np.sin(np.pi - v) * np.cos(u)])
        ray = cam.R.T @ world
        ref, _ = cv2.projectPoints(ray.reshape(1, 1, 3), np.zeros(3), np.zeros(3), K, DIST)
        assert mx[r, c] == pytest.approx(ref[0, 0, 0], abs=1e-2)   # float32 maps
        assert my[r, c] == pytest.approx(ref[0, 0, 1], abs=1e-2)
    assert not np.array_equal(mx, plain[0])


def test_bounds_follow_the_undistorted_frame_edge():
    cam, size, ws = fake_cam(), (3840, 2160), K[0, 0]
    flat = stitcher._spherical_bounds(cam, 1.0, size, ws)
    bent = stitcher._spherical_bounds(cam, 1.0, size, ws, DIST)
    assert bent != flat       # the lens moves where the frame edge points


def test_committed_intrinsics_are_sane():
    for name in ("cam0", "cam1"):
        c = lens.load(name)
        assert c["size"] == (3840, 2160)
        assert 2100 < c["mtx"][0, 0] < 2400 and abs(c["mtx"][0, 0] - c["mtx"][1, 1]) < 10
        assert abs(c["mtx"][0, 2] - 1920) < 100 and abs(c["mtx"][1, 2] - 1080) < 100
        assert c["dist"].shape == (5,) and not c["dist"][2:4].any()    # radial only
        lens.check_monotonic(c["dist"])


def synthetic_views(rng, n=16, noise=0.08):
    """Corner grids of a 10x7 board seen through DIST/K at assorted poses."""
    objp = np.zeros((70, 3))
    objp[:, :2] = np.mgrid[0:10, 0:7].T.reshape(-1, 2)
    views = []
    while len(views) < n:
        rvec = np.radians(rng.uniform(-30, 30, 3)) * [1, 1, 0.3]
        tvec = np.array([rng.uniform(-20, 20), rng.uniform(-11, 11), rng.uniform(22, 34)]) - [4.5, 3, 0]
        pts, _ = cv2.projectPoints(objp, rvec, tvec, K, DIST * [1, 1, 0, 0, 1])
        pts = pts.reshape(-1, 2)
        if pts[:, 0].min() > 20 and pts[:, 0].max() < 3820 and pts[:, 1].min() > 20 and pts[:, 1].max() < 2140:
            views.append(pts + rng.normal(0, noise, pts.shape))
    return views


def test_fit_recovers_a_known_lens_and_drops_bad_corners():
    rng = np.random.default_rng(3)
    views = synthetic_views(rng)
    views[0][:25] += rng.uniform(-4, 4, (25, 2)) + 3      # a bezel-hugging, blurred detection
    got = lens.fit(views, (3840, 2160))
    assert got["rms"] < 0.2
    assert sum(got["rejected"]) >= 20 and got["rejected"][0] >= 20
    assert got["K"][0, 0] == pytest.approx(K[0, 0], rel=2e-3)
    # what matters is where rays land at the frame edge, not the coefficients themselves
    xn = np.array([0.0, 0.5, 0.85])

    def edge(d):
        return lens.distort_pixels(K[0, 2] + K[0, 0] * xn, np.full(3, K[1, 2]), K, d)[0]
    np.testing.assert_allclose(edge(got["dist"]), edge(DIST * [1, 1, 0, 0, 1]), atol=1.0)
    assert not got["dist"][2:4].any()


def test_coverage_warns_about_central_boards_only():
    rng = np.random.default_rng(4)
    central = [np.array([[1700.0, 900.0]]) + rng.uniform(0, 400, (70, 2)) for _ in range(12)]
    report = lens.coverage(central, {"size": (3840, 2160), "tilts": [5.0] * 12})
    text = "\n".join(report)
    assert "fewer than 8 boards reach an edge strip" in text and "tilted 25+ deg" in text
    wide = [np.array([[x, 500.0]]) + rng.uniform(0, 300, (70, 2)) for x in [50] * 9 + [3400] * 9]
    assert "WARNING" not in "\n".join(lens.coverage(wide, {"size": (3840, 2160), "tilts": [30.0] * 18}))


def test_detects_a_rendered_board_to_a_fraction_of_a_pixel():
    board = np.kron((np.indices((8, 11)).sum(0) % 2).astype(np.uint8) * 255, np.ones((120, 120), np.uint8))
    board = cv2.copyMakeBorder(board, 120, 120, 120, 120, cv2.BORDER_CONSTANT, value=255)
    src = np.float32([[0, 0], [1560, 0], [1560, 1200], [0, 1200]])
    dst = np.float32([[300, 200], [1500, 260], [1450, 1000], [330, 900]])
    H = cv2.getPerspectiveTransform(src, dst)
    img = cv2.warpPerspective(board, H, (1920, 1080), flags=cv2.INTER_LINEAR, borderValue=255)
    inner = np.stack(np.meshgrid(np.arange(1, 11), np.arange(1, 8)), -1).reshape(-1, 2) * 120.0 + 120
    want = cv2.perspectiveTransform(inner.reshape(-1, 1, 2).astype(np.float32), H).reshape(-1, 2)
    got = lens.detect_corners(img)
    assert got is not None
    err = min(np.abs(got - want).max(), np.abs(got[::-1] - want).max())     # grid is symmetric: either order is valid
    assert err < 0.5
