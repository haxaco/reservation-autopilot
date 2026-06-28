#!/usr/bin/env bash
set -euo pipefail

LOGDIR="${RESERVATION_AUTOPILOT_LOGDIR:-$(cd "$(dirname "$0")" && pwd)/logs}"
mkdir -p "$LOGDIR"
STAMP=$(date +%Y%m%d-%H%M%S)

cd "$(dirname "$0")"

python3 ./reservation_autopilot.py "$@" 2>&1 | tee "$LOGDIR/run-$STAMP.json"
EXIT=${PIPESTATUS[0]}

# Keep last 7 days of logs
find "$LOGDIR" -name "run-*.json" -mtime +7 -delete 2>/dev/null || true

exit $EXIT
