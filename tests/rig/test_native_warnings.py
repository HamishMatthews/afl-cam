"""Native code warnings (#5): compile cam2stitch and cam2h264 with
-Wall -Wextra on the board and report them. Fails only once the tree is
clean (WARN_BUDGET = 0), so new warnings are caught."""

import subprocess
from pathlib import Path

WARN_BUDGET = None      # None while the existing warnings are being fixed


def test_native_code_has_no_warnings():
    r = subprocess.run(["make", "-C", "afl_cam/native", "warnings"],
                       capture_output=True, text=True, timeout=300)
    out = r.stdout + r.stderr
    Path("rig-artifacts").mkdir(exist_ok=True)
    Path("rig-artifacts/native-warnings.txt").write_text(out)
    n = sum(1 for l in out.splitlines() if ": warning:" in l)
    errs = [l for l in out.splitlines() if ": error:" in l or "fatal error" in l]
    print(f"[native] {n} warnings, {len(errs)} errors\n{out[-6000:]}")
    assert not errs, errs[:5]
    if WARN_BUDGET is not None:
        assert n <= WARN_BUDGET, f"{n} warnings (budget {WARN_BUDGET})"
