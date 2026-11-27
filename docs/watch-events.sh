#!/usr/bin/env bash
# watch-events.sh — block until a matching CI event fires.
#
# Usage:
#   bash docs/watch-events.sh "build.*"                    # any build termination
#   bash docs/watch-events.sh "phase.failure"              # any phase failure
#   bash docs/watch-events.sh "phase.*:connectors"         # anything on connectors
#   bash docs/watch-events.sh "level.complete:L4,*.failure" # L4 done or any failure
#   bash docs/watch-events.sh "build.success,build.failure" --poll=15
#
# Options:
#   --poll=N   Poll interval in seconds (default: 30)
#   --timeout=N  Max wait in seconds (default: 7200 = 2 hours)
#
# Output: JSON array of matching events (one line per event).
# Exit code: 0 = events found, 1 = timeout, 2 = error.

set -euo pipefail

FILTER="${1:?Usage: watch-events.sh FILTER [--poll=N] [--timeout=N]}"
POLL=30
TIMEOUT=7200

for arg in "${@:2}"; do
  case "$arg" in
    --poll=*) POLL="${arg#--poll=}" ;;
    --timeout=*) TIMEOUT="${arg#--timeout=}" ;;
  esac
done

WORKER_URL="${CI_DASHBOARD_URL:-https://casehub-ci-dashboard.casehub.workers.dev}"
SINCE=$(python3 -c "import time; print(int(time.time() * 1000))")
DEADLINE=$(($(date +%s) + TIMEOUT))

while [ "$(date +%s)" -lt "$DEADLINE" ]; do
  RESPONSE=$(curl -sf "${WORKER_URL}/events?since=${SINCE}&filter=${FILTER}" 2>/dev/null || echo "[]")
  COUNT=$(echo "$RESPONSE" | python3 -c "import json,sys; events=json.load(sys.stdin); print(len(events))" 2>/dev/null || echo "0")

  if [ "$COUNT" -gt 0 ]; then
    echo "$RESPONSE" | python3 -c "
import json, sys
events = json.load(sys.stdin)
for e in events:
    print(json.dumps(e))
"
    exit 0
  fi

  sleep "$POLL"
done

echo "timeout after ${TIMEOUT}s waiting for: $FILTER" >&2
exit 1
