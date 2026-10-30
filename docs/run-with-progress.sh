#!/bin/bash
# Run a Maven command with dashboard updates at start and end.
#
# Usage:
#   run-with-progress.sh <phase-name> <maven-command...>          Sequential (updates progress.json)
#   run-with-progress.sh --parallel <phase-name> <maven-command...>  Parallel-safe (own gist file)
#
# Sequential phases (convergence, G1-G6) update progress.json directly.
# Parallel phases (slices, future parallel groups) write to their own
# gist file (phase-<name>.json) to avoid contention.

set -euo pipefail

MODE="phase"
if [ "${1:-}" = "--parallel" ]; then
  MODE="phase-file"
  shift
fi

PHASE="$1"
shift
LOGFILE="/tmp/phase-$(echo "$PHASE" | tr ' :+/()' '------').log"

# Signal start
bash docs/update-progress.sh "$MODE" "$PHASE" in_progress 2>/dev/null || true

# Run command, capture output
set +e
"$@" 2>&1 | tee "$LOGFILE"
EXIT_CODE=${PIPESTATUS[0]}
set -e

# Final update with stats
if [ "$EXIT_CODE" -eq 0 ]; then
  bash docs/update-progress.sh "$MODE" "$PHASE" success "$LOGFILE" 2>/dev/null || true
else
  bash docs/update-progress.sh "$MODE" "$PHASE" failure "$LOGFILE" 2>/dev/null || true
fi

exit $EXIT_CODE
