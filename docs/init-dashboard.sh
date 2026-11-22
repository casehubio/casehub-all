#!/bin/bash
# Initialize the CI dashboard for a resumed build.
# Archives the previous run's progress and creates a fresh entry.
#
# Usage: init-dashboard.sh <run_id> <run_url> <commit> <resume_from> [retry_count]
#
# Env: CI_DASHBOARD_URL, CI_DASHBOARD_TOKEN

set -euo pipefail

RUN_ID="$1"
RUN_URL="$2"
COMMIT="$3"
RESUME_FROM="$4"
RETRY_COUNT="${5:-0}"

WORKER="${CI_DASHBOARD_URL:-https://casehub-ci-dashboard.casehub.workers.dev}"
TOKEN="${CI_DASHBOARD_TOKEN:-}"
TMPDIR=$(mktemp -d)
trap 'rm -rf "$TMPDIR"' EXIT

curl -sf "$WORKER/progress" > "$TMPDIR/old.json" 2>/dev/null || echo '{}' > "$TMPDIR/old.json"
NOW=$(date -u +%Y-%m-%dT%H:%M:%SZ)

python3 -c "
import json, yaml
old = json.load(open('$TMPDIR/old.json'))
history = old.get('history', [])
prev = old.get('current')
if prev and prev.get('run_id') and prev['run_id'] != $RUN_ID:
    phases = prev.get('phases', [])
    failed = [p['name'] for p in phases if p.get('status') == 'failure']
    entry = dict(prev, conclusion='failure' if failed else 'success', ended='$NOW',
        total_tests=sum(p.get('tests_run',0) for p in phases),
        total_modules=sum(p.get('modules',0) for p in phases), phases=phases)
    history.insert(0, entry)
_p = yaml.safe_load(open('docs/pipeline.yaml'))
phases = [n for _l in _p['levels'] for _g in [([_l['base']] if 'base' in _l else [])]+_l['stacks']+[([_l['convergence']] if 'convergence' in _l else [])] for n in _g]
data = {'current':{'run_id':$RUN_ID,'run_url':'$RUN_URL','commit':'$COMMIT','started':'$NOW','resume_from':'$RESUME_FROM' or None,'retry_count':$RETRY_COUNT,'phases':[{'name':p,'status':'pending'} for p in phases]},'history':history[:200],'_archives':old.get('_archives',[])}
json.dump(data, open('$TMPDIR/body.json','w'))
" 2>/dev/null

if [ -f "$TMPDIR/body.json" ]; then
  curl -sf -X PUT "$WORKER/progress" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -H "X-Init: true" \
    -d @"$TMPDIR/body.json" > /dev/null || true
fi
