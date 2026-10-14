#!/usr/bin/env bash
# Read-only board diagnostics, round 3.
sec() { echo; echo "===== $* ====="; }
L=/home/hamish/Documents/afl-cam/outputs/_live
sec intent.json; od -c $L/intent.json | head -20; echo; cat $L/intent.json; echo
sec intent.json.tmp; cat $L/intent.json.tmp; echo
sec "stitch.log (no mpp spam)"; grep -av 'mpp_mem_pool\|mpp_buffer_ref_dec\|put_meta' $L/stitch.log | cut -c1-220 | tail -250
sec preview-chain.log; tail -40 $L/preview-chain.log
sec "ramlog / journal mounts"; findmnt | grep -E 'log|zram'; ls -la /var/log/journal /var/log.hdd/journal 2>&1 | head; systemctl status armbian-ramlog --no-pager -n 5
