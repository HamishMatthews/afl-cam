#!/usr/bin/env bash
TREE=$1; OUT=$TREE/probe-out; mkdir -p $OUT
RIG=/home/hamish/Documents/afl-cam
make -C $TREE/afl_cam/native cam2stitch 2>&1 | tail -1
st=$(curl -sf -m 3 http://127.0.0.1/api/status | python3 -c 'import json,sys; print(json.load(sys.stdin).get("state",""))')
echo "daemon state: $st"; [ "$st" = recording ] && { echo "recording — abort"; exit 0; }
exec 9>/tmp/afl-cam-rig.lock; flock 9
sudo -n systemctl stop afl-camd
D=$TREE/dump; rm -rf $D; mkdir -p $D
trap 'pkill -f "$TREE/afl_cam/native/cam2" ; sudo -n systemctl start afl-camd; rm -rf $D; echo restored' EXIT
sleep 2; pkill -f native/cam2stitch; sleep 1
export LD_LIBRARY_PATH=/opt/libmali-g610/usr/lib/aarch64-linux-gnu:/opt/libmali-g610/usr/lib/aarch64-linux-gnu/mali VK_DRIVER_FILES=/opt/libmali-g610/usr/share/vulkan/icd.d/mali.json
cd $TREE
AFL_CAM_DUMP_DIR=$D AFL_CAM_DUMP_LO=200 timeout 30 afl_cam/native/cam2stitch /dev/video0 /dev/video2 $RIG/calibration/stitch_live_full /tmp/probe_lag.raw.h264 0 3840 2160 30 0 12 afl_cam/native/stitch_nv12.spv > /dev/null 2> $OUT/lag_stderr.txt
echo "rc=$?"; ls -la $D | head; rm -f /tmp/probe_lag.*
/home/hamish/venvs/cv2-new/bin/python3 probe/lag_analyze.py $D $TREE $RIG/calibration/stitch_live_full
