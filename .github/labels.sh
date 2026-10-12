#!/usr/bin/env bash
# One-off: create the labels AGENTS.md relies on. Needs gh, authenticated.
#   bash .github/labels.sh
set -u
mk() { gh label create "$1" --color "$2" --description "$3" --force; }
mk ready      0e8a16 "Specified and free to pick up"
mk claimed    fbca04 "An agent or person is on it (see assignee + comment)"
mk blocked    b60205 "Waiting on something; say what in a comment"
mk needs-rig  5319e7 "Can only be verified on the board"
mk rig:slow   d4c5f9 "PR label: run the slow rig tests (record cycle)"
for a in native daemon web ui stitch deploy tests docs; do
    mk "area:$a" c5def5 "Area: $a"
done
