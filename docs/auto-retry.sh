#!/usr/bin/env bash
# auto-retry.sh — dispatch a resume build when the current run fails.
# Called by finalize when build_ok=false and retry_count < 2.
#
# Self-healing: analyses error messages to find the optimal resume level.
# ClassNotFoundException from upstream → resume from the source repo's level.
# Infrastructure (429, corrupt JAR) → resume from the failed level.
# Code error → resume from the failed level.
#
# Usage: bash docs/auto-retry.sh <retry_count> <test_mode> <submodule_update> <submodule_list>
#   Reads JOB_RESULTS env var: space-separated "job=result" pairs.
#   Requires GH_TOKEN in env.

set -euo pipefail

RETRY_COUNT="${1:-0}"
TEST_MODE="${2:-skip}"
SUBMODULE_UPDATE="${3:-pinned}"
SUBMODULE_LIST="${4:-}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

RETRY=$(( RETRY_COUNT + 1 ))

# Determine which level failed from job results
FAILED_LEVEL=""
for pair in $JOB_RESULTS; do
  job="${pair%%=*}"
  result="${pair#*=}"
  if [ "$result" = "failure" ]; then
    case "$job" in
      level7) FAILED_LEVEL="L7" ;;
      level6) FAILED_LEVEL="L6" ;;
      level5) FAILED_LEVEL="L5" ;;
      level4) FAILED_LEVEL="L4" ;;
      level3) FAILED_LEVEL="L3" ;;
      level2) FAILED_LEVEL="L2" ;;
      level1) FAILED_LEVEL="L1" ;;
    esac
    break
  fi
done

if [ -z "$FAILED_LEVEL" ]; then
  echo "::warning::Build failed at L0 (attempt $RETRY/2). Full rebuild required."
  RESUME=""
else
  # Try to find the optimal heal level by analysing error messages
  RESUME="$FAILED_LEVEL"

  # Fetch phase data from Worker for error analysis
  WORKER_URL="${CI_DASHBOARD_URL:-https://casehub-ci-dashboard.casehub.workers.dev}"
  ERROR_TEXT=""
  PROGRESS=$(curl -sf "$WORKER_URL/progress" 2>/dev/null || echo '{}')
  if [ -n "$PROGRESS" ]; then
    ERROR_TEXT=$(echo "$PROGRESS" | python3 -c "
import json, sys
try:
    data = json.load(sys.stdin)
    phases = data.get('current', {}).get('phases', [])
    errors = []
    for p in phases:
        if p.get('status') == 'failure':
            for e in p.get('test_errors', []):
                errors.append(e.get('error', ''))
    print(' '.join(errors))
except Exception:
    pass
" 2>/dev/null || echo "")
  fi

  # Use heal_resume to find optimal resume level
  if [ -n "$ERROR_TEXT" ] && [ -f "$SCRIPT_DIR/heal_resume.py" ]; then
    HEAL_LEVEL=$(python3 -c "
import sys, yaml
sys.path.insert(0, '$SCRIPT_DIR')
from heal_resume import find_heal_level
with open('$SCRIPT_DIR/pipeline.yaml') as f:
    pipeline = yaml.safe_load(f)
print(find_heal_level('''$ERROR_TEXT''', '$FAILED_LEVEL', pipeline))
" 2>/dev/null || echo "$FAILED_LEVEL")

    if [ "$HEAL_LEVEL" != "$FAILED_LEVEL" ]; then
      echo "::warning::Self-healing: error at $FAILED_LEVEL traced to upstream repo at $HEAL_LEVEL. Resuming from $HEAL_LEVEL instead."
      RESUME="$HEAL_LEVEL"
    fi
  fi

  echo "::warning::Build failed at $FAILED_LEVEL (attempt $RETRY/2). Resuming from $RESUME."
fi

gh workflow run mvn-all.yml \
  -f test_mode="$TEST_MODE" \
  -f submodule_update="$SUBMODULE_UPDATE" \
  -f submodule_list="$SUBMODULE_LIST" \
  -f retry_count="$RETRY" \
  -f resume_from="$RESUME"
