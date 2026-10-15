# which source frame does each stitched frame's Y / U / V follow? (cam0-only region)
import sys, glob, numpy as np, cv2
sys.path.insert(0, sys.argv[2])
from afl_cam import stitch_maps
d = sys.argv[1]
M = stitch_maps.load(sys.argv[3])
cw, ch = M.canvas; sw, sh = M.src
c0, c1 = M.cams
seqs = sorted(int(p[-7:-4]) for p in glob.glob(d + '/ssbo_*.bin'))
print('seqs', seqs)
# full-canvas source coords for cam0
mx = np.full((ch, cw), -1, np.float32); my = mx.copy()
mh, mw = c0.map.shape[:2]; ox, oy = c0.off
mx[oy:oy+mh, ox:ox+mw] = c0.map[..., 0]; my[oy:oy+mh, ox:ox+mw] = c0.map[..., 1]
only0 = (c0.weight > 250) & (c1.weight < 5) & (mx >= 0)
# chroma block grid: block (by,bx) sampled at canvas (2by+1, 2bx+1)
cmx = mx[1::2, 1::2]; cmy = my[1::2, 1::2]; conly = only0[1::2, 1::2]
cmx_uv = (cmx + 0.5) / 2 - 0.5; cmy_uv = (cmy + 0.5) / 2 - 0.5
def src(seq):
    b = np.fromfile(f'{d}/src0_{seq:03d}.bin', np.uint8)
    y = b[:sw*sh].reshape(sh, sw); uv = b[sw*sh:].reshape(sh//2, sw//2, 2)
    Y = cv2.remap(y, mx, my, cv2.INTER_LINEAR).astype(np.float32)
    U = cv2.remap(np.ascontiguousarray(uv[..., 0]), cmx_uv, cmy_uv, cv2.INTER_LINEAR).astype(np.float32)
    V = cv2.remap(np.ascontiguousarray(uv[..., 1]), cmx_uv, cmy_uv, cv2.INTER_LINEAR).astype(np.float32)
    return Y, U, V
def out(seq):
    b = np.fromfile(f'{d}/ssbo_{seq:03d}.bin', np.uint8)
    Y = b[:cw*ch].reshape(ch, cw).astype(np.float32)
    uvw = b[cw*ch:].reshape(ch//2, cw//2, 4)
    return Y, uvw[..., 0].astype(np.float32), uvw[..., 1].astype(np.float32)
S = {s: src(s) for s in seqs}; O = {s: out(s) for s in seqs}
def corr(a, b, m):
    a = a[m]; b = b[m]; a = a - a.mean(); b = b - b.mean()
    return float((a*b).sum() / np.sqrt((a*a).sum() * (b*b).sum() + 1e-9))
for s in seqs[2:]:
    for pi, name in enumerate('YUV'):
        m = only0 if pi == 0 else conly
        do = O[s][pi] - O[s-1][pi]
        r = {lag: corr(do, S[s-lag][pi] - S[s-lag-1][pi], m) for lag in (0, 1) if s-lag-1 in S}
        mad = {lag: float(np.abs(O[s][pi][m] - S[s-lag][pi][m]).mean()) for lag in (0, 1, 2) if s-lag in S}
        print(f'out {s} {name}: temporal-diff corr vs src lag0/lag1 {r}   abs diff vs src lag0/1/2 {mad}')
