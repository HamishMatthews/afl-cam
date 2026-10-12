#!/bin/bash
# afl-cam rig test suite — run ON the board (the tests drive the real
# daemon + cameras).
#
#   scripts/run_tests.sh              fast tests (no record cycle)
#   scripts/run_tests.sh -m slow      everything, including the record cycle
set -e
cd "$(dirname "$0")/.."
exec uv run pytest tests/rig -v "$@"
