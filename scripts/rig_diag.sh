#!/usr/bin/env bash
# Read-only board diagnostics: why the last boot ended and the daemon's state.
S() { echo 123 | sudo -S -p '' "$@"; }
sec() { echo; echo "===== $* ====="; }
sec boots; S journalctl --list-boots --no-pager | tail -8
sec failed units; systemctl --failed --no-pager
sec afl-camd; systemctl status afl-camd afl-cam-ap mediamtx --no-pager -n 30
sec root fs; findmnt /; df -h / /home
sec net; ip -br addr; S iw dev wlan0 info
sec "previous boot: last 120 lines"; S journalctl -b -1 -n 120 --no-pager
sec "previous boot: afl-camd"; S journalctl -b -1 -u afl-camd --no-pager | tail -150
sec "previous boot: warnings"; S journalctl -b -1 -p warning --no-pager | grep -v afl-camd | tail -80
sec "boot -2: last 40 lines"; S journalctl -b -2 -n 40 --no-pager
sec "this boot: afl-camd"; S journalctl -b -u afl-camd --no-pager | tail -120
sec "this boot: afl-cam-ap"; S journalctl -b -u afl-cam-ap --no-pager | tail -40
sec "this boot: kernel ext4/nvme/usb"; S dmesg | grep -iE 'ext4|nvme|i/o error|under-?volt|brown|over-?current|watchdog' | tail -40
sec intent; cat /home/hamish/Documents/afl-cam/outputs/_live/intent.json; echo
ls -lt /home/hamish/Documents/afl-cam/outputs | head -5
sec latest session; d=$(ls -td /home/hamish/Documents/afl-cam/outputs/2* | head -1); ls -la "$d"; cat "$d"/segments.json 2>/dev/null
sec http; curl -s -m 5 -o /dev/null -w '%{http_code}\n' http://127.0.0.1/ ; curl -s -m 5 http://127.0.0.1/api/status | head -c 1500; echo
sec ports; S ss -ltnp | grep -E ':80 |:8080 |:8889 |:8888 '
