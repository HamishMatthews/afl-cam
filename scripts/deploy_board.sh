#!/usr/bin/env bash
# deploy_board.sh — bring the installed field daemon up to a given tree's
# commit, ON the board.
#
#   scripts/deploy_board.sh <tree>
#
# Run by the self-hosted runner on every push to main
# (.github/workflows/deploy.yml): <tree> is the runner's checkout of main.
# Fast-forwards the installed checkout (AFL_CAM_RIG_HOME) to <tree>'s HEAD
# with a local fetch (no GitHub credentials needed on the board), syncs the
# venv and restarts afl-camd. cam2stitch/cam2h264 rebuild themselves on the
# next start when their sources changed.
#
# Refuses, leaving everything as it was, when:
#   - afl-camd is recording, finalizing or re-aligning (never restart mid-game;
#     re-run the workflow afterwards),
#   - the installed checkout has local edits or commits not on main.
# Shares the rig lock with rig_test.sh, so it never lands mid-test.
#
# Needs: the sudoers rule in deploy/rig-runner.md and uv.
set -euo pipefail

TREE=$(realpath "${1:?usage: deploy_board.sh <tree>}")
RIG_HOME=${AFL_CAM_RIG_HOME:-/home/hamish/Documents/afl-cam}
LOCK=/tmp/afl-cam-rig.lock

exec 9>"$LOCK"
if ! flock -n 9; then
    echo "[deploy] rig busy (a test holds $LOCK) — queued…"
    flock 9
fi

status() {
    for port in 80 8080; do
        curl -sf -m 3 "http://127.0.0.1:$port/api/status" && return 0
    done
    return 1
}

state=$(status | python3 -c 'import json,sys; print(json.load(sys.stdin).get("state",""))' 2>/dev/null || true)
case "$state" in
    recording|finalizing|realigning)
        echo "[deploy] afl-camd is $state — not restarting it now." >&2
        echo "[deploy] Re-run this workflow once it is idle." >&2
        exit 1 ;;
esac

cd "$RIG_HOME"
if [ "$(git rev-parse --abbrev-ref HEAD)" != main ]; then
    echo "[deploy] $RIG_HOME is not on main — not touching it." >&2
    exit 1
fi
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    echo "[deploy] $RIG_HOME has local edits — not touching it:" >&2
    git status --short --untracked-files=no >&2
    exit 1
fi
old=$(git rev-parse HEAD)
git fetch --quiet "$TREE" HEAD
new=$(git rev-parse FETCH_HEAD)
if [ "$old" = "$new" ]; then
    echo "[deploy] already at $(git log -1 --format='%h %s')"
    exit 0
fi
if ! git merge-base --is-ancestor "$old" "$new"; then
    echo "[deploy] $RIG_HOME has commits that are not on main — not touching it." >&2
    exit 1
fi
git merge --quiet --ff-only "$new"
echo "[deploy] $RIG_HOME: ${old:0:7} -> $(git log -1 --format='%h %s')"

if ! git diff --quiet "$old" "$new" -- deploy/systemd deploy/mediamtx.yml \
        deploy/avahi deploy/hostapd-afl-cam.conf scripts/install_field_services.sh; then
    echo "::warning::service files changed — run scripts/install_field_services.sh on the board to install them"
fi

export UV_PYTHON=${UV_PYTHON:-/usr/bin/python3}
uv sync --quiet

sudo -n systemctl stop afl-camd
sudo -n systemctl start afl-camd
for _ in $(seq 60); do
    if status > /dev/null; then
        echo "[deploy] afl-camd is up"
        exit 0
    fi
    sleep 1
done
echo "[deploy] afl-camd did not answer after the restart:" >&2
journalctl -u afl-camd -n 30 --no-pager >&2 || true
exit 1
