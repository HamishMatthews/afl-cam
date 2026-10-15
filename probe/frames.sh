#!/usr/bin/env bash
RIG=/home/hamish/Documents/afl-cam
OUT=$1/probe-out
mkdir -p $OUT/frames
for sess in 20261015_115828 20261015_120147 20261015_120742 20261015_115113; do
  d=$RIG/outputs/$sess
  # contact sheet: 1 fps proxy frames, 400 px wide, 4 columns
  ffmpeg -v error -i $d/proxy.mp4 -vf "fps=1,scale=400:-2,tile=4x8" -frames:v 1 -strict unofficial -q:v 4 $OUT/frames/${sess}_sheet.jpg -y
done
ls -la $OUT/frames
