"""Native code warnings (#5): cam2stitch and cam2h264 compile with
-Wall -Wextra on the board without a single warning."""

import subprocess
from pathlib import Path


def test_native_code_has_no_warnings(capsys):
    r = subprocess.run(["make", "-C", "afl_cam/native", "warnings"],
                       capture_output=True, text=True, timeout=300)
    out = r.stdout + r.stderr
    Path("rig-artifacts").mkdir(exist_ok=True)
    Path("rig-artifacts/native-warnings.txt").write_text(out)
    lines = out.splitlines()
    n = sum(1 for l in lines if ": warning:" in l)
    errs = [l for l in lines if ": error:" in l or "fatal error" in l]
    with capsys.disabled():      # CI logs hide a passing test's prints
        print(f"\n[native] {n} warnings, {len(errs)} errors\n{out[-9000:]}")
    assert not errs, errs[:5]
    assert n == 0, f"{n} compiler warnings"
