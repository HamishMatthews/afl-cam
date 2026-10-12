"""The live map set is the one geometry: what dump_camera_params writes,
cam2stitch reads, and the offline re-stitch must reproduce per pixel
(out = w0*cam0(map0) + w1*cam1(map1), the stitch_nv12.comp formula)."""

import cv2
import numpy as np
import pytest

from afl_cam import stitch_maps, stitcher

SW, SH = 64, 48          # raw camera frame
CW, CH = 96, 48          # canvas: cam1's ROI starts at x=32
OFF1 = 32


def synthetic_maps():
    xs, ys = np.meshgrid(np.arange(SW, dtype=np.float32), np.arange(SH, dtype=np.float32))
    m0 = np.stack([xs, ys], axis=-1)
    m1 = np.stack([xs + 0.5, ys + 0.25], axis=-1)   # sub-pixel: exercises interpolation
    m0[-4:, :4] = -1                               # a corner with no source
    ramp = np.clip((56 - np.arange(CW)) * 255 / 16, 0, 255).astype(np.uint8)
    w0 = np.zeros((CH, CW), np.uint8)
    w0[:, :SW] = ramp[:SW]
    w0[-4:, :4] = 0
    w1 = np.zeros((CH, CW), np.uint8)
    w1[:, OFF1:] = 255 - ramp[OFF1:]
    params = np.zeros(stitch_maps.PARAMS_FLOATS, np.float32)
    return stitch_maps.LiveMaps(
        canvas=(CW, CH), src=(SW, SH), params=params,
        cams=[stitch_maps.CameraMap(m0, (0, 0), w0),
              stitch_maps.CameraMap(m1, (OFF1, 0), w1)])


def gpu_reference(maps, f0, f1):
    """Per-pixel w0*cam0 + w1*cam1 over the whole canvas, float maps."""
    acc = np.zeros((CH, CW, 3), np.float32)
    for cam, frame in zip(maps.cams, (f0, f1)):
        mh, mw = cam.map.shape[:2]
        warp = cv2.remap(frame, cam.map[:, :, 0], cam.map[:, :, 1], cv2.INTER_LINEAR,
                         borderMode=cv2.BORDER_CONSTANT, borderValue=0)
        full = np.zeros((CH, CW, 3), np.float32)
        x0, y0 = cam.off
        full[y0:y0 + mh, x0:x0 + mw] = warp
        acc += full * (cam.weight[:, :, None] / 255.0)
    return acc


def frames():
    rng = np.random.default_rng(1)
    smooth = lambda: cv2.GaussianBlur(rng.integers(0, 256, (SH, SW, 3), np.uint8), (5, 5), 0)
    return smooth(), smooth()


def test_write_load_round_trip(tmp_path):
    maps = synthetic_maps()
    stitch_maps.write(tmp_path, maps)
    got = stitch_maps.load(tmp_path)
    assert got.canvas == (CW, CH) and got.src == (SW, SH)
    for a, b in zip(maps.cams, got.cams):
        assert a.off == b.off
        np.testing.assert_array_equal(a.map, b.map)
        np.testing.assert_array_equal(a.weight, b.weight)
    # the raw layout cam2stitch reads: RGBA8 weights, float32 (x,y) maps
    assert (tmp_path / "w0.bin").stat().st_size == CW * CH * 4
    assert (tmp_path / "map1.bin").stat().st_size == SW * SH * 2 * 4
    assert not list(tmp_path.glob("*.tmp"))


def test_torn_set_is_rejected(tmp_path):
    stitch_maps.write(tmp_path, synthetic_maps())
    (tmp_path / "map1.bin").write_bytes(b"")   # a crashed re-align
    assert stitch_maps.try_load(tmp_path) is None
    assert stitch_maps.try_load(tmp_path / "missing") is None


def test_offline_stitch_matches_live_formula(tmp_path):
    maps = synthetic_maps()
    stitch_maps.write(tmp_path, maps)
    f0, f1 = frames()
    vs = stitcher.VideoStitcher(output_width=0, maps_dir=tmp_path)
    assert vs.calibrate(f0, f1)
    assert vs.output_shape == (CH, CW)
    out = vs.stitch(f0, f1).astype(np.float32)
    ref = gpu_reference(maps, f0, f1)
    # fixed-point remap + uint8 truncation vs float: within a couple of LSB
    assert np.abs(out - ref).max() <= 2.0
    assert out[-4:, :4].max() == 0          # no source → black, as on the GPU


def test_frame_size_must_match_maps(tmp_path):
    stitch_maps.write(tmp_path, synthetic_maps())
    f0, f1 = frames()
    vs = stitcher.VideoStitcher(output_width=0, maps_dir=tmp_path)
    with pytest.raises(stitcher.StitchError, match="expect 64x48"):
        vs.calibrate(cv2.resize(f0, (32, 24)), cv2.resize(f1, (32, 24)))
