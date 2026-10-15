#!/usr/bin/env bash
# raw UVC MJPEG straight from V4L2 (no MPP, no GPU, no encoder), software-decoded
TREE=$1; OUT=$TREE/probe-out; mkdir -p $OUT/raw
RIG=/home/hamish/Documents/afl-cam
PY=/home/hamish/venvs/cv2-new/bin/python3
st=$(curl -sf -m 3 http://127.0.0.1/api/status | python3 -c 'import json,sys; print(json.load(sys.stdin).get("state",""))')
echo "daemon state: $st"
[ "$st" = recording ] && { echo "recording — abort"; exit 0; }
exec 9>/tmp/afl-cam-rig.lock; flock 9
sudo -n systemctl stop afl-camd
trap 'pkill -f native/cam2 ; sudo -n systemctl start afl-camd; echo restored' EXIT
sleep 2; pkill -f native/cam2stitch; sleep 1
for v in /dev/video0 /dev/video2; do
  echo "== $v"; v4l2-ctl -d $v --info | head -8; v4l2-ctl -d $v --list-ctrls-menus; v4l2-ctl -d $v --list-formats-ext | head -30
done
cd $TREE/rawcap 2>/dev/null || mkdir -p $TREE/rawcap && cd $TREE/rawcap
v4l2-ctl -d /dev/video0 --set-fmt-video=width=3840,height=2160,pixelformat=MJPG -p 30
echo "capture start $(date +%T)"
v4l2-ctl -d /dev/video0 --stream-mmap=4 --stream-count=1200 --stream-to=cam0.mjpg 2>&1 | tail -2
echo "capture end $(date +%T)"; ls -la cam0.mjpg
OUT=$OUT $PY - <<'PY'
import cv2, numpy as np, os
data=open('cam0.mjpg','rb').read()
# split on SOI/EOI
frames=[]; i=0
while True:
    s=data.find(b'\xff\xd8',i)
    if s<0: break
    e=data.find(b'\xff\xd9',s)
    if e<0: break
    frames.append(data[s:e+2]); i=e+2
print('frames',len(frames))
prev=None; scores=[]
small=[]
for k,f in enumerate(frames):
    im=cv2.imdecode(np.frombuffer(f,np.uint8),cv2.IMREAD_REDUCED_COLOR_4)
    g=cv2.cvtColor(im,cv2.COLOR_BGR2GRAY).astype(np.float32)
    scores.append(0 if prev is None else float(np.abs(g-prev).mean())); prev=g
s=np.array(scores); print('motion max %.2f mean %.2f'%(s.max(),s.mean()))
best=int(np.argmax(np.convolve(s,np.ones(4),'same')))
print('best motion at frame',best)
out=os.environ.get('OUT','.')
for k in range(max(1,best-3),min(len(frames),best+4)):
    open(f'{out}/raw/raw_{k:04d}.jpg','wb').write(frames[k])   # untouched camera JPEG
    im=cv2.imdecode(np.frombuffer(frames[k],np.uint8),cv2.IMREAD_REDUCED_COLOR_2)
    cv2.imwrite(f'{out}/raw/half_{k:04d}.jpg',im,[cv2.IMWRITE_JPEG_QUALITY,90])
# JPEG header: sampling factors
f=frames[best]; p=f.find(b'\xff\xc0'); 
print('SOF0', f[p:p+19].hex())
PY
rm -f cam0.mjpg
ls -la $OUT/raw
