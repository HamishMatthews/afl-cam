#!/usr/bin/env bash
# Read-only: did the deployed daemon recover the power-cut session?
S() { echo 123 | sudo -S -p '' "$@"; }
sleep 60   # let the finalize run
S journalctl -u afl-camd --since '-10 min' --no-pager | grep -av 'mpp' | grep -aiE 'intent|resum|finaliz|recover|retime|mux|concat|error' | tail -40
d=/home/hamish/Documents/afl-cam/outputs/20261014_190718; ls -la $d
python3 -c "
import sqlite3;c=sqlite3.connect('file:/home/hamish/Documents/afl-cam/recordings.db?mode=ro',uri=True)
print(c.execute('select id,status,duration_sec,file_size_bytes from recordings where id=123').fetchall())
print(c.execute('select id,stitch_status from sessions where id=91').fetchall())"
ls -la /home/hamish/Documents/afl-cam/outputs/_live/ | grep intent
