#!/usr/bin/env bash
# read-only: live pipeline timing during the 13:27 recording + CPU now
RIG=/home/hamish/Documents/afl-cam; L=$RIG/outputs/_live
s() { echo; echo "===== $*"; }
s stitch-log; ls -la $L; wc -l $L/stitch.log
grep -n "REC_START\|REC_STOP" $L/stitch.log
a=$(grep -n "REC_START" $L/stitch.log | tail -1 | cut -d: -f1); b=$(grep -n "REC_STOP" $L/stitch.log | tail -1 | cut -d: -f1)
echo "rec lines $a..$b"
sed -n "${a},${b}p" $L/stitch.log | grep loop-ms | awk 'NR%4==1'
s standby-loop-now; grep loop-ms $L/stitch.log | tail -5
s non-loop-lines; grep -vE "loop-ms|^submitted" $L/stitch.log | sed -E 's/[0-9]+/N/g' | sort | uniq -c | sort -rn | head -30
s status; curl -sf -m 3 http://127.0.0.1/api/status | python3 -m json.tool | head -80
s top; top -b -H -n 2 -d 3 | awk '/^top/{n++} n==2' | head -30
s procs; ps -eo pid,pcpu,comm,args --sort=-pcpu | head -12
