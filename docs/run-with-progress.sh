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

# Count lines of code per file type (repo dir = phase name)
# Must not fail the build — wrapped in set +e
set +e
if [ -d "$PHASE" ]; then
  _loc_count() { find "$@" 2>/dev/null | xargs wc -l 2>/dev/null | tail -1 | awk '{print $1+0}'; }
  JAVA_SRC=$(_loc_count "$PHASE" -name "*.java" -not -path "*/test/*" -not -path "*/target/*")
  JAVA_TEST=$(_loc_count "$PHASE" -name "*.java" -path "*/test/*" -not -path "*/target/*")
  YAML_LOC=$(_loc_count "$PHASE" \( -name "*.yaml" -o -name "*.yml" \) -not -path "*/target/*")
  JSON_LOC=$(_loc_count "$PHASE" -name "*.json" -not -path "*/target/*" -not -path "*/node_modules/*" -not -path "*/.yarn/*")
  TS_LOC=$(_loc_count "$PHASE" \( -name "*.ts" -o -name "*.tsx" \) -not -path "*/target/*" -not -path "*/node_modules/*" -not -path "*/.yarn/*" -not -path "*/dist/*")
  XML_LOC=$(_loc_count "$PHASE" -name "pom.xml")
  echo "[LOC] java_src=${JAVA_SRC:-0} java_test=${JAVA_TEST:-0} yaml=${YAML_LOC:-0} json=${JSON_LOC:-0} ts=${TS_LOC:-0} xml=${XML_LOC:-0}" >> "$LOGFILE"
fi
set -e

# Final update with stats
FINAL_SENT=1
if [ "$EXIT_CODE" -eq 0 ]; then
  bash docs/update-progress.sh "$MODE" "$PHASE" success "$LOGFILE" 2>/dev/null || true
else
  bash docs/update-progress.sh "$MODE" "$PHASE" failure "$LOGFILE" 2>/dev/null || true
fi

exit $EXIT_CODE
