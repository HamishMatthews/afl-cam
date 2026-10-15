#!/usr/bin/env bash
# baseline (main) vs fixed cam2stitch/cam2h264: MPP error lines + decode errors of the output
TREE=$1; OUT=$TREE/probe-out; mkdir -p $OUT
RIG=/home/hamish/Documents/afl-cam
N=$TREE/afl_cam/native; B=$TREE/probe/base
INC="-I/usr/local/include/rockchip"
gcc -O2 $INC -o $B/cam2stitch $B/cam2stitch.c -L/usr/local/lib -lrockchip_mpp -lvulkan -lpthread
gcc -O2 $INC -o $B/cam2h264 $B/cam2h264.c -L/usr/local/lib -lrockchip_mpp -lpthread
make -C $N cam2stitch cam2h264 WERROR=-Werror 2>&1 | grep -E "error|warning" ; echo "build rc=${PIPESTATUS[0]}"
st=$(curl -sf -m 3 http://127.0.0.1/api/status | python3 -c 'import json,sys; print(json.load(sys.stdin).get("state",""))')
echo "daemon state: $st"; [ "$st" = recording ] && { echo "recording — abort"; exit 0; }
exec 9>/tmp/afl-cam-rig.lock; flock 9
sudo -n systemctl stop afl-camd
T=$TREE/stabtmp; mkdir -p $T
trap 'pkill -f "$TREE/.*/cam2" ; sudo -n systemctl start afl-camd; rm -rf $T; echo restored' EXIT
sleep 2; pkill -f native/cam2stitch; sleep 1
export LD_LIBRARY_PATH=/opt/libmali-g610/usr/lib/aarch64-linux-gnu:/opt/libmali-g610/usr/lib/aarch64-linux-gnu/mali VK_DRIVER_FILES=/opt/libmali-g610/usr/share/vulkan/icd.d/mali.json
for v in base fixed; do
  bin=$B; [ $v = fixed ] && bin=$N
  echo "===== $v cam2stitch (45 s, preview on)"
  AFL_CAM_PREVIEW_UDP=127.0.0.1:9 timeout 70 $bin/cam2stitch /dev/video0 /dev/video2 $RIG/calibration/stitch_live_full $T/$v.raw.h264 0 3840 2160 30 0 45 $TREE/afl_cam/native/stitch_nv12.spv > /dev/null 2> $T/$v.err
  echo "rc=$?"
  echo "mpp error lines: $(grep -cE 'ref_count|invalid mem pool|put_meta|mismatch' $T/$v.err)"; grep -E 'ref_count|invalid mem pool|put_meta' $T/$v.err | sed -E 's/0x[0-9a-f]+/X/g' | sort | uniq -c | head -5
  grep -E "dropping bad packet|failed" $T/$v.err | sort | uniq -c | head
  grep "^submitted" $T/$v.err | tail -1
  grep "loop-ms" $T/$v.err | tail -3
  ls -la $T/$v.raw.h264
  echo "hevc decode errors: $(ffmpeg -v error -f hevc -i $T/$v.raw.h264 -f null - 2>&1 | wc -l)"; ffmpeg -v error -f hevc -i $T/$v.raw.h264 -f null - 2>&1 | sed -E 's/0x[0-9a-f]+/X/g; s/[0-9]+/N/g' | sort | uniq -c | head -6
  echo "frames: $(ffprobe -v error -count_frames -f hevc -select_streams v -show_entries stream=nb_read_frames -of csv=p=0 $T/$v.raw.h264)  pts lines: $(wc -l < $T/$v.pts)"
  echo "===== $v cam2h264 (10 s)"
  timeout 30 $bin/cam2h264 /dev/video0 $T/${v}_c0.raw.h264 300 > /dev/null 2> $T/${v}_c0.err; echo "rc=$?"
  echo "mpp error lines: $(grep -cE 'ref_count|invalid mem pool|put_meta' $T/${v}_c0.err)"
  f=$(ls $T/${v}_c0* | grep -v err | head -3); ls -la $T/${v}_c0*
  echo "decode errors: $(ffmpeg -v error -i $T/${v}_c0.raw.h264 -f null - 2>&1 | wc -l)"
done
