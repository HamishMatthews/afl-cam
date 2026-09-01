"""Refine the inter-camera rotation with K fixed, in CANVAS space.

The bundle on a grass field is degenerate (focal/R entangled — the last
session's bundle estimated focal 2125 vs measured 2237 and left a median
13-28px canvas misalignment between the warps; tile phase-correlation and
SIFT both confirm). With K fixed from the checkerboard calibration, only the
RELATIVE rotation between the cameras is unknown.

The cost is what actually shows in the stitched output: for each SIFT match,
the distance between the two cameras' projections of the point on the SHARED
CANVAS (spherical u/v × warp_scale — the canvas offsets cancel). RANSAC over
2-point rotation hypotheses → LM refinement on inliers. The fit lands on the
depth plane of the dominant match cluster (for the field: distant content);
residual spread for other depths is parallax, which no single rotation can
remove.

Usage:
    python3 scripts/refine_rotation.py <cam0.jpg> <cam1.jpg> \
        [--cams calibration/stitch_live_full/cams.txt] [--out refined_cams.txt]
"""

import sys
from pathlib import Path

import cv2
import numpy as np
from scipy.optimize import least_squares

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from afl_cam.stitcher import Stitcher  # noqa: E402

INLIER_PX = 2.0


def rot_perturb(wx, wy, wz):
    """Exponential-map rotation from a 3-vector."""
    th = np.hypot(wx, np.hypot(wy, wz))
    if th < 1e-12:
        return np.eye(3)
    K = np.array([[0, -wz, wy], [wz, 0, -wx], [-wy, wx, 0]])
    return np.eye(3) + np.sin(th) / th * K + (1 - np.cos(th)) / th ** 2 * (K @ K)


def parse_cams(path):
    rows = {}
    for line in open(path):
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            vals = [float(v) for v in parts[1:]]
        except ValueError:
            continue
        rows.setdefault(parts[0], []).append(vals)
    R = {i: np.array(rows[f"R{i}"], dtype=np.float64) for i in (0, 1)}
    K = {i: np.array(rows[f"K{i}"], dtype=np.float64) for i in (0, 1)}
    return K, R, rows["warp_scale"][0][0]


def two_point_rotation(a1, a2, b1, b2):
    """Rotation R with R@a1=b1, R@a2=b2 (b2 projected onto a2⊥a1 first).

    Returns None if the pair is degenerate (a1≈a2 or b1≈b2 — rays nearly
    parallel don't determine a rotation).
    """
    a1 = a1 / np.linalg.norm(a1)
    a2 = a2 - (a2 @ a1) * a1
    na2 = np.linalg.norm(a2)
    if na2 < 0.05:
        return None
    a2 = a2 / na2
    a3 = np.cross(a1, a2)
    b1 = b1 / np.linalg.norm(b1)
    b2 = b2 - (b2 @ b1) * b1
    nb2 = np.linalg.norm(b2)
    if nb2 < 0.05:
        return None
    b2 = b2 / nb2
    b3 = np.cross(b1, b2)
    A = np.stack([a1, a2, a3])
    B = np.stack([b1, b2, b3])
    return B.T @ A


def spherical_uv(R, Ki, pts):
    """World direction spherical coords (u, v) for image points pts (Nx2)."""
    d = (R @ (Ki @ np.c_[pts, np.ones(len(pts))].T)).T
    n = np.linalg.norm(d, axis=1)
    u = np.arctan2(d[:, 0], d[:, 2])
    v = np.pi - np.arccos(np.clip(d[:, 1] / n, -1, 1))
    return u, v


def canvas_residual(R1, K, R0, pts0, pts1, w_scale):
    """Canvas-space distance (px) between the two projections of each match.

    The canvas offset is shared (min of bounds), so it cancels from the
    difference: residual = w_scale * hypot(du, dv).
    """
    K0i, K1i = np.linalg.inv(K[0]), np.linalg.inv(K[1])
    u0, v0 = spherical_uv(R0, K0i, pts0)
    u1, v1 = spherical_uv(R1, K1i, pts1)
    return w_scale * np.hypot(u0 - u1, v0 - v1)


def match_features(stills, f0, f1, max_features=8000):
    """SIFT + cross-check + Lowe ratio (0.75). Returns (pts0, pts1) Nx2."""
    sift = cv2.SIFT_create(nfeatures=max_features)
    e0, e1 = stills._enhance(f0), stills._enhance(f1)
    kp0, des0 = sift.detectAndCompute(e0, None)
    kp1, des1 = sift.detectAndCompute(e1, None)
    bf = cv2.BFMatcher(cv2.NORM_L2)
    knn = bf.knnMatch(des0, des1, k=2)
    good = [m for m, n in knn if m.distance < 0.75 * n.distance]
    good = sorted(good, key=lambda m: m.distance)[:2000]
    pts0 = np.array([[kp0[m.queryIdx].pt[0], kp0[m.queryIdx].pt[1]] for m in good],
                    dtype=np.float64)
    pts1 = np.array([[kp1[m.trainIdx].pt[0], kp1[m.trainIdx].pt[1]] for m in good],
                    dtype=np.float64)
    return pts0, pts1


def main():
    argv = sys.argv[1:]
    cams_path = REPO / "calibration" / "stitch_live_full" / "cams.txt"
    out_path = None
    if "--cams" in argv:
        i = argv.index("--cams")
        cams_path = Path(argv[i + 1])
        del argv[i:i + 2]
    if "--out" in argv:
        i = argv.index("--out")
        out_path = Path(argv[i + 1])
        del argv[i:i + 2]
    if len(argv) < 2:
        raise SystemExit(__doc__.split("Usage:")[1].strip())

    f0 = cv2.imread(argv[0])
    f1 = cv2.imread(argv[1])
    if f0 is None or f1 is None:
        raise SystemExit("cannot read stills")
    # depth-band filter: restrict matches to image rows in [ymin, ymax]
    # (0.0 = top = horizon = distant; 1.0 = bottom = near ground). Players at
    # 20-80 m sit just under the horizon: y ~ 0.5-0.58 of a 2160-row image.
    ymin, ymax = 0.0, 1.0
    if "--band" in argv:
        i = argv.index("--band")
        lo, hi = argv[i + 1].split(":")
        ymin, ymax = float(lo), float(hi)
        del argv[i:i + 2]

    stills = Stitcher()
    K, R, w_scale = parse_cams(cams_path)
    R0, R1 = R[0].copy(), R[1].copy()
    K0i, K1i = np.linalg.inv(K[0]), np.linalg.inv(K[1])

    pts0, pts1 = match_features(stills, f0, f1)
    if (ymin, ymax) != (0.0, 1.0):
        keep0 = (pts0[:, 1] >= ymin * f0.shape[0]) & (pts0[:, 1] <= ymax * f0.shape[0])
        keep1 = (pts1[:, 1] >= ymin * f1.shape[0]) & (pts1[:, 1] <= ymax * f1.shape[0])
        pts0, pts1 = pts0[keep0 & keep1], pts1[keep0 & keep1]
    print(f"{len(pts0)} matches (SIFT, cross-check + Lowe 0.75, "
          f"band {ymin:.2f}-{ymax:.2f})")

    d0 = (R0 @ (K0i @ np.c_[pts0, np.ones(len(pts0))].T)).T
    d1 = (R1 @ (K1i @ np.c_[pts1, np.ones(len(pts1))].T)).T
    d0 /= np.linalg.norm(d0, axis=1, keepdims=True)
    d1 /= np.linalg.norm(d1, axis=1, keepdims=True)

    base = canvas_residual(R1, K, R0, pts0, pts1, w_scale)
    print(f"current alignment: canvas residual median {np.median(base):.1f}px, "
          f"<{INLIER_PX}px: {(base < INLIER_PX).sum()}/{len(pts0)}")

    rng = np.random.default_rng(42)
    n = len(pts0)
    best_inl, best_R1 = -1, None
    for _ in range(4000):
        i, j = rng.choice(n, size=2, replace=False)
        Rr = two_point_rotation(d0[i], d0[j], d1[i], d1[j])
        if Rr is None:
            continue
        # Rr maps d0->d1; the corrected R1 maps d1 back to d0: R1c = Rr^T @ R1
        R1c = Rr.T @ R1
        res = canvas_residual(R1c, K, R0, pts0, pts1, w_scale)
        ninl = int((res < INLIER_PX).sum())
        if ninl > best_inl:
            best_inl, best_R1 = ninl, R1c
    print(f"RANSAC: best hypothesis has {best_inl} inliers (<{INLIER_PX}px)")

    if best_inl < 8:
        raise SystemExit("no rotation explains the matches — capture better "
                         "stills (more distinctive content in the overlap)")

    inl_mask = canvas_residual(best_R1, K, R0, pts0, pts1, w_scale) < INLIER_PX
    pts0i, pts1i = pts0[inl_mask], pts1[inl_mask]
    print(f"refining on {len(pts0i)} inliers")

    def cost(w):
        Rp = rot_perturb(w[0], w[1], w[2])
        return canvas_residual(Rp @ best_R1, K, R0, pts0i, pts1i, w_scale)

    res = least_squares(cost, np.zeros(3), method="lm", max_nfev=50)
    R1_ref = rot_perturb(res.x[0], res.x[1], res.x[2]) @ best_R1
    if abs(np.linalg.det(R1_ref) - 1.0) > 1e-6:
        raise SystemExit("refined R is not a proper rotation — degenerate data")

    final = canvas_residual(R1_ref, K, R0, pts0i, pts1i, w_scale)
    all_res = canvas_residual(R1_ref, K, R0, pts0, pts1, w_scale)
    print(f"refined inlier residual: median {np.median(final):.3f}px "
          f"mean {np.mean(final):.3f}px p90 {np.percentile(final, 90):.3f}px")
    print(f"all matches: median {np.median(all_res):.1f}px, "
          f"{(all_res < INLIER_PX).sum()}/{n} within {INLIER_PX}px")
    if np.median(final) > 1.0:
        print("WARNING: inlier residual > 1px — the scene's depth spread "
              "exceeds what a single rotation can align (parallax)")

    print("refined R1:")
    for row in R1_ref:
        print("  " + " ".join(f"{v:.12g}" for v in row))

    if out_path:
        out_path = Path(out_path)
        out, wrote_r1 = [], False
        for line in open(cams_path):
            parts = line.split()
            if parts and parts[0] == "R1":
                if not wrote_r1:
                    out.append(f"R1 {R1_ref[0,0]:.12g} {R1_ref[0,1]:.12g} {R1_ref[0,2]:.12g}\n"
                               f"R1 {R1_ref[1,0]:.12g} {R1_ref[1,1]:.12g} {R1_ref[1,2]:.12g}\n"
                               f"R1 {R1_ref[2,0]:.12g} {R1_ref[2,1]:.12g} {R1_ref[2,2]:.12g}\n")
                    wrote_r1 = True
            else:
                out.append(line)
        with open(out_path, "w") as fh:
            fh.write("".join(out))
        print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
