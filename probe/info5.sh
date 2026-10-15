#!/usr/bin/env bash
# read-only: which cores the live cam2stitch threads run on
s() { echo; echo "===== $*"; }
s capacity; for c in /sys/devices/system/cpu/cpu[0-7]; do echo "$(basename $c) cap $(cat $c/cpu_capacity) cur $(cat $c/cpufreq/scaling_cur_freq) gov $(cat $c/cpufreq/scaling_governor)"; done
cat /proc/sys/kernel/sched_energy_aware 2>/dev/null
p=$(pgrep -f native/cam2stitch | head -1); echo "pid $p"
s per-thread-core-samples
for i in $(seq 1 20); do ps -L -o tid=,psr=,pcpu=,comm= -p $p | awk '$3>3{printf "%s:%s(%s) ", $1-'$p', $2, $3}'; echo; sleep 0.5; done
