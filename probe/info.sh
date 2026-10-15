#!/usr/bin/env bash
# read-only: system state, CPU hogs, today's sessions, daemon journal
RIG=/home/hamish/Documents/afl-cam
s() { echo; echo "===== $*"; }
s uname; uname -a; uptime; whoami; id
s git; git -C $RIG log --oneline -3
s status; curl -sf -m 3 http://127.0.0.1/api/status | head -c 3000; echo
s cpu-top-threads; top -b -n 2 -d 2 -H -w 200 | awk '/^top -/{n++} n==2' | head -45
s per-cpu; mpstat -P ALL 2 1 2>/dev/null || (for i in 1 2; do grep '^cpu' /proc/stat; sleep 2; done)
s ps-psr; ps -eLo pid,tid,psr,pcpu,stat,comm,args --sort=-pcpu | head -30
s irq; cat /proc/interrupts | grep -Ei 'xhci|usb|ehci|ohci|rkvenc|jpeg|mpp|gpu|CPU0' 
for i in $(grep -Ei 'xhci|usb' /proc/interrupts | awk -F: '{print $1}'); do echo "irq $i -> $(cat /proc/irq/$i/smp_affinity_list 2>/dev/null)"; done
s governors; for c in /sys/devices/system/cpu/cpufreq/policy*; do echo "$c $(cat $c/scaling_governor) $(cat $c/scaling_cur_freq)"; done
cat /sys/class/devfreq/*/governor 2>/dev/null; ls /sys/class/devfreq/
s temps; for z in /sys/class/thermal/thermal_zone*; do echo "$(cat $z/type) $(cat $z/temp)"; done
s mpp; ls -la /usr/lib/aarch64-linux-gnu/librockchip_mpp* /usr/local/lib/librockchip_mpp* 2>/dev/null
for f in /usr/lib/aarch64-linux-gnu/librockchip_mpp.so* /usr/local/lib/librockchip_mpp.so*; do [ -f "$f" ] && strings "$f" | grep -iE 'mpp_version|author|^[0-9a-f]{7,} ' | head -5; done
s dma_heap; ls -la /dev/dma_heap/ 2>&1; ls -la /dev/dri /dev/mpp_service /dev/rga 2>&1
s cam2stitch-procs; pgrep -af cam2 ; for p in $(pgrep -f native/cam2stitch); do cat /proc/$p/status | grep -E 'Threads|VmRSS|Cpus_allowed_list'; ls /proc/$p/task | while read t; do echo "$t $(cat /proc/$p/task/$t/comm) psr=$(awk '{print $39}' /proc/$p/task/$t/stat) utime=$(awk '{print $14}' /proc/$p/task/$t/stat)"; done; done
s sessions-today; ls -la --time-style=full-iso $RIG/outputs | tail -15
for d in $(ls -d $RIG/outputs/2026101[45]_* 2>/dev/null | tail -8); do echo "--- $d"; ls -la --time-style=full-iso $d; [ -f $d/segments.json ] && cat $d/segments.json; echo; done
s db; python3 - <<'PY'
import sqlite3
c=sqlite3.connect('file:/home/hamish/Documents/afl-cam/recordings.db?mode=ro',uri=True)
cur=c.execute("select * from recordings order by rowid desc limit 8")
cols=[d[0] for d in cur.description]; print(cols)
for r in cur: print(r)
PY
s journal; journalctl -u afl-camd --since "2026-10-15 00:00" --no-pager 2>&1 | grep -vE 'GET /api/(status|thumb)|"GET /' | tail -400
s dmesg; dmesg -T 2>&1 | tail -80 || sudo -n dmesg -T | tail -80
