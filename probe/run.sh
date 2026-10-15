#!/usr/bin/env bash
# dispatch: probe/EXP names the experiment script under probe/
set -uo pipefail
TREE=$1
cd "$TREE"
mkdir -p probe-out
EXP=$(cat probe/EXP)
echo "[probe] experiment: $EXP"
bash "probe/$EXP.sh" "$TREE" 2>&1 | tee "probe-out/$EXP.log"
