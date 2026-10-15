#!/usr/bin/env bash
RIG=/home/hamish/Documents/afl-cam
OUT=$1/probe-out
s() { echo; echo "===== $*"; }
s journal-restarts
journalctl -u afl-camd --since "2026-10-15 11:40" --until "2026-10-15 12:12" --no-pager -o short-precise 2>&1 \
  | grep -vE 'mpp\[|mpp_buffer|mpp_mem_pool|submitted [0-9]+ frames|loop-ms|GET /api' | cut -c1-400 | head -400
s mpp-spam-per-10min
journalctl -u afl-camd --since "2026-10-15 10:00" --no-pager -o short 2>&1 | grep -E 'mpp_buffer|mpp_mem_pool' | awk '{print substr($3,1,4)}' | sort | uniq -c
s mpp-error-kinds
journalctl -u afl-camd --since "2026-10-15 10:00" --no-pager -o cat 2>&1 | grep -E 'mpp' | sed -E 's/0x[0-9a-f]+/X/g; s/[0-9]+/N/g' | sort | uniq -c | sort -rn | head -20
s loop-ms-samples
journalctl -u afl-camd --since "2026-10-15 11:58" --until "2026-10-15 11:59:30" --no-pager -o cat 2>&1 | grep -E 'loop-ms|stats|fps' | head -60
s frames
mkdir -p $OUT/frames
for sess in 20261015_115828 20261015_120147 20261015_115113; do
  d=$RIG/outputs/$sess
  ffprobe -v error -show_entries stream=codec_name,width,height,avg_frame_rate,r_frame_rate,nb_frames -of compact $d/stitch.mp4
  ffprobe -v error -show_entries stream=codec_name,width,height,avg_frame_rate,nb_frames -of compact $d/proxy.mp4
  for t in 5 10 15 20; do
    ffmpeg -v error -ss $t -i $d/stitch.mp4 -frames:v 1 -vf scale=1768:-2 -q:v 3 $OUT/frames/${sess}_m_$t.jpg -y
    ffmpeg -v error -ss $t -i $d/proxy.mp4 -frames:v 1 -q:v 3 $OUT/frames/${sess}_p_$t.jpg -y
  done
done
ls -la $OUT/frames
