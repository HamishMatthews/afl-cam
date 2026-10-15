#!/usr/bin/env bash
# recording fps with the main-branch cam2stitch: free scheduling vs big cores only
TREE=$1; OUT=$TREE/probe-out; mkdir -p $OUT
RIG=/home/hamish/Documents/afl-cam
P=$TREE/probe/cpu
INC="-I/usr/local/include/rockchip"
gcc -O2 -Wall -Wextra $INC -o $P/m $P/m.c -L/usr/local/lib -lrockchip_mpp -lvulkan -lpthread -lm; echo "build rc=$?"
st=$(curl -sf -m 3 http://127.0.0.1/api/status | python3 -c 'import json,sys; print(json.load(sys.stdin).get("state",""))')
echo "daemon state: $st"; [ "$st" = recording ] && { echo "recording — abort"; exit 0; }
exec 9>/tmp/afl-cam-rig.lock; flock 9
sudo -n systemctl stop afl-camd
T=$TREE/fpstmp; mkdir -p $T
trap 'pkill -f "$P/m " ; sudo -n systemctl start afl-camd; rm -rf $T; echo restored' EXIT
sleep 2; pkill -f native/cam2stitch; sleep 1
export LD_LIBRARY_PATH=/opt/libmali-g610/usr/lib/aarch64-linux-gnu:/opt/libmali-g610/usr/lib/aarch64-linux-gnu/mali VK_DRIVER_FILES=/opt/libmali-g610/usr/share/vulkan/icd.d/mali.json
ctl() { python3 -c "import socket,sys; s=socket.socket(socket.AF_UNIX); s.connect('$T/ctl.sock'); s.sendall((sys.argv[1]+'\n').encode()); s.settimeout(2); print('  ctl', sys.argv[1].split()[0], '->', s.recv(200).decode().strip()[:60])" "$1"; }
for v in free big free2 big2; do
  echo "===== $v"
  pre=""; case $v in big*) pre="taskset -c 4-7";; esac
  AFL_CAM_CTL_SOCK=$T/ctl.sock AFL_CAM_PREVIEW_UDP=127.0.0.1:9 AFL_CAM_PREVIEW_VIDEO_W=1600 AFL_CAM_PREVIEW_BITRATE_KBPS=3500 \
    $pre $P/m /dev/video0 /dev/video2 $RIG/calibration/stitch_live_full $T/placeholder.raw.h264 0 3840 2160 30 0 0 $TREE/afl_cam/native/stitch_nv12.spv > /dev/null 2> $T/$v.err &
  pid=$!
  sleep 15
  ctl "REC_START $T/rec_$v"; sleep 3
  for i in $(seq 1 6); do ps -L -o tid=,psr=,pcpu= -p $(pgrep -f "$P/m " | head -1) | awk '$3>3{printf "%s:%s ", $1, $2}'; echo; sleep 2; done | tail -3
  sleep 5
  ctl REC_STOP; sleep 3
  ctl QUIT; sleep 4; kill $pid 2>/dev/null; wait $pid
  a=$(grep -n "REC_START" $T/$v.err | cut -d: -f1); b=$(grep -n "REC_STOP" $T/$v.err | head -1 | cut -d: -f1)
  sed -n "${a},${b}p" $T/$v.err | grep loop-ms | tail -5
  grep "exposure" $T/$v.err | head -2
  python3 - $T/rec_$v/stitch.pts <<'PY'
import sys
v=[int(l) for l in open(sys.argv[1]) if l.strip()]
d=sorted((b-a)/1000 for a,b in zip(v,v[1:]))
print(len(v), "frames %.1f fps; interval median %.1f p10 %.1f p90 %.1f" % ((len(v)-1)/((v[-1]-v[0])/1e6), d[len(d)//2], d[len(d)//10], d[9*len(d)//10]))
PY
done
