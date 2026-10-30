#!/bin/bash
# Run a Maven command with dashboard updates at start and end.
#
# Usage: run-with-progress.sh <phase-name> <maven-command...>
#
# Updates the gist at phase start (in_progress) and end (success/failure)
# with stats parsed from the Maven log.
#
# Background incremental updates are disabled to avoid race conditions
# when multiple phases run in parallel (e.g., slices A-D).

set -euo pipefail

PHASE="$1"
shift
LOGFILE="/tmp/phase-$(echo "$PHASE" | tr ' :+' '---').log"

# Signal start
bash docs/update-progress.sh phase "$PHASE" in_progress 2>/dev/null || true

# Run Maven, capture output
set +e
"$@" 2>&1 | tee "$LOGFILE"
EXIT_CODE=${PIPESTATUS[0]}
set -e

# Final update with full stats
if [ "$EXIT_CODE" -eq 0 ]; then
  bash docs/update-progress.sh phase "$PHASE" success "$LOGFILE" 2>/dev/null || true
else
  bash docs/update-progress.sh phase "$PHASE" failure "$LOGFILE" 2>/dev/null || true
fi

exit $EXIT_CODE
