# per-thread CPU % of a pid over N seconds + per-core busy %
import os, sys, time
pid, secs = int(sys.argv[1]), float(sys.argv[2])
hz = os.sysconf("SC_CLK_TCK")
def threads():
    out = {}
    for t in os.listdir(f"/proc/{pid}/task"):
        try:
            f = open(f"/proc/{pid}/task/{t}/stat").read().rsplit(")", 1)[1].split()
            out[int(t)] = (int(f[11]) + int(f[12]), int(f[36]))
        except OSError:
            pass
    return out
def cores():
    c = {}
    for l in open("/proc/stat"):
        if l.startswith("cpu") and l[3].isdigit():
            v = list(map(int, l.split()[1:]))
            c[l.split()[0]] = (sum(v) - v[3] - v[4], sum(v))
    return c
a, ca = threads(), cores(); time.sleep(secs); b, cb = threads(), cores()
tot = 0
rows = []
for t, (j, cpu) in b.items():
    d = (j - a.get(t, (j, 0))[0]) / hz / secs * 100
    tot += d
    rows.append((d, t, cpu))
for d, t, cpu in sorted(rows, reverse=True)[:7]:
    print(f"  tid {t} (+{t - pid}) {d:5.1f}% last-core {cpu}")
print(f"  process total {tot:.1f}%")
print("  cores " + " ".join(f"{k[3:]}:{(cb[k][0]-ca[k][0])*100/max(1,cb[k][1]-ca[k][1]):.0f}" for k in sorted(cb)))
