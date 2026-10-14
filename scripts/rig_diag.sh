#!/usr/bin/env bash
# Read-only board diagnostics, round 2.
S() { echo 123 | sudo -S -p '' "$@"; }
sec() { echo; echo "===== $* ====="; }
F() { grep -av 'mpp_mem_pool\|mpp_buffer_ref_dec\|put_meta\|mpp\[' ; }
sec uptime; uptime; date; S chronyc tracking | head -4; systemctl status fake-hwclock-load --no-pager -n 10; ls -l /etc/fake-hwclock.data; cat /etc/fake-hwclock.data
sec journald conf; grep -v '^#' /etc/systemd/journald.conf | grep .; ls /etc/systemd/journald.conf.d/ 2>/dev/null && cat /etc/systemd/journald.conf.d/* 2>/dev/null
sec "boot lengths (monotonic of last entry)"; for b in -5 -4 -3 -2 -1 0; do echo "boot $b: $(S journalctl -b $b -o short-monotonic -n 1 --no-pager | tail -1 | cut -c1-140)"; done
sec "last -x"; last -x -F | head -20
sec "afl-camd all of today, no mpp spam"; S journalctl -u afl-camd -u afl-cam-ap -u mediamtx --since '2026-10-14 17:00' -o short-monotonic --no-pager | F | grep -v 'mediamtx.*INF \[RTSP\]\|mediamtx.*INF \[path\|mediamtx.*INF \[HLS\]\|WebRTC' | tail -400
sec "boot 0 kernel + NM + hostapd + power"; S journalctl -b 0 -o short-monotonic --no-pager | grep -aiE 'hostapd|dnsmasq|NetworkManager.*(wlan0|Hotspot|afl-cam)|field_wifi|voltage|over-?current|thermal|watchdog|oom|segfault|Killed process' | tail -80
sec "boot -1 everything after 19:00:03"; S journalctl -b -1 -o short-monotonic --no-pager | tail -25 | cut -c1-200
sec NM connections; nmcli -t -f NAME,TYPE,AUTOCONNECT,DEVICE con show
sec session; d=/home/hamish/Documents/afl-cam/outputs/20261014_190718; head -3 $d/stitch.pts; tail -3 $d/stitch.pts; wc -l $d/stitch.pts $d/stitch.000.pts; cat $d/stitch.000.pts | head; ls -la /home/hamish/Documents/afl-cam/outputs/_live
sec db; python3 -c "
import sqlite3;c=sqlite3.connect('file:/home/hamish/Documents/afl-cam/recordings.db?mode=ro',uri=True)
for t in c.execute(\"select name from sqlite_master where type='table'\"): print(t)
for r in c.execute('select * from sessions order by rowid desc limit 3'): print(r)
for r in c.execute('select * from recordings order by rowid desc limit 3'): print(r)"
