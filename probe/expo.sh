#!/usr/bin/env bash
# read-only: camera controls now, and what the exposure loop did since the pipeline started
RIG=/home/hamish/Documents/afl-cam; L=$RIG/outputs/_live
s() { echo; echo "===== $*"; }
s pipeline; for p in $(pgrep -f native/cam2stitch); do echo "pid $p started $(ps -o lstart= -p $p)"; done; date
s exposure-log; grep -n "exposure\|REC_START\|REC_STOP" $L/stitch.log | tail -20
s status-luma; curl -sf -m 3 http://127.0.0.1/api/status | python3 -c '
import json,sys; s=json.load(sys.stdin)["stitch"]
for c in ("cam0","cam1"): print(c, {k:s[c][k] for k in ("luma","u","v","dt_ms","seq_lost")})'
for v in /dev/video0 /dev/video2; do s "ctrls $v"; v4l2-ctl -d $v --list-ctrls-menus 2>&1; done
