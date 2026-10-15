#!/usr/bin/env bash
# raw UVC MJPEG straight from V4L2 (no MPP, no GPU, no encoder), software-decoded
TREE=$1; OUT=$TREE/probe-out; mkdir -p $OUT/raw
PY=/home/hamish/venvs/cv2-new/bin/python3
st=$(curl -sf -m 3 http://127.0.0.1/api/status | python3 -c 'import json,sys; print(json.load(sys.stdin).get("state",""))')
echo "daemon state: $st"; [ "$st" = recording ] && { echo "recording — abort"; exit 0; }
exec 9>/tmp/afl-cam-rig.lock; flock 9
sudo -n systemctl stop afl-camd
trap 'sudo -n systemctl start afl-camd; rm -rf $TREE/rawcap; echo restored' EXIT
sleep 2; pkill -f native/cam2stitch; sleep 1
for v in /dev/video0 /dev/video2; do echo "== $v"; v4l2-ctl -d $v --list-ctrls-menus; done
mkdir -p $TREE/rawcap && cd $TREE/rawcap
v4l2-ctl -d /dev/video0 --set-fmt-video=width=3840,height=2160,pixelformat=MJPG -p 30
echo "capture start $(date +%T)"
v4l2-ctl -d /dev/video0 --stream-mmap=4 --stream-count=900 --stream-to=cam0.mjpg 2>&1 | tail -1
echo "capture end $(date +%T)"
OUT=$OUT $PY - <<'PY'
import cv2, numpy as np, os
data=open('cam0.mjpg','rb').read(); frames=[]; i=0
while True:
    s=data.find(b'\xff\xd8',i)
    if s<0: break
    e=data.find(b'\xff\xd9',s)
    if e<0: break
    frames.append(data[s:e+2]); i=e+2
print('frames',len(frames))
Y=[];U=[];V=[]
for f in frames:
    im=cv2.imdecode(np.frombuffer(f,np.uint8),cv2.IMREAD_REDUCED_COLOR_4)
    yuv=cv2.cvtColor(im,cv2.COLOR_BGR2YUV).astype(np.int16)
    Y.append(yuv[...,0]);U.append(yuv[...,1]);V.append(yuv[...,2])
n=len(Y)
dy=np.array([0]+[np.abs(Y[k]-Y[k-1]).mean() for k in range(1,n)])
dc=np.array([0]+[(np.abs(U[k]-U[k-1])+np.abs(V[k]-V[k-1])).mean() for k in range(1,n)])
print('motion max %.2f mean %.2f'%(dy.max(),dy.mean()))
# per-pixel: where luma changed a lot in step k, how much of the chroma change happens in step k vs k+1..k+3
num=np.zeros(5); 
idx=np.argsort(dy)[-40:]
for k in sorted(idx):
    if k<2 or k+4>=n: continue
    m=np.abs(Y[k]-Y[k-1])>25
    if m.sum()<500: continue
    for j in range(5):
        a=k+j-1
        num[j]+=(np.abs(U[a+1]-U[a])+np.abs(V[a+1]-V[a]))[m].mean() if a+1<n else 0
print('chroma change at moving-luma pixels, steps -1..+3 rel. to the luma step:', np.round(num/len(idx),2))
best=int(np.argmax(np.convolve(dy,np.ones(3),'same')))
print('best motion at frame',best)
o=os.environ['OUT']
for k in range(max(1,best-3),min(n,best+4)):
    open(f'{o}/raw/raw_{k:04d}.jpg','wb').write(frames[k])
PY
rm -f cam0.mjpg; ls $OUT/raw
