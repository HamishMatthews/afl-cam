#!/usr/bin/env bash
# 1) record 20 s through the live daemon (main build) and see what paces it
# 2) the same standalone with the encoder-sync build (s.c)
TREE=$1; OUT=$TREE/probe-out; mkdir -p $OUT
RIG=/home/hamish/Documents/afl-cam; L=$RIG/outputs/_live
P=$TREE/probe/cpu
INC="-I/usr/local/include/rockchip"
gcc -O2 -Wall -Wextra -Werror $INC -o $P/s $P/s.c -L/usr/local/lib -lrockchip_mpp -lvulkan -lpthread -lm; echo "build s rc=$?"
api() { curl -sf -m 5 -X POST -H 'Content-Type: application/json' -d '{}' http://127.0.0.1/api/$1; echo; }
st=$(curl -sf -m 3 http://127.0.0.1/api/status | python3 -c 'import json,sys; print(json.load(sys.stdin).get("state",""))')
echo "daemon state: $st"; [ "$st" = recording ] && { echo "recording — abort"; exit 0; }
exec 9>/tmp/afl-cam-rig.lock; flock 9
echo "===== daemon recording"
api record/start; sleep 4
rd=$(curl -sf -m 3 http://127.0.0.1/api/status | python3 -c 'import json,sys; print(json.load(sys.stdin)["stitch"]["rec_dir"])'); echo "rec_dir $rd"
pid=$(pgrep -f native/cam2stitch | head -1)
python3 $P/sample.py $pid 8
for i in 1 2 3; do ps -L -o tid=,psr=,pcpu= -p $pid | awk '$3>3{printf "%s:%s ", $1-'$pid', $2}'; echo; sleep 1; done
ps -eo pid,pcpu,comm --sort=-pcpu | head -8
sleep 5
api record/stop
sleep 3
grep loop-ms $L/stitch.log | tail -12 | head -8
python3 - $rd/stitch.pts <<'PY'
import sys
v=[int(l) for l in open(sys.argv[1]) if l.strip()]
d=sorted((b-a)/1000 for a,b in zip(v,v[1:]))
print(len(v), "frames %.1f fps; interval median %.1f p10 %.1f p90 %.1f" % ((len(v)-1)/((v[-1]-v[0])/1e6), d[len(d)//2], d[len(d)//10], d[9*len(d)//10]))
PY
for i in $(seq 1 30); do s=$(curl -sf -m 3 http://127.0.0.1/api/status | python3 -c 'import json,sys; print(json.load(sys.stdin).get("state",""))'); [ "$s" = standby ] && break; sleep 2; done; echo "state $s"
sid=$(curl -sf -m 5 http://127.0.0.1/api/recordings | python3 -c "
import json,sys; d=json.load(sys.stdin); rows=d.get('sessions', d) if isinstance(d, dict) else d
for r in rows:
    if str(r.get('folder','')).rstrip('/').endswith('$(basename $rd)'): print(r['id'])" | head -1)
echo "test session id $sid"; [ -n "$sid" ] && curl -sf -m 10 -X DELETE http://127.0.0.1/api/recordings/$sid; echo
echo "===== standalone sync build"
sudo -n systemctl stop afl-camd
T=$TREE/prodtmp; mkdir -p $T
trap 'pkill -f "$P/s " ; sudo -n systemctl start afl-camd; rm -rf $T; echo restored' EXIT
sleep 2; pkill -f native/cam2stitch; sleep 1
export LD_LIBRARY_PATH=/opt/libmali-g610/usr/lib/aarch64-linux-gnu:/opt/libmali-g610/usr/lib/aarch64-linux-gnu/mali VK_DRIVER_FILES=/opt/libmali-g610/usr/share/vulkan/icd.d/mali.json
ctl() { python3 -c "import socket,sys; s=socket.socket(socket.AF_UNIX); s.connect('$T/ctl.sock'); s.sendall((sys.argv[1]+'\n').encode()); s.settimeout(2); print('  ctl', sys.argv[1].split()[0], '->', s.recv(200).decode().strip()[:60])" "$1"; }
AFL_CAM_CTL_SOCK=$T/ctl.sock AFL_CAM_PREVIEW_UDP=127.0.0.1:9 AFL_CAM_PREVIEW_VIDEO_W=1600 AFL_CAM_PREVIEW_BITRATE_KBPS=3500 \
  $P/s /dev/video0 /dev/video2 $RIG/calibration/stitch_live_full $T/placeholder.raw.h264 0 3840 2160 30 0 0 $TREE/afl_cam/native/stitch_nv12.spv > /dev/null 2> $T/s.err &
spid=$!
sleep 15; grep loop-ms $T/s.err | tail -2
ctl "REC_START $T/rec"; sleep 20; ctl REC_STOP; sleep 2
ctl "REC_START $T/rec2"; sleep 3; ctl REC_STOP; sleep 3
ctl QUIT; sleep 4; kill $spid 2>/dev/null; wait $spid
a=$(grep -n "REC_START" $T/s.err | head -1 | cut -d: -f1); b=$(grep -n "REC_STOP" $T/s.err | head -1 | cut -d: -f1)
sed -n "${a},${b}p" $T/s.err | grep loop-ms | tail -4
echo "mpp error lines: $(grep -cE 'ref_count|invalid mem pool|put_meta' $T/s.err)"; grep -E "failed|bad packet" $T/s.err | head
for r in rec rec2; do
python3 - $T/$r/stitch.pts <<'PY'
import sys
v=[int(l) for l in open(sys.argv[1]) if l.strip()]
d=sorted((b-a)/1000 for a,b in zip(v,v[1:]))
print(len(v), "frames %.1f fps; interval median %.1f p10 %.1f p90 %.1f; dup pts %d" % ((len(v)-1)/((v[-1]-v[0])/1e6), d[len(d)//2], d[len(d)//10], d[9*len(d)//10], len(v)-len(set(v))))
PY
echo "$r master decode errors: $(ffmpeg -v error -f hevc -i $T/$r/stitch.raw.h264 -f null - 2>&1 | wc -l) frames $(ffprobe -v error -count_frames -f hevc -select_streams v -show_entries stream=nb_read_frames -of csv=p=0 $T/$r/stitch.raw.h264)"
echo "$r preview decode errors: $(ffmpeg -v error -f h264 -i $T/$r/preview.raw.h264 -f null - 2>&1 | wc -l) frames $(ffprobe -v error -count_frames -f h264 -select_streams v -show_entries stream=nb_read_frames -of csv=p=0 $T/$r/preview.raw.h264) pts $(wc -l < $T/$r/preview.pts)"
done
