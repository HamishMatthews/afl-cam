"""Frame pairing between the two free-running cameras.

Both recorders stamp every frame with the kernel-monotonic capture time
(the .pts sidecars). cam0 drives: each cam0 frame is paired with the
nearest cam1 frame, and the output is trimmed to the window where both
cameras are live. cam2stitch's main loop implements the same rule in
streaming form; tests/unit/test_pairing.py holds the two to each other.
"""

import numpy as np


def pair_frames(pts0: np.ndarray, pts1: np.ndarray) -> tuple[int, int, np.ndarray]:
    """Return (s0, e_cut, pair): output cam0 frames s0..e_cut-1, each paired
    with cam1 frame pair[i]. Ties go to the later cam1 frame."""
    s0 = int(np.searchsorted(pts0, pts1[0]))
    e_cut = int(np.searchsorted(pts0, pts1[-1], side="right"))
    pair = np.searchsorted(pts1, pts0)
    pair = np.clip(pair, 0, len(pts1) - 1)
    lo = np.clip(pair - 1, 0, len(pts1) - 1)
    pair = np.where(np.abs(pts1[lo] - pts0) < np.abs(pts1[pair] - pts0), lo, pair)
    return s0, e_cut, pair
