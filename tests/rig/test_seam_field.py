"""The seam field makes the two cameras agree where they meet.

cam2stitch (AFL_CAM_SEAM_DUMP=1, set by rig_test.sh) dumps its overlap
readings, both cameras' gains and the seam field it hands the shader. From
them this test rebuilds what each camera puts on screen at every overlap
point, as the shader does, and compares the cameras on flat ground (points
flat in both cameras: a misaligned point there still reads the same
surface, so what is left is the brightness and colour step a viewer sees on
a road). It then reads a frame of the stitched preview and checks that the
shader really applies the field: where one camera renders, the picture
follows the field-corrected prediction, not the uncorrected one."""

import os
import subprocess
import time
from pathlib import Path

import numpy as np
import pytest

from afl_cam import stitch_maps
from afl_cam.config import STITCH_PARAMS_DIR

from .conftest import wake_standby

DUMP = Path("outputs/_live/seam_dump.bin")
CELL = 64
SEAM_BAND = 96          # px either side of the seam that count as "at the seam"


def _load():
    b = DUMP.read_bytes()
    n, gx, gy = (int(v) for v in np.frombuffer(b, np.int32, 3))
    g = np.frombuffer(b, np.float32, 2, 12).astype(float)
    o = 20
    xy = np.frombuffer(b, np.int16, 2 * n, o).reshape(n, 2).astype(int)
    o += 4 * n
    ch = []
    for _ in range(8):
        ch.append(np.frombuffer(b, np.uint8, n, o).astype(float))
        o += n
    field = np.frombuffer(b, np.float32, gx * gy * 8, o).reshape(gy, gx, 2, 4)[..., :3]
    return {"g": g, "xy": xy, "gx": gx, "gy": gy, "cams": (ch[0:3], ch[4:7]),
            "flat": (ch[3] > 0, ch[7] > 0), "field": field.astype(float)}


def _rgb(y, u, v):
    yf = (y - 16) / 219
    u, v = (u - 128) / 255, (v - 128) / 255
    return np.stack([yf + 1.402 * v, yf - 0.344136 * u - 0.714136 * v, yf + 1.772 * u], -1)


def _luma(rgb):
    return 16 + 219 * (rgb @ np.array([0.299, 0.587, 0.114]))


def _warmth(rgb):
    return 255 * (rgb[..., 0] - rgb[..., 2])


def _field_at(field, cam, xy):
    """the shader's bilinear read: nodes on a CELL grid of canvas pixels"""
    gx = xy[:, 0] / CELL
    gy = xy[:, 1] / CELL
    i, j = gx.astype(int), gy.astype(int)
    tx, ty = (gx - i)[:, None], (gy - j)[:, None]
    f = field[..., cam, :]
    top = f[j, i] * (1 - tx) + f[j, i + 1] * tx
    bot = f[j + 1, i] * (1 - tx) + f[j + 1, i + 1] * tx
    return top * (1 - ty) + bot * ty


def _screen(d, with_field):
    """per point, what each camera puts on screen (RGB 0..1)"""
    out = []
    for c in (0, 1):
        rgb = _rgb(*d["cams"][c]) * d["g"][c]
        if with_field:
            rgb = rgb * _field_at(d["field"], c, d["xy"])
        out.append(np.clip(rgb, 0, 1))
    return out


def _nodes(d):
    return (d["xy"][:, 1] + CELL // 2) // CELL * d["gx"] + (d["xy"][:, 0] + CELL // 2) // CELL


def _node_medians(v, node, sel, min_n=8):
    """median of v per node over the selected points; nodes with too few dropped"""
    out = {}
    for q in np.unique(node[sel]):
        m = sel & (node == q)
        if m.sum() >= min_n:
            out[int(q)] = float(np.median(v[m]))
    return out


def _med_abs(m):
    return float(np.median(np.abs(list(m.values())))) if m else float("nan")


def _live_for(d):
    """the live map set the pipeline runs (full canvas, or the crop)"""
    roots = [STITCH_PARAMS_DIR, Path(os.environ.get("AFL_CAM_CROP_DIR", "rig-artifacts/crop"))]
    for root in roots:
        for p in [root, *sorted(root.glob("*"))] if root.exists() else []:
            live = stitch_maps.try_load(p) if p.is_dir() else None
            if live and live.canvas[0] // CELL + 2 == d["gx"] and live.canvas[1] // CELL + 2 == d["gy"]:
                return live
    return None


def _preview_frame():
    """one decoded frame of the stitched preview: its Y plane as coded
    (limited range, the same levels as the predictions)"""
    url = "rtsp://127.0.0.1:8554/preview"
    probe = subprocess.run(["ffprobe", "-v", "error", "-rtsp_transport", "tcp", "-select_streams",
                            "v:0", "-show_entries", "stream=width,height", "-of", "csv=p=0", url],
                           capture_output=True, text=True, timeout=60, check=True)
    w, h = (int(v) for v in probe.stdout.strip().split(",")[:2])
    p = subprocess.run(["ffmpeg", "-v", "error", "-rtsp_transport", "tcp", "-i", url,
                        "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "yuv420p", "-"],
                       capture_output=True, timeout=60, check=True)
    return np.frombuffer(p.stdout[: w * h], np.uint8).reshape(h, w).astype(float)


def _shader_check(d, live, ok):
    """the preview follows the field where one camera renders"""
    frame = _preview_frame()
    w0 = live.cams[0].weight[d["xy"][:, 1], d["xy"][:, 0]]
    one = ok & ((w0 == 0) | (w0 == 255))
    px = np.clip((d["xy"][:, 0] * frame.shape[1] / live.canvas[0]).astype(int), 0, frame.shape[1] - 1)
    py = np.clip((d["xy"][:, 1] * frame.shape[0] / live.canvas[1]).astype(int), 0, frame.shape[0] - 1)
    node = _nodes(d)
    preds = {}
    for name, wf in (("plain", False), ("field", True)):
        a, b = _screen(d, wf)
        preds[name] = np.where(w0 == 0, _luma(b), _luma(a))
    qs = [q for q in np.unique(node[one]) if (one & (node == q)).sum() >= 12]

    def mean(v):
        return np.array([v[one & (node == q)].mean() for q in qs])

    shown, plain, fld = mean(frame[py, px]), mean(preds["plain"]), mean(preds["field"])
    # the preview's Y is in the predictions' levels: no scale fit (a field
    # close to one gain would fold into it). Where the field varies enough,
    # an offset is fitted too (exposure drift between the dump and the frame)
    corr = fld - plain
    seen = shown - plain
    big = np.abs(corr) > 0.8
    print(f"[seam] preview vs prediction r {np.corrcoef(plain, shown)[0, 1]:.3f}, "
          f"median offset {np.median(seen):+.2f}")
    if big.sum() < 10:
        print("[seam] shader check: the field is too flat here to see in the preview")
        return
    if np.std(corr[big]) < 0.5:
        # a near-uniform correction can't be told from the preview's own
        # level offset (compression, the box downscale, exposure drift)
        print(f"[seam] shader check: the field is too uniform here to see in the preview "
              f"(moves {big.sum()} blocks by {corr[big].mean():+.2f}, sd {np.std(corr[big]):.2f})")
        return
    slope = float(np.polyfit(corr[big], seen[big], 1)[0])
    print(f"[seam] shader check: {big.sum()} blocks the field moves >0.8 (sd "
          f"{np.std(corr[big]):.2f}), the preview follows {slope:.2f} of it")
    assert 0.5 < slope < 1.5, "the preview does not show the seam field"


def test_seam_field_matches_the_cameras(capsys):
    with capsys.disabled():
        wake_standby()
        time.sleep(30)                      # white balance + exposure + field settle
        t0 = DUMP.stat().st_mtime if DUMP.exists() else 0
        for _ in range(30):
            if DUMP.exists() and DUMP.stat().st_mtime != t0:
                break
            time.sleep(1)
        assert DUMP.exists(), "cam2stitch wrote no seam dump (AFL_CAM_SEAM_DUMP unset?)"
        d = _load()
        live = _live_for(d)
        fld = d["field"]
        y0, y1 = d["cams"][0][0], d["cams"][1][0]
        ok = (y0 >= 24) & (y1 >= 24) & (y0 <= 232) & (y1 <= 232)
        flat = ok & d["flat"][0] & d["flat"][1]
        print(f"\n[seam] gains {d['g'][0]:.3f} / {d['g'][1]:.3f}, field cam0 "
              f"{fld[..., 0, :].min():.3f}..{fld[..., 0, :].max():.3f}, overlap points "
              f"{len(y0)}, flat in both {flat.sum()}")
        if flat.sum() < 200:
            pytest.skip("too little flat, unclipped overlap to compare")

        node = _nodes(d)
        at_seam = np.zeros(len(y0), bool)
        if live is not None:
            # the seam: per overlap row, where cam0's weight drops below half
            w0 = live.cams[0].weight[d["xy"][:, 1], d["xy"][:, 0]]
            for r in np.unique(d["xy"][:, 1]):
                row = d["xy"][:, 1] == r
                xs = d["xy"][row & (w0 < 128), 0]
                if xs.size:
                    at_seam |= row & (np.abs(d["xy"][:, 0] - xs.min()) <= SEAM_BAND)
            print(f"[seam] blend: {np.mean((w0 > 25) & (w0 < 230)):.0%} of the overlap mid-blend")

        res = {}
        for name, wf in (("before", False), ("after", True)):
            a, b = _screen(d, wf)
            dy = _node_medians(_luma(b) - _luma(a), node, flat)
            dw = _node_medians(_warmth(b) - _warmth(a), node, flat)
            sy = _node_medians(_luma(b) - _luma(a), node, flat & at_seam)
            sw = _node_medians(_warmth(b) - _warmth(a), node, flat & at_seam)
            res[name] = (sy, sw) if len(sy) >= 5 else (dy, dw)
            print(f"[seam] {name:6s}: flat overlap ({len(dy)} blocks) |dY| {_med_abs(dy):.2f}, "
                  f"|d warmth| {_med_abs(dw):.2f}; at the seam ({len(sy)} blocks) "
                  f"|dY| {_med_abs(sy):.2f}, |d warmth| {_med_abs(sw):.2f}")

        if live is not None:
            _shader_check(d, live, ok)

        (by, bw), (ay, aw) = res["before"], res["after"]
        assert _med_abs(ay) < max(1.0, 0.5 * _med_abs(by)), "the seam field leaves a brightness step"
        assert _med_abs(aw) < max(1.5, 1.5 * _med_abs(bw)), "the seam field makes a colour step"
