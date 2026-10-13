"""Crop of the live map set: the rectangle is fully covered and as large as
the mask allows, and the cropped set keeps every pixel's source and weight."""

import json
import subprocess
import sys

import numpy as np

from afl_cam import crop_maps, framing, stitch_maps

SW, SH = 64, 48
CW, CH = 96, 48


def make_live(tmp_path, name="live"):
    """cam0's ROI is 60x40 at (0, 4) and cam1's 56x44 at (40, 0). Each map
    points into the frame except a corner wedge per camera (the curved edge)."""
    def cam_map(w, h, wedge):
        xs, ys = np.meshgrid(np.arange(w, dtype=np.float32) % (SW - 1),
                             np.arange(h, dtype=np.float32) % (SH - 1))
        m = np.stack([xs + 2, ys + 2], axis=2).astype(np.float32)
        m[:, :, 0] = np.clip(m[:, :, 0], 3, SW - 4)
        m[:, :, 1] = np.clip(m[:, :, 1], 3, SH - 4)
        yy, xx = np.mgrid[0:h, 0:w]
        m[wedge(yy, xx)] = -1
        return m
    m0 = cam_map(60, 40, lambda yy, xx: yy + xx < 12)            # top-left corner missing
    m1 = cam_map(56, 44, lambda yy, xx: (yy - 43) + (55 - xx) > -10)   # bottom-right corner missing
    w0 = np.zeros((CH, CW), np.uint8)
    w0[:, :60] = np.linspace(255, 0, 60).astype(np.uint8)
    w1 = (255 - w0).astype(np.uint8)
    params = np.zeros(stitch_maps.PARAMS_FLOATS, np.float32)
    params[4] = 30.0
    for i, tl_y in enumerate((20.0, 18.0)):
        params[8 + stitch_maps.CAM_BLOCK * i + 10] = tl_y
    live = stitch_maps.LiveMaps(canvas=(CW, CH), src=(SW, SH), params=params, cams=[
        stitch_maps.CameraMap(m0, (0, 4), w0), stitch_maps.CameraMap(m1, (40, 0), w1)])
    stitch_maps.write(tmp_path / name, live)
    return stitch_maps.load(tmp_path / name)


def test_largest_rectangle_known_mask():
    m = np.zeros((6, 10), bool)
    m[1:5, 2:9] = True
    m[0, 0:3] = True
    m[5, 4] = True
    assert crop_maps.largest_rectangle(m) == (2, 1, 7, 4)
    assert crop_maps.largest_rectangle(np.zeros((3, 3), bool))[2] == 0


def test_crop_rect_is_covered_aligned_and_maximal(tmp_path, monkeypatch):
    monkeypatch.setattr(crop_maps, "COARSE", 2)
    live = make_live(tmp_path)
    cov = crop_maps.coverage(live)
    x, y, w, h = crop_maps.crop_rect(cov)
    assert cov[y:y + h, x:x + w].all()
    assert w % 4 == 0 and h % 2 == 0
    # brute force: nothing larger that is also aligned
    best = 0
    for yy in range(CH):
        for hh in range(2, CH - yy + 1, 2):
            row_ok = cov[yy:yy + hh].all(axis=0)
            run = 0
            for v in row_ok:
                run = run + 1 if v else 0
                best = max(best, hh * (run // 4 * 4))
    assert w * h == best


def test_crop_keeps_sources_weights_and_horizon(tmp_path, monkeypatch):
    monkeypatch.setattr(crop_maps, "COARSE", 2)
    live = make_live(tmp_path)
    rect = crop_maps.crop_rect(crop_maps.coverage(live))
    x, y, w, h = rect
    cropped = crop_maps.crop(live, rect)
    assert cropped.canvas == (w, h)
    # every canvas pixel still takes the same source pixel and weight
    for orig, new in zip(live.cams, cropped.cams):
        assert np.array_equal(new.weight, orig.weight[y:y + h, x:x + w])
        ox, oy = new.off
        mh, mw = new.map.shape[:2]
        assert np.array_equal(new.map, orig.map[y + oy - orig.off[1]:y + oy - orig.off[1] + mh,
                                                x + ox - orig.off[0]:x + ox - orig.off[0] + mw])
    assert crop_maps.coverage(cropped).all()
    # the horizon guide lands on the same ground, shifted by the crop
    stitch_maps.write(tmp_path / "c", cropped)
    old = framing.horizon_row(framing.read_params(tmp_path / "live"))
    new = framing.horizon_row(framing.read_params(tmp_path / "c"))
    assert abs((old - y) - new) < 1e-3


def test_make_crop_set_reuses_until_the_live_set_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(crop_maps, "COARSE", 2)
    make_live(tmp_path)
    a = crop_maps.make_crop_set(tmp_path / "live", tmp_path / "crop")
    assert not a["reused"] and stitch_maps.load(tmp_path / "crop").canvas == tuple(a["canvas"])
    assert crop_maps.make_crop_set(tmp_path / "live", tmp_path / "crop")["reused"]
    make_live(tmp_path)                       # rewritten maps (a re-align) -> new signature
    assert not crop_maps.make_crop_set(tmp_path / "live", tmp_path / "crop")["reused"]


def test_cli_and_missing_set(tmp_path):
    make_live(tmp_path)
    run = subprocess.run([sys.executable, "-m", "afl_cam.crop_maps",
                          str(tmp_path / "live"), str(tmp_path / "crop")],
                         capture_output=True, text=True)
    assert run.returncode == 0 and json.loads(run.stdout)["canvas"][0] % 4 == 0
    bad = subprocess.run([sys.executable, "-m", "afl_cam.crop_maps",
                          str(tmp_path / "nope"), str(tmp_path / "c2")],
                         capture_output=True, text=True)
    assert bad.returncode == 1
