"""Refine the live map set so the overlap stops ghosting.

Measures the residual misalignment between the two warped views on a frame
pair, fits a smooth correction field (afl_cam/seam_refine.py) and warps
both maps by half of it. Prints the stitch score before and after.

Usage:
    python3 scripts/refine_seam.py [--maps DIR] [--cam0 IMG --cam1 IMG] [--write]

Defaults: the live map set (calibration/stitch_live_full) and the re-align
stills saved in it (cam0.rgb/cam1.rgb). Without --write nothing changes.
With --write the refined maps replace the live ones (old maps kept as
map{0,1}.bin.prev) — only if the score improved. Best on stills shot at the
oval with players in the overlap: the fit aligns the depths it sees.
"""

import argparse
import json
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from afl_cam import seam_refine, stitch_maps  # noqa: E402
from afl_cam.stitch_score import load_still  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--maps", type=Path, default=REPO / "calibration" / "stitch_live_full")
    ap.add_argument("--cam0", type=Path)
    ap.add_argument("--cam1", type=Path)
    ap.add_argument("--write", action="store_true", help="replace the live maps if better")
    args = ap.parse_args()

    live = stitch_maps.load(args.maps)
    f0 = load_still(args.cam0 or args.maps / "cam0.rgb", live.src)
    f1 = load_still(args.cam1 or args.maps / "cam1.rgb", live.src)
    refined, before, after = seam_refine.refine(f0, f1, live)
    print("before:", json.dumps(before))
    print("after: ", json.dumps(after))

    if before["misalign_median"] is None or after["misalign_median"] is None:
        print("overlap too textureless to refine — maps unchanged")
        return 1
    if after["misalign_median"] >= before["misalign_median"]:
        print("no improvement — maps unchanged")
        return 1
    if not args.write:
        print("dry run — pass --write to apply")
        return 0
    for i in (0, 1):
        shutil.copy2(args.maps / f"map{i}.bin", args.maps / f"map{i}.bin.prev")
    stitch_maps.write(args.maps, refined)
    print(f"refined maps written to {args.maps} (previous kept as map*.bin.prev)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
