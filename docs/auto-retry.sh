#!/usr/bin/env bash
# auto-retry.sh — dispatch a resume build when the current run fails.
# Called by finalize when build_ok=false and retry_count < 2.
#
# Usage: bash docs/auto-retry.sh <retry_count> <test_mode> <submodule_update> <submodule_list> <results_json>
#   results_json: JSON object mapping job names to results,
#     e.g. '{"level9":"failure","level8":"success",...}'
#
# Requires GH_TOKEN in env.

set -euo pipefail

RETRY_COUNT="${1:-0}"
TEST_MODE="${2:-skip}"
SUBMODULE_UPDATE="${3:-pinned}"
SUBMODULE_LIST="${4:-}"
RESULTS_JSON="${5:-{}}"

RETRY=$(( RETRY_COUNT + 1 ))

# Determine resume level from job results (highest failing level)
RESUME=$(python3 -c "
import json, sys
results = json.loads('$RESULTS_JSON')
level_map = [
    ('level9', 'L8'), ('level8', 'L8'),
    ('level7', 'L5'), ('level6', 'L5'), ('level5', 'L5'),
    ('engine-slices', 'L4'), ('engine-base', 'L4'),
    ('work-slices', 'L3'), ('work-base', 'L3'),
    ('level2', 'L2'), ('level1', 'L1'),
]
for job, resume in level_map:
    if results.get(job) == 'failure':
        print(resume)
        sys.exit(0)
print('')
")

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
