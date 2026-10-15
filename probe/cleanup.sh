#!/usr/bin/env bash
# delete the 13:44 test recording the prod probe made (folder 20261015_134409)
RIG=/home/hamish/Documents/afl-cam
ids=$(curl -sf -m 5 http://127.0.0.1/api/recordings | python3 -c "
import json,sys
for r in json.load(sys.stdin)['recordings']:
    if str(r.get('created_at','')).startswith('2026-10-15 13:44'): print(r['id'], r['created_at'], r['duration_s'], file=sys.stderr); print(r['id'])")
echo "matches: $ids"
[ "$(echo $ids | wc -w)" = 1 ] && [ -d $RIG/outputs/20261015_134409 ] && curl -sf -m 10 -X DELETE http://127.0.0.1/api/recordings/$ids; echo
ls -d $RIG/outputs/20261015_1344* 2>&1
