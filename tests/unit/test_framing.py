"""Framing guides (Setup view): seam, overlap band and horizon read from a
live map set with the stdlib reader the daemon uses."""

import math

import numpy as np

from afl_cam import framing, stitch_maps

SW, SH, CW, CH, OFF1 = 64, 48, 96, 48, 32


def write_set(tmp_path, scale=30.0, tl_y=20.0):
    ramp = np.clip((56 - np.arange(CW)) * 255 / 16, 0, 255).astype(np.uint8)
    w0 = np.zeros((CH, CW), np.uint8)
    w0[:, :SW] = ramp[:SW]
    w1 = np.zeros((CH, CW), np.uint8)
    w1[:, OFF1:] = 255 - ramp[OFF1:]
    m = np.zeros((SH, SW, 2), np.float32)
    params = np.zeros(stitch_maps.PARAMS_FLOATS, np.float32)
    params[4] = scale
    for cam in (0, 1):
        params[8 + stitch_maps.CAM_BLOCK * cam + 10] = tl_y
    stitch_maps.write(tmp_path, stitch_maps.LiveMaps(
        canvas=(CW, CH), src=(SW, SH), params=params,
        cams=[stitch_maps.CameraMap(m, (0, 0), w0),
              stitch_maps.CameraMap(m, (OFF1, 0), w1)]))
    return w0, w1


def test_seam_overlap_and_horizon(tmp_path):
    w0, w1 = write_set(tmp_path)
    f = framing.framing(tmp_path)
    assert f["canvas"] == [CW, CH]
    both = np.nonzero((w0[0] > 0) & (w1[0] > 0))[0]
    assert f["overlap"] == [round(both[0] / CW, 4), round(both[-1] / CW, 4)]
    seam = next(x for x in both if w1[0, x] >= w0[0, x])
    assert f["seam"] == round(seam / CW, 4)
    assert both[0] < seam < both[-1]
    assert f["horizon"] == round((30 * math.pi / 2 - 20) / CH, 4)
    assert f["aligned_at"] > 0


def test_horizon_off_canvas_is_omitted(tmp_path):
    write_set(tmp_path, tl_y=-200.0)      # horizon far below the canvas
    f = framing.framing(tmp_path)
    assert f["horizon"] is None
    assert f["seam"] is not None          # seam falls back to the middle row


def test_missing_or_torn_set(tmp_path):
    assert framing.framing(tmp_path) is None
    (tmp_path / "params.bin").write_bytes(b"\0" * 10)
    assert framing.framing(tmp_path) is None
