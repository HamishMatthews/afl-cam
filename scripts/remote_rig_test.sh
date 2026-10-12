#!/usr/bin/env bash
# remote_rig_test.sh — run the rig tests on the board from a dev machine.
#
#   scripts/remote_rig_test.sh [pytest args]       (from any worktree)
#
# Ships this worktree's files (tracked + untracked, minus .gitignore'd —
# so uncommitted edits are tested) to ~/afl-cam-test/<worktree> on the board
# and runs scripts/rig_test.sh there. Parallel sessions are safe: each
# worktree gets its own dir and the board queues them on the rig lock.
# AFL_CAM_PI overrides the ssh host (default: the board's tailscale name).
set -euo pipefail

HOST=${AFL_CAM_PI:-orangepi5-max}
cd "$(git rev-parse --show-toplevel)"
NAME=$(basename "$PWD")-$(git rev-parse --abbrev-ref HEAD | tr '/' '-')
DEST="afl-cam-test/$NAME"

echo "[remote] shipping $PWD -> $HOST:~/$DEST"
git ls-files -z -co --exclude-standard \
    | while IFS= read -r -d '' f; do [ -e "$f" ] && printf '%s\0' "$f"; done \
    | tar --null -T - -cf - \
    | ssh "$HOST" "rm -rf ~/$DEST && mkdir -p ~/$DEST && tar -xf - -C ~/$DEST"

ssh -t "$HOST" "cd ~/$DEST && RIG_SLOW='${RIG_SLOW:-}' bash scripts/rig_test.sh . $(printf '%q ' "$@")"
