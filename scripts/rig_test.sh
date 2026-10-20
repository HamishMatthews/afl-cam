#!/usr/bin/env bash
# rig_test.sh — run the rig test suite against ANY checkout of afl-cam,
# ON the board, without touching the installed field daemon's code.
#
#   scripts/rig_test.sh <tree> [pytest args]
#
# Used by the self-hosted CI runner (.github/workflows/rig-test.yml) and by
# scripts/remote_rig_test.sh (a dev machine ships its worktree here over ssh).
#
# One test at a time owns the cameras: callers queue on a flock. While the
# lock is held the installed afl-camd is stopped, <tree>'s own daemon runs on
# a side port with its own ctl socket and outputs/, and afl-camd is restarted
# on exit (pass or fail). Defaults to the fast suite; RIG_SLOW=1 (or explicit
# pytest args) runs the record cycle too.
#
# Needs: the sudoers rule in deploy/rig-runner.md (systemctl stop/start
# afl-camd without a password) and uv.
set -euo pipefail
# a runner started outside a login shell (e.g. restarted by hand) may not
# have uv's install dirs on PATH
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:/usr/local/bin:$PATH"

TREE=$(realpath "${1:?usage: rig_test.sh <tree> [pytest args]}"); shift
RIG_HOME=${AFL_CAM_RIG_HOME:-/home/hamish/Documents/afl-cam}
PORT=${AFL_CAM_TEST_PORT:-8081}
SOCK=/tmp/afl-cam-test.sock
LOCK=/tmp/afl-cam-rig.lock

exec 9>"$LOCK"
if ! flock -n 9; then
    echo "[rig] rig busy (another test holds $LOCK) — queued…"
    flock 9
fi
echo "[rig] lock held — testing $TREE"

cd "$TREE"
# calibration/ is board-local (gitignored): share the rig's frozen alignment
[ -e calibration ] || ln -s "$RIG_HOME/calibration" calibration
# match the field venv: system python3 with system site-packages (gi comes
# from apt's python3-gi, not PyPI); uv would otherwise pick its own CPython
export UV_PYTHON=${UV_PYTHON:-/usr/bin/python3}
[ -d .venv ] || uv venv --system-site-packages --quiet
uv sync --quiet

DPID=""
restore() {
    if [ -n "$DPID" ] && kill -0 "$DPID" 2>/dev/null; then
        kill -TERM "$DPID"          # daemon QUITs cam2stitch on SIGTERM
        for _ in $(seq 30); do kill -0 "$DPID" 2>/dev/null || break; sleep 1; done
        kill -KILL "$DPID" 2>/dev/null || true
    fi
    pkill -f "$TREE/afl_cam/native/cam2" 2>/dev/null || true
    rm -f "$SOCK"
    sudo -n systemctl start afl-camd || echo "[rig] WARNING: afl-camd restart failed" >&2
    echo "[rig] afl-camd restored, lock released"
}
trap restore EXIT
trap 'exit 130' INT TERM

# never stop the field daemon mid-recording or mid-finalize: a finalize
# killed halfway leaves the session un-muxed (2026-10-14)
for _ in $(seq 180); do
    state=$( { curl -sf -m 3 http://127.0.0.1:80/api/status \
               || curl -sf -m 3 http://127.0.0.1:8080/api/status; } 2>/dev/null \
        | python3 -c 'import json,sys; print(json.load(sys.stdin).get("state",""))' 2>/dev/null || true)
    case "$state" in
        recording) echo "[rig] afl-camd is recording — not testing now" >&2
                   trap - EXIT; exit 1 ;;
        finalizing|realigning) echo "[rig] afl-camd is $state — waiting…"; sleep 10 ;;
        *) break ;;
    esac
done
sudo -n systemctl stop afl-camd
# DIAG: run the live pipeline on the frozen maps with the blend re-cut by
# this tree's seam_refine.place_seam (what the next re-align will write)
mkdir -p rig-artifacts/blend
cp calibration/stitch_live_full/* rig-artifacts/blend/ 2>/dev/null || true
.venv/bin/python - <<'PY'
import json
from pathlib import Path
import numpy as np
from afl_cam import seam_refine, stitch_maps, stitch_score
d = Path("rig-artifacts/blend")
live = stitch_maps.load(d)
f0, f1 = (stitch_score.load_still(d / f"cam{i}.rgb", live.src) for i in (0, 1))
print("[blend] before", json.dumps(stitch_score.score(f0, f1, live)))
import cv2
(v0, v1), (m0, m1) = stitch_score.warp_views(f0, f1, live)
y0b, y1b, x0b, x1b = stitch_score.overlap_box(m0, m1)
both = (m0 & m1)[y0b:y1b, x0b:x1b]
def runs(b):
    e = np.flatnonzero(np.diff(np.concatenate([[0], b.astype(int), [0]])))
    return [(int(a) + x0b, int(c) - 1 + x0b) for a, c in zip(e[::2], e[1::2])]
g0 = cv2.GaussianBlur(stitch_score._gray(v0[y0b:y1b, x0b:x1b]).astype(np.float32), (0, 0), 4)
g1 = cv2.GaussianBlur(stitch_score._gray(v1[y0b:y1b, x0b:x1b]).astype(np.float32), (0, 0), 4)
cost = cv2.GaussianBlur(np.abs(g0 - g1), (0, 0), 6)
cost = cost + seam_refine.edge_penalty(both, float(np.median(cost[both])))
path = seam_refine.seam_path(cost, both, 64)
for r in range(200, y1b - y0b, 200):
    print(f"[blend] canvas row {r + y0b}: both {runs(both[r])[:4]} m0 {runs(m0[r + y0b, x0b:x1b])[:3]} "
          f"m1 {runs(m1[r + y0b, x0b:x1b])[:3]} path x {int(path[r]) + x0b if path[r] >= 0 else -1} "
          f"cost min at {int(np.argmin(cost[r])) + x0b}")
new = seam_refine.place_seam(f0, f1, live)
print("[blend] after ", json.dumps(stitch_score.score(f0, f1, new)))
stitch_maps.write(d, new)
stitch_score.render_seam_crop(f0, f1, new, Path("rig-artifacts/stitch-score/seam_recut.jpg"))
xs = np.arange(2180, 3100, 24)
for r in range(100, live.canvas[1], 200):
    print(f"[blend] row {r:4d} old " + "".join(str(v // 26) for v in live.cams[0].weight[r, xs])
          + "  new " + "".join(str(v // 26) for v in new.cams[0].weight[r, xs]))
PY
export AFL_CAM_STITCH_DIR="$PWD/rig-artifacts/blend"
# a re-align from the tests writes to a scratch dir, never calibration/
mkdir -p rig-artifacts/realign
# and so does the derived cropped map set
# and cam2stitch dumps its seam readings for test_seam_field
AFL_CAM_CROP_DIR="$TREE/rig-artifacts/crop" \
AFL_CAM_SEAM_DUMP=1 \
AFL_CAM_PORT=$PORT AFL_CAM_CTL_SOCK=$SOCK AFL_CAM_ALIGN_OUT="$TREE/rig-artifacts/realign" \
    .venv/bin/python -m afl_cam.cli daemon > rig-daemon.log 2>&1 &
DPID=$!

for _ in $(seq 60); do
    curl -sf "http://127.0.0.1:$PORT/api/status" > /dev/null && break
    kill -0 "$DPID" 2>/dev/null || { echo "[rig] daemon died:" >&2; tail -30 rig-daemon.log >&2; exit 1; }
    sleep 1
done

if [ $# -eq 0 ] && [ -z "${RIG_SLOW:-}" ]; then
    set -- -m "not slow" -k seam_field
fi
# --durations=0 --durations-min=0 prints every test's time, slowest first, at the
# end of the log: the place to look when the suite is slowing development down
AFL_CAM_TEST_BASE="http://127.0.0.1:$PORT" uv run pytest tests/rig -v \
    --durations=0 --durations-min=0 "$@"
