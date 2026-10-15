#!/usr/bin/env bash
# exposure tracking: shock the controls after the start-up lock, watch it recover
TREE=$1; OUT=$TREE/probe-out; mkdir -p $OUT
RIG=/home/hamish/Documents/afl-cam
P=$TREE/probe/cpu
INC="-I/usr/local/include/rockchip"
gcc -O2 -Wall -Wextra -Werror $INC -o $P/ae $P/ae.c -L/usr/local/lib -lrockchip_mpp -lvulkan -lpthread -lm; echo "build rc=$?"
st=$(curl -sf -m 3 http://127.0.0.1/api/status | python3 -c 'import json,sys; print(json.load(sys.stdin).get("state",""))')
echo "daemon state: $st"; [ "$st" = recording ] && { echo "recording — abort"; exit 0; }
exec 9>/tmp/afl-cam-rig.lock; flock 9
sudo -n systemctl stop afl-camd
T=$TREE/aetmp; mkdir -p $T
trap 'pkill -f "$P/ae " ; sudo -n systemctl start afl-camd; rm -rf $T; echo restored' EXIT
sleep 2; pkill -f native/cam2stitch; sleep 1
export LD_LIBRARY_PATH=/opt/libmali-g610/usr/lib/aarch64-linux-gnu:/opt/libmali-g610/usr/lib/aarch64-linux-gnu/mali VK_DRIVER_FILES=/opt/libmali-g610/usr/share/vulkan/icd.d/mali.json
st() { python3 -c "
import socket,json
s=socket.socket(socket.AF_UNIX); s.connect('$T/ctl.sock'); s.sendall(b'STATUS\n'); s.settimeout(2)
d=b''
while not d.endswith(b'\n'): d+=s.recv(65536)
j=json.loads(d.decode().strip().splitlines()[-1])
print(' '.join('%s luma %5.1f exp %4.1fms b %2d g %3d |' % (c, j[c]['luma'], j[c]['exp_ms'], j[c]['bright'], j[c]['gamma']) for c in ('cam0','cam1')), 'out', j.get('out', j.get('paired')))
" 2>&1 | tail -1; }
for v in bright dark; do
  echo "===== shock $v"
  env AE_SHOCK_${v^^}=1 AFL_CAM_CTL_SOCK=$T/ctl.sock AFL_CAM_PREVIEW_UDP=127.0.0.1:9 \
    $P/ae /dev/video0 /dev/video2 $RIG/calibration/stitch_live_full $T/placeholder.raw.h264 0 3840 2160 30 0 0 $TREE/afl_cam/native/stitch_nv12.spv > /dev/null 2> $T/$v.err &
  pid=$!
  for i in $(seq 1 25); do sleep 2; echo "t=$((i*2))s $(st)"; done
  kill $pid; wait $pid
  grep -E "exposure|SHOCK|failed" $T/$v.err | head -60
  grep loop-ms $T/$v.err | tail -2
done
