"""Score a stitch: overlap misalignment (ghosting) and exposure step.

Warps one camera-pair frame with a live map set (the exact maps + weights
cam2stitch uses) and prints the score as JSON; optionally saves a seam crop
(blended overlap above a red=cam0 / green=cam1 edge overlay).

Usage:
    python3 scripts/stitch_score.py <cam0> <cam1> [--maps DIR] [--crop out.jpg]

<cam0>/<cam1> are images (jpg/png) or the raw `cam0.rgb`/`cam1.rgb` stills
the re-align leaves in the map dir. --maps defaults to
calibration/stitch_live_full. Lower misalign_* is better; luma_ratio near 1.
"""

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from afl_cam import stitch_maps, stitch_score  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("cam0", type=Path)
    ap.add_argument("cam1", type=Path)
    ap.add_argument("--maps", type=Path, default=REPO / "calibration" / "stitch_live_full")
    ap.add_argument("--crop", type=Path, help="save a seam crop image here")
    args = ap.parse_args()

    live = stitch_maps.load(args.maps)
    f0, f1 = (stitch_score.load_still(p, live.src) for p in (args.cam0, args.cam1))
    if (f0.shape[1], f0.shape[0]) != live.src:
        raise SystemExit(f"frames are {f0.shape[1]}x{f0.shape[0]}, maps expect "
                         f"{live.src[0]}x{live.src[1]}")
    print(json.dumps(stitch_score.score(f0, f1, live), indent=2))
    if args.crop:
        stitch_score.render_seam_crop(f0, f1, live, args.crop)
        print(f"seam crop → {args.crop}", file=sys.stderr)


if __name__ == "__main__":
    main()
