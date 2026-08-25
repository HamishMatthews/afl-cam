"""Unit test: streaming frame pairing == the offline pts pairing.

The live recorder (cam2stitch) pairs frames as they stream; the offline
stitcher (stitch_video_files) pairs whole recordings with searchsorted +
nearest-of-two-bracketing. The streaming port must reproduce the offline
pairing EXACTLY — it can, by delaying one cam1 frame (wait until the next
cam1 timestamp arrives, then choose the nearest of the two bracketing
frames). One frame of latency (~40 ms) is irrelevant for recording.

Verified on synthetic streams: start offsets, different rates (20 vs 30
fps), dropped frames, and early/late endings.
"""

import numpy as np


def offline_pairing(pts0, pts1):
    """Reference: the exact pairing stitcher.py stitch_video_files uses."""
    s0 = int(np.searchsorted(pts0, pts1[0]))
    e_cut = int(np.searchsorted(pts0, pts1[-1], side="right"))
    pair = np.searchsorted(pts1, pts0)
    pair = np.clip(pair, 0, len(pts1) - 1)
    lo = np.clip(pair - 1, 0, len(pts1) - 1)
    pair = np.where(np.abs(pts1[lo] - pts0) < np.abs(pts1[pair] - pts0), lo, pair)
    out = []
    for i in range(s0, e_cut):
        out.append((i, int(pair[i]), (pts0[i] + pts1[pair[i]]) // 2))
    return out


def streaming_pairing(pts0, pts1):
    """Streaming port for cam2stitch's main loop.

    cam0 drives; for each cam0 frame, wait until the cam1 frame whose
    timestamp EXCEEDS pts0[i] has arrived (lookahead of one), then pick the
    nearest of the two bracketing cam1 frames. Frames before pts1[0] (s0
    trim) and after pts1[-1] (e_cut trim) are dropped.
    """
    s0 = int(np.searchsorted(pts0, pts1[0]))
    e_cut = int(np.searchsorted(pts0, pts1[-1], side="right"))
    out = []
    j = 0          # cursor: cam1 frames up to j have been consumed/retained
    for i in range(s0, e_cut):
        # advance until the frame beyond pts0[i] is available
        while j < len(pts1) and pts1[j] <= pts0[i]:
            j += 1
        # now pts1[j-1] <= pts0[i] < pts1[j] (or j == len(pts1) at the end)
        if j == 0:
            jj = 0                      # pts0[i] < pts1[0] — use the first
        elif j == len(pts1):
            jj = j - 1                  # exact tie at the stream end
        else:
            # prefer the LOWER bracket only when STRICTLY closer — ties keep
            # the UPPER, matching the offline `|lo| < |pair|` comparison
            jj = j - 1 if (pts0[i] - pts1[j - 1]) < (pts1[j] - pts0[i]) else j
        out.append((i, jj, (pts0[i] + pts1[jj]) // 2))
    return out


def check(pts0, pts1, name):
    off = offline_pairing(pts0, pts1)
    stm = streaming_pairing(pts0, pts1)
    assert stm == off, (
        f"{name}: mismatch at first differing pair\n"
        f"  offline[{len(stm)}] = {off[len(stm)] if len(stm) < len(off) else 'END'}\n"
        f"  stream[{len(stm)}] = {stm[len(stm)-1] if stm else 'EMPTY'}")
    print(f"{name}: OK ({len(stm)} pairs)")


def main():
    rng = np.random.default_rng(7)
    T = 1_000_000

    # 1. clean 30/30 with 10ms start offset
    n = 300
    pts0 = np.arange(n) * T // 30 + 50_000
    pts1 = np.arange(n) * T // 30 + 60_000
    check(pts0, pts1, "30fps/30fps, 10ms offset")

    # 2. cam1 at 20 fps (auto-exposure), cam0 at 30
    pts0 = np.arange(300) * T // 30 + 1_000
    pts1 = np.arange(200) * 50_000 + 20_000
    check(pts0, pts1, "30fps cam0 vs 20fps cam1")

    # 3. dropped cam0 frames (gaps of 2-3 intervals)
    pts0 = np.cumsum(rng.integers(25_000, 90_000, 400)) + 10_000
    pts1 = np.cumsum(rng.integers(28_000, 42_000, 420)) + 40_000
    check(pts0, pts1, "random gaps both cameras")

    # 4. cam1 starts 3s late (big s0) and ends 2s early (e_cut)
    pts0 = np.arange(600) * T // 30 + 1_000
    pts1 = np.arange(450) * 40_000 + 3_001_000
    check(pts0, pts1, "cam1 3s late, 20-25fps")

    # 5. extreme: cam1 much slower (10 fps)
    pts0 = np.arange(400) * T // 30 + 5_000
    pts1 = np.arange(130) * 100_000 + 15_000
    check(pts0, pts1, "cam1 at 10 fps")

    # 6. equal timestamps edge (unlikely but must not crash)
    pts0 = np.arange(100) * 33_000
    pts1 = np.arange(100) * 33_000
    check(pts0, pts1, "identical streams")

    print("all pairing tests passed")


if __name__ == "__main__":
    main()
