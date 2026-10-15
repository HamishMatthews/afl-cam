#!/usr/bin/env bash
# read-only: which build recorded the latest session, and what paced it
RIG=/home/hamish/Documents/afl-cam
s() { echo; echo "===== $*"; }
s board-checkout; git -C $RIG log --oneline -3; git -C $RIG status -s | head
s binary; ls -la --time-style=full-iso $RIG/afl_cam/native/cam2stitch $RIG/afl_cam/native/cam2stitch.c
for p in $(pgrep -f native/cam2stitch); do echo "pid $p started $(ps -o lstart= -p $p) exe $(readlink /proc/$p/exe)"; done
systemctl show afl-camd -p ActiveEnterTimestamp
s sessions; ls -lt --time-style=+%H:%M:%S $RIG/outputs | head -6
d=$(ls -td $RIG/outputs/2026*/ | head -1); echo "latest: $d"; ls -la $d
for f in $d/stitch*.pts $d/preview*.pts; do [ -f $f ] && python3 - $f <<'PY'
import sys
v=[int(l) for l in open(sys.argv[1]) if l.strip()]
d=[(b-a)/1000 for a,b in zip(v,v[1:])]
d.sort()
print(sys.argv[1].split('/')[-1], len(v), "frames over %.1fs = %.1f fps; interval ms median %.1f p10 %.1f p90 %.1f max %.1f" % ((v[-1]-v[0])/1e6, (len(v)-1)/((v[-1]-v[0])/1e6), d[len(d)//2], d[len(d)//10], d[9*len(d)//10], d[-1]))
PY
done
ffprobe -v error -show_entries stream=codec_name,avg_frame_rate,nb_frames,duration -of compact $d/stitch.mp4
s journal-during-latest
journalctl -u afl-camd --since "-40 min" --no-pager -o short 2>&1 | grep -E 'loop-ms|REC_|exposure|fps|interval|lock|settle|spawn|cam2stitch' | grep -v "GET /api" | tail -60
s camera-controls
for v in $(ls /dev/video* ); do v4l2-ctl -d $v --get-ctrl=auto_exposure,exposure_time_absolute,exposure_dynamic_framerate,brightness,gamma 2>/dev/null | tr '\n' ' ' | sed "s|^|$v: |"; echo; done
s deploy; journalctl --since "-60 min" --no-pager -o short 2>&1 | grep -iE "deploy" | tail -10
