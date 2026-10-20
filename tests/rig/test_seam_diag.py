"""Temporary seam residual diagnostic (not for main)."""

import time
from pathlib import Path

import numpy as np

from .conftest import stitch, wake_standby

DUMP = Path("outputs/_live/ov_dump.bin")


def _load():
    b = DUMP.read_bytes()
    n = int(np.frombuffer(b, np.int32, 1)[0])
    g = np.frombuffer(b, np.float32, 2, 4)
    o = 12
    xy = np.frombuffer(b, np.int16, 2 * n, o).reshape(n, 2).astype(int); o += 4 * n
    w0 = np.frombuffer(b, np.uint8, n, o).astype(float) / 255; o += n
    ch = []
    for _ in range(6):
        ch.append(np.frombuffer(b, np.uint8, n, o).astype(float)); o += n
    y0, u0, v0, y1, u1, v1 = ch
    # what the shader outputs per camera: RGB x gain ~ Y and chroma offsets x gain
    y0, y1 = 16 + (y0 - 16) * g[0], 16 + (y1 - 16) * g[1]
    u0, v0 = 128 + (u0 - 128) * g[0], 128 + (v0 - 128) * g[0]
    u1, v1 = 128 + (u1 - 128) * g[1], 128 + (v1 - 128) * g[1]
    return g, xy, w0, (y0, u0, v0), (y1, u1, v1)


def _rows(tag, keys, sel_list, a, b):
    print(f"[seam] {tag}")
    for k, sel in zip(keys, sel_list):
        if sel.sum() < 30:
            continue
        d = [np.median(b[i][sel] - a[i][sel]) for i in range(3)]
        print(f"[seam]   {k:<16} n={int(sel.sum()):5d}  cam0 Y {np.median(a[0][sel]):6.1f}  "
              f"dY {d[0]:+5.1f}  dU {d[1]:+5.1f}  dV {d[2]:+5.1f}")


def test_seam_diag(capsys):
    with capsys.disabled():
        wake_standby()
        time.sleep(25)
        acc = []
        for _ in range(3):
            t = DUMP.stat().st_mtime if DUMP.exists() else 0
            for _ in range(25):
                time.sleep(1)
                if DUMP.exists() and DUMP.stat().st_mtime != t:
                    break
            acc.append(_load())
        s = stitch()
        print("\n[seam] status", {c: {k: (s.get(c) or {}).get(k) for k in ("u", "v", "gain", "wb_k", "exp_ms", "gamma")} for c in ("cam0", "cam1")})
        g, xy, w0, a, b = acc[-1]
        # stack the three dumps
        xy = np.concatenate([q[1] for q in acc]); w0 = np.concatenate([q[2] for q in acc])
        a = [np.concatenate([q[3][i] for q in acc]) for i in range(3)]
        b = [np.concatenate([q[4][i] for q in acc]) for i in range(3)]
        print("[seam] gains", [list(map(float, q[0])) for q in acc])
        ok = (a[0] > 30) & (b[0] > 30) & (a[0] < 225) & (b[0] < 225)
        xs, ys = xy[:, 0], xy[:, 1]
        print(f"[seam] overlap x {xs.min()}..{xs.max()}  y {ys.min()}..{ys.max()}  points {len(xs)}")
        _rows("all overlap", ["all"], [ok], a, b)
        seam = ok & (w0 > 0.2) & (w0 < 0.8)
        _rows("at the seam (w0 0.2-0.8)", ["seam"], [seam], a, b)
        # seam x position per row band
        yb = np.linspace(ys.min(), ys.max() + 1, 7)
        keys, sels = [], []
        for i in range(6):
            sel = seam & (ys >= yb[i]) & (ys < yb[i + 1])
            if sel.sum():
                keys.append(f"y {int(yb[i])}-{int(yb[i+1])} x~{int(np.median(xs[sel]))}")
            else:
                keys.append(f"y {int(yb[i])}-{int(yb[i+1])}")
            sels.append(sel)
        _rows("seam by row band", keys, sels, a, b)
        xb = np.linspace(xs.min(), xs.max() + 1, 9)
        _rows("overlap by column (cam0 edge -> cam1 edge)",
              [f"x {int(xb[i])}-{int(xb[i+1])}" for i in range(8)],
              [ok & (xs >= xb[i]) & (xs < xb[i + 1]) for i in range(8)], a, b)
        lb = [30, 60, 90, 120, 150, 180, 225]
        _rows("seam by brightness (tone curve)",
              [f"Y {lb[i]}-{lb[i+1]}" for i in range(6)],
              [ok & (w0 > 0.05) & (w0 < 0.95) & (a[0] >= lb[i]) & (a[0] < lb[i + 1]) for i in range(6)], a, b)
