"""Copy the live map set to a scratch dir with the blend re-cut by this
tree's seam_refine.place_seam, the blend the next Re-align writes. The rig
tests run the pipeline on it (AFL_CAM_STITCH_DIR), so what they judge is the
shipped blend, not whatever an older Re-align left in calibration/.

    rig_recut_blend.py <scratch dir>
"""

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from afl_cam import seam_refine, stitch_maps, stitch_score
from afl_cam.config import STITCH_PARAMS_DIR


def main(out: Path) -> int:
    out.mkdir(parents=True, exist_ok=True)
    for f in STITCH_PARAMS_DIR.glob("*"):
        if f.is_file():
            shutil.copy2(f, out / f.name)
    live = stitch_maps.try_load(out)
    stills = [out / f"cam{i}.rgb" for i in (0, 1)]
    if live is None or not all(p.exists() for p in stills):
        print("[recut] no map set or stills to re-cut; the live blend stands")
        return 0
    f0, f1 = (stitch_score.load_still(p, live.src) for p in stills)
    new = seam_refine.place_seam(f0, f1, live)
    stitch_maps.write(out, new)
    for name, maps in (("live", live), ("recut", new)):
        s = stitch_score.score(f0, f1, maps)
        print(f"[recut] {name} seam_ghost {s['seam_ghost']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1])))
