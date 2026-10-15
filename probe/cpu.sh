#!/usr/bin/env bash
# standby / recording / after-recording CPU: packet fix (a) vs + standby-cpu fix (b)
TREE=$1; OUT=$TREE/probe-out; mkdir -p $OUT
RIG=/home/hamish/Documents/afl-cam
P=$TREE/probe/cpu
INC="-I/usr/local/include/rockchip"
for v in a b; do gcc -O2 -Wall -Wextra -Werror $INC -o $P/$v $P/$v.c -L/usr/local/lib -lrockchip_mpp -lvulkan -lpthread -lm; echo "build $v rc=$?"; done
st=$(curl -sf -m 3 http://127.0.0.1/api/status | python3 -c 'import json,sys; print(json.load(sys.stdin).get("state",""))')
echo "daemon state: $st"; [ "$st" = recording ] && { echo "recording — abort"; exit 0; }
exec 9>/tmp/afl-cam-rig.lock; flock 9
sudo -n systemctl stop afl-camd
T=$TREE/cputmp; mkdir -p $T
trap 'pkill -f "$P/[ab] " ; sudo -n systemctl start afl-camd; rm -rf $T; echo restored' EXIT
sleep 2; pkill -f native/cam2stitch; sleep 1
export LD_LIBRARY_PATH=/opt/libmali-g610/usr/lib/aarch64-linux-gnu:/opt/libmali-g610/usr/lib/aarch64-linux-gnu/mali VK_DRIVER_FILES=/opt/libmali-g610/usr/share/vulkan/icd.d/mali.json
ctl() { python3 -c "import socket,sys; s=socket.socket(socket.AF_UNIX); s.connect('$T/ctl.sock'); s.sendall((sys.argv[1]+'\n').encode()); s.settimeout(2); print('  ctl', sys.argv[1].split()[0], '->', s.recv(200).decode().strip()[:60])" "$1"; }
loopms() { grep "loop-ms" $T/$v.err | tail -4 | head -3 | sed 's/^/  /'; }
for v in a b; do
  echo "===== $v"
  : > $T/$v.err
  AFL_CAM_CTL_SOCK=$T/ctl.sock AFL_CAM_PREVIEW_UDP=127.0.0.1:9 AFL_CAM_PREVIEW_VIDEO_W=1600 AFL_CAM_PREVIEW_BITRATE_KBPS=3500 \
    $P/$v /dev/video0 /dev/video2 $RIG/calibration/stitch_live_full $T/placeholder.raw.h264 0 3840 2160 30 0 0 $TREE/afl_cam/native/stitch_nv12.spv > /dev/null 2> $T/$v.err &
  pid=$!
  sleep 20
  echo "-- standby (fresh)"; python3 $P/sample.py $pid 15; loopms
  ctl "REC_START $T/rec_$v"; sleep 5
  echo "-- recording"; python3 $P/sample.py $pid 15; loopms
  ctl REC_STOP; sleep 5
  echo "-- standby (after a recording)"; python3 $P/sample.py $pid 15; loopms
  ctl QUIT; sleep 5; kill $pid 2>/dev/null; wait $pid
  echo "mpp error lines: $(grep -cE 'ref_count|invalid mem pool|put_meta|mismatch' $T/$v.err)"
  ls -la $T/rec_$v/
  echo "master decode errors: $(ffmpeg -v error -f hevc -i $T/rec_$v/stitch.raw.h264 -f null - 2>&1 | wc -l)  frames: $(ffprobe -v error -count_frames -f hevc -select_streams v -show_entries stream=nb_read_frames -of csv=p=0 $T/rec_$v/stitch.raw.h264)  pts: $(wc -l < $T/rec_$v/stitch.pts)"
  echo "preview decode errors: $(ffmpeg -v error -f h264 -i $T/rec_$v/preview.raw.h264 -f null - 2>&1 | wc -l)  frames: $(ffprobe -v error -count_frames -f h264 -select_streams v -show_entries stream=nb_read_frames -of csv=p=0 $T/rec_$v/preview.raw.h264)"
  ffmpeg -v error -f hevc -i $T/rec_$v/stitch.raw.h264 -vf "select=eq(n\,60)" -frames:v 1 -y $OUT/cpu_${v}_master.jpg
  ffmpeg -v error -f h264 -i $T/rec_$v/preview.raw.h264 -vf "select=eq(n\,60)" -frames:v 1 -y $OUT/cpu_${v}_preview.jpg
done
