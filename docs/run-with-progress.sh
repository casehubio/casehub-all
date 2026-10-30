#!/bin/bash
# Run a Maven command with incremental dashboard updates.
#
# Usage: run-with-progress.sh <phase-name> <maven-command...>
#
# Starts a background process that updates the gist every 90 seconds
# with the latest stats from the growing log. On completion, sends
# final stats with success or failure status.

set -euo pipefail

PHASE="$1"
shift
LOGFILE="/tmp/phase-$(echo "$PHASE" | tr ' :+' '---').log"

# Signal start
bash docs/update-progress.sh phase "$PHASE" in_progress || true

# Background updater: push incremental stats every 90s
(
  while true; do
    sleep 90
    bash docs/update-progress.sh phase "$PHASE" in_progress "$LOGFILE" 2>/dev/null || true
  done
) &
UPDATER_PID=$!

# Run Maven, capture output
set +e
"$@" 2>&1 | tee "$LOGFILE"
EXIT_CODE=${PIPESTATUS[0]}
set -e

# Stop background updater
kill $UPDATER_PID 2>/dev/null || true
wait $UPDATER_PID 2>/dev/null || true

# Final update with full stats
if [ "$EXIT_CODE" -eq 0 ]; then
  bash docs/update-progress.sh phase "$PHASE" success "$LOGFILE" || true
else
  bash docs/update-progress.sh phase "$PHASE" failure "$LOGFILE" || true
fi

exit $EXIT_CODE
