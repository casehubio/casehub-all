#!/usr/bin/env bash
# auto-retry.sh — dispatch a resume build when the current run fails.
# Called by finalize when build_ok=false and retry_count < 2.
#
# Usage: bash docs/auto-retry.sh <retry_count> <test_mode> <submodule_update> <submodule_list>
#   Reads JOB_RESULTS env var: space-separated "job=result" pairs.
#   Requires GH_TOKEN in env.

set -euo pipefail

RETRY_COUNT="${1:-0}"
TEST_MODE="${2:-skip}"
SUBMODULE_UPDATE="${3:-pinned}"
SUBMODULE_LIST="${4:-}"

RETRY=$(( RETRY_COUNT + 1 ))

# Determine resume level from job results (highest failing level)
RESUME=""
for pair in $JOB_RESULTS; do
  job="${pair%%=*}"
  result="${pair#*=}"
  if [ "$result" = "failure" ]; then
    case "$job" in
      level9|level8) RESUME="L8" ;;
      level7|level6|level5) RESUME="L5" ;;
      engine-slices|engine-base) RESUME="L4" ;;
      level3) RESUME="L3" ;;
      level2) RESUME="L2" ;;
      level1) RESUME="L1" ;;
    esac
    break
  fi
done

if [ -n "$RESUME" ]; then
  echo "::warning::Build failed at $RESUME (attempt $RETRY/2). Resuming from $RESUME."
else
  echo "::warning::Build failed at L0 (attempt $RETRY/2). Full rebuild required."
fi

gh workflow run mvn-all.yml \
  -f test_mode="$TEST_MODE" \
  -f submodule_update="$SUBMODULE_UPDATE" \
  -f submodule_list="$SUBMODULE_LIST" \
  -f retry_count="$RETRY" \
  -f resume_from="$RESUME"
