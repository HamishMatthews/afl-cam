"""afl_cam.pairing (offline stitch) == the streaming pairing cam2stitch's
main loop runs. The streaming model below mirrors cam2stitch.c: cam0
drives, cam1 is read one frame ahead, the nearer of the two bracketing
cam1 frames wins, ties keep the later one."""

import numpy as np
import pytest

from afl_cam.pairing import pair_frames

T = 1_000_000


def offline(pts0, pts1):
    s0, e_cut, pair = pair_frames(pts0, pts1)
    return [(i, int(pair[i]), (pts0[i] + pts1[pair[i]]) // 2)
            for i in range(s0, e_cut)]


def streaming(pts0, pts1):
    s0 = int(np.searchsorted(pts0, pts1[0]))
    e_cut = int(np.searchsorted(pts0, pts1[-1], side="right"))
    out = []
    j = 0
    for i in range(s0, e_cut):
        while j < len(pts1) and pts1[j] <= pts0[i]:
            j += 1
        if j == 0:
            jj = 0
        elif j == len(pts1):
            jj = j - 1
        else:
            jj = j - 1 if (pts0[i] - pts1[j - 1]) < (pts1[j] - pts0[i]) else j
        out.append((i, jj, (pts0[i] + pts1[jj]) // 2))
    return out


rng = np.random.default_rng(7)
CASES = {
    "30/30 fps, 10 ms offset": (np.arange(300) * T // 30 + 50_000,
                                np.arange(300) * T // 30 + 60_000),
    "30 vs 20 fps": (np.arange(300) * T // 30 + 1_000,
                     np.arange(200) * 50_000 + 20_000),
    "random gaps": (np.cumsum(rng.integers(25_000, 90_000, 400)) + 10_000,
                    np.cumsum(rng.integers(28_000, 42_000, 420)) + 40_000),
    "cam1 3 s late, ends early": (np.arange(600) * T // 30 + 1_000,
                                  np.arange(450) * 40_000 + 3_001_000),
    "cam1 at 10 fps": (np.arange(400) * T // 30 + 5_000,
                       np.arange(130) * 100_000 + 15_000),
    "identical streams": (np.arange(100) * 33_000, np.arange(100) * 33_000),
}


@pytest.mark.parametrize("name", CASES)
def test_streaming_matches_offline(name):
    pts0, pts1 = CASES[name]
    assert streaming(pts0, pts1) == offline(pts0, pts1)


def test_trims_to_overlap():
    pts0 = np.arange(600) * T // 30 + 1_000
    pts1 = np.arange(450) * 40_000 + 3_001_000
    s0, e_cut, _ = pair_frames(pts0, pts1)
    assert pts0[s0] >= pts1[0]
    assert pts0[e_cut - 1] <= pts1[-1]
