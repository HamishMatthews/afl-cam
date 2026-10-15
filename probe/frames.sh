#!/usr/bin/env bash
RIG=/home/hamish/Documents/afl-cam
OUT=$1/probe-out
mkdir -p $OUT/frames
d=$RIG/outputs/20261015_120147
ffprobe -v error -show_entries stream=width,height -of csv $d/stitch.mp4
# consecutive master frames around the stand-up (t 1.8-3.4 s), person crop, full res
ffmpeg -v error -ss 1.8 -i $d/stitch.mp4 -t 1.6 -vf "crop=1920:1464:1100:0,scale=960:-2" -strict unofficial -q:v 3 $OUT/frames/m_%02d.jpg -y
ffmpeg -v error -ss 1.8 -i $d/proxy.mp4 -t 1.6 -vf "crop=600:456:344:0,scale=960:-2" -strict unofficial -q:v 3 $OUT/frames/p_%02d.jpg -y
# separate camera previews from the session, if any
ls $d
ls -la $OUT/frames | head -80
