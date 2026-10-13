"""Framing options: projections invert, the crop rectangle is real, and the
comparison runs end to end on a synthetic rig (no calibration needed)."""

import numpy as np
import pytest

from afl_cam import framing_options as fo


def test_largest_rectangle_known_mask():
    m = np.zeros((6, 10), bool)
    m[1:5, 2:9] = True          # 4 x 7
    m[0, 0:3] = True            # decoys
    m[5, 4] = True
    assert fo.largest_rectangle(m) == (2, 1, 7, 4)
    assert fo.largest_rectangle(np.zeros((3, 3), bool))[2:] == (0, 0)


@pytest.mark.parametrize("make", [
    lambda b: fo.equirect(b),
    lambda b: fo.cylinder(b),
    lambda b: fo.panini(b, 0.5),
    lambda b: fo.panini(b, 1.0),
])
def test_projection_round_trip(make):
    rng = np.random.default_rng(1)
    rays = np.stack([rng.uniform(-1, 1, 200), rng.uniform(-0.5, 0.5, 200),
                     rng.uniform(0.6, 1, 200)], axis=1)
    rays /= np.linalg.norm(rays, axis=1, keepdims=True)
    proj = make(np.eye(3))
    x, y = proj.forward(rays)
    assert np.allclose(proj.rays(x, y), rays, atol=1e-9)


def test_rig_frame_is_orthonormal_and_down_matches_image_down():
    cams = fo.synthetic_rig(35, 15)
    b = fo.rig_frame(cams)
    assert np.allclose(b @ b.T, np.eye(3), atol=1e-9)
    assert b[1] @ cams[0].R[:, 1] > 0


def test_load_cams_reads_cams_txt(tmp_path):
    cams = fo.synthetic_rig()
    lines = []
    for i, c in enumerate(cams):
        for name, m in (("K", c.K), ("R", c.R)):
            lines += [f"{name}{i} " + " ".join(f"{v:.12g}" for v in row) for row in m]
        lines.append(f"cam{i} dist " + " ".join(str(v) for v in c.dist))
    p = tmp_path / "cams.txt"
    p.write_text("\n".join(lines) + "\n")
    got = fo.load_cams(p)
    assert np.allclose(got[1].R, cams[1].R) and np.allclose(got[0].K, cams[0].K)
    p.write_text("canvas_w 100\n")
    with pytest.raises(ValueError):
        fo.load_cams(p)


def test_evaluate_synthetic_rig(monkeypatch):
    monkeypatch.setattr(fo, "GRID_DIV", 12.0)
    rows, panels = fo.evaluate(fo.synthetic_rig(35, 15))
    by = {r.option: r for r in rows}
    assert len(rows) == 9
    for name, r in by.items():
        if "black margins" not in name and "stretch" not in name and not name.startswith("current"):
            assert r.black_pct == 0 and 40 < r.kept_pct < 100
        assert r.size[0] % 4 == 0 and r.size[1] % 2 == 0
    # a cropped rectangle really is covered everywhere
    for key in ("crop", "cylinder", "panini d=0.5", "panini d=1"):
        fp, (x, y, w, h) = panels[key]
        assert fp.union[y:y + h, x:x + w].all()
    assert "| option |" in fo.table(rows)
