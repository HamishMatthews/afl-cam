"""The re-align chain (bundle, rotation refine, final dump, seam refine) on a
STORED pair of field stills, written to a scratch dir. No cameras, no
daylight, no daemon, and calibration/ is never touched. The pair lives in
tests/data/realign/ (cam0.jpg, cam1.jpg: 3840x2160 stills of a distant
scene with detail in the overlap, taken facing the field); the test skips
until it is there."""

import json
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
STILLS = [REPO / "tests" / "data" / "realign" / f"cam{i}.jpg" for i in (0, 1)]
PY = Path("/home/hamish/venvs/cv2-new/bin/python3")
MAP_FILES = ("params.bin", "map0.bin", "map1.bin", "w0.bin", "w1.bin")
# ghosting after the fit, median px on the stored pair; the rig CI read 18 px
# on the live re-align stills, and a failed fit lands at 13-70 px, so this
# only catches a broken chain, not a slightly worse fit
MAX_MEDIAN_PX = 60


@pytest.mark.slow
def test_realign_chain_on_stored_stills(tmp_path):
    if not all(p.is_file() for p in STILLS):
        pytest.skip("no stored field stills in tests/data/realign")
    if not PY.exists():
        pytest.skip(f"no OpenCV 5 venv at {PY}")
    out = tmp_path / "maps"
    run = subprocess.run(
        [str(PY), str(REPO / "scripts" / "recapture_alignment.py"), str(out),
         "--canvas-width", "5304", "--stills", *map(str, STILLS)],
        capture_output=True, text=True, timeout=900, cwd=str(REPO))
    assert run.returncode == 0, (run.stdout + run.stderr)[-1500:]
    for name in MAP_FILES:
        assert (out / name).is_file() and (out / name).stat().st_size > 0, name
    score = subprocess.run(
        [str(PY), str(REPO / "scripts" / "stitch_score.py"),
         str(out / "cam0.rgb"), str(out / "cam1.rgb"), "--maps", str(out)],
        capture_output=True, text=True, timeout=300, cwd=str(REPO))
    assert score.returncode == 0, score.stderr[-500:]
    s = json.loads(score.stdout)
    print(f"[realign chain] {s}")
    assert s["misalign_median"] is not None and s["misalign_median"] < MAX_MEDIAN_PX, s
