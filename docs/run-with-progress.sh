#!/bin/bash
# Run a Maven command with dashboard updates at start and end.
#
# Usage:
#   run-with-progress.sh <phase-name> <maven-command...>             Sequential
#   run-with-progress.sh --parallel <phase-name> <maven-command...>  Parallel-safe
#
# The "in_progress" update is skipped if an early inline curl already
# set it (e.g., the job-start step). The started timestamp is preserved
# so duration includes setup time, not just Maven execution.

set -euo pipefail

MODE="phase"
if [ "${1:-}" = "--parallel" ]; then
  MODE="phase-file"
  shift
fi

PHASE="$1"
shift
LOGFILE="/tmp/phase-$(echo "$PHASE" | tr ' :+/()' '------').log"

# Ensure final update fires even if the script crashes
cleanup() {
  local code=$?
  if [ "$code" -ne 0 ] && [ -z "${FINAL_SENT:-}" ]; then
    bash docs/update-progress.sh "$MODE" "$PHASE" failure "$LOGFILE" 2>/dev/null || true
  fi
}
trap cleanup EXIT

# Signal start (won't overwrite existing started timestamp)
bash docs/update-progress.sh "$MODE" "$PHASE" in_progress 2>/dev/null || true

# Run command, capture output
set +e
"$@" 2>&1 | tee "$LOGFILE"
EXIT_CODE=${PIPESTATUS[0]}
set -e

# Final update with stats
FINAL_SENT=1
if [ "$EXIT_CODE" -eq 0 ]; then
  bash docs/update-progress.sh "$MODE" "$PHASE" success "$LOGFILE" 2>/dev/null || true
else
  bash docs/update-progress.sh "$MODE" "$PHASE" failure "$LOGFILE" 2>/dev/null || true
fi

exit $EXIT_CODE
