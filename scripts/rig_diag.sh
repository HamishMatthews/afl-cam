#!/usr/bin/env bash
# Read-only: why can't afl-camd read afl_cam/web/index.html?
S() { echo 123 | sudo -S -p '' "$@"; }
sec() { echo; echo "===== $* ====="; }
R=/home/hamish/Documents/afl-cam
sec uptime; uptime; date
sec boots; S journalctl --list-boots --no-pager | tail -6
sec web dir; ls -la $R/afl_cam/web; stat $R/afl_cam/web/index.html; head -c 200 $R/afl_cam/web/index.html | od -c | head -5
sec git; cd $R && git log -1 --oneline && git status --short | head -30; git fsck --no-dangling 2>&1 | head -20
sec mounts; findmnt /
sec "kernel ext4/nvme/io errors"; S dmesg | grep -iE 'ext4|nvme|i/o error|remount' | tail -30
sec "afl-camd this boot"; S journalctl -b -u afl-camd --no-pager | grep -av mpp | head -40
sec "afl-camd previous boot tail"; S journalctl -b -1 -u afl-camd --no-pager | grep -av mpp | tail -20
sec "deploys in the journal"; S journalctl --since '-14h' --no-pager | grep -a '\[deploy\]' | tail -10
sec http; curl -s -m 5 http://127.0.0.1/ | head -c 200; echo
