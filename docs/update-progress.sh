#!/bin/bash
# CI progress updater — writes to a GitHub Gist (no git history pollution).
#
# Usage:
#   update-progress.sh init                          Start a new build
#   update-progress.sh phase <name> <status>         Update a phase
#   update-progress.sh finish <conclusion>           Archive current run to history
#
# Environment:
#   GITHUB_RUN_ID, GITHUB_SHA, GITHUB_SERVER_URL, GITHUB_REPOSITORY
#   GH_TOKEN or GITHUB_TOKEN — for gist API access
#
# Statuses: pending | in_progress | success | failure | skipped

set -euo pipefail

GIST_ID="2858101a4be664207373bf9491005f7d"

fetch_gist() {
  gh api "gists/$GIST_ID" --jq '.files["progress.json"].content'
}

update_gist() {
  local json_content="$1"
  local escaped
  escaped=$(python3 -c 'import sys,json; print(json.dumps(sys.stdin.read()))' <<< "$json_content")
  gh api "gists/$GIST_ID" -X PATCH \
    --input <(printf '{"files":{"progress.json":{"content":%s}}}' "$escaped") > /dev/null
}

case "${1:-}" in
  init)
    NOW=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    RUN_URL="${GITHUB_SERVER_URL:-https://github.com}/${GITHUB_REPOSITORY:-casehubio/casehub-all}/actions/runs/${GITHUB_RUN_ID:-0}"

    CURRENT=$(fetch_gist)
    UPDATED=$(python3 -c "
import json, sys
data = json.loads(sys.stdin.read())
data['current'] = {
    'run_id': ${GITHUB_RUN_ID:-0},
    'run_url': '$RUN_URL',
    'commit': '${GITHUB_SHA:0:7}',
    'started': '$NOW',
    'phases': [{'name': p, 'status': 'pending'} for p in [
        'Prepare + Platform base', 'Slice A', 'Slice B', 'Slice C', 'Slice D',
        'Platform convergence', 'G1: Core libs', 'G2: Domain',
        'G3: Middleware', 'G4: Extensions', 'G5+G6: Apps'
    ]]
}
print(json.dumps(data, indent=2))
" <<< "$CURRENT")
    update_gist "$UPDATED"
    echo "::notice::Dashboard: https://casehubio.github.io/casehub-all/"
    ;;

  phase)
    NAME="${2:?phase name required}"
    STATUS="${3:?status required}"
    NOW=$(date -u +%Y-%m-%dT%H:%M:%SZ)

    CURRENT=$(fetch_gist)
    UPDATED=$(python3 -c "
import json, sys
from datetime import datetime, timezone
data = json.loads(sys.stdin.read())
if data.get('current'):
    for p in data['current']['phases']:
        if p['name'] == '$NAME':
            p['status'] = '$STATUS'
            if '$STATUS' == 'in_progress':
                p['started'] = '$NOW'
            elif '$STATUS' in ('success', 'failure', 'skipped'):
                p.setdefault('started', '$NOW')
                p['ended'] = '$NOW'
                if 'started' in p:
                    s = datetime.fromisoformat(p['started'].replace('Z','+00:00'))
                    e = datetime.fromisoformat('$NOW'.replace('Z','+00:00'))
                    p['duration_s'] = int((e - s).total_seconds())
            break
print(json.dumps(data, indent=2))
" <<< "$CURRENT")
    update_gist "$UPDATED"
    ;;

  finish)
    CONCLUSION="${2:?conclusion required}"
    NOW=$(date -u +%Y-%m-%dT%H:%M:%SZ)

    CURRENT=$(fetch_gist)
    UPDATED=$(python3 -c "
import json, sys
from datetime import datetime, timedelta, timezone
data = json.loads(sys.stdin.read())
cur = data.get('current')
if cur:
    started = cur.get('started', '$NOW')
    s = datetime.fromisoformat(started.replace('Z','+00:00'))
    e = datetime.fromisoformat('$NOW'.replace('Z','+00:00'))
    entry = {
        'run_id': cur.get('run_id'),
        'run_url': cur.get('run_url'),
        'commit': cur.get('commit'),
        'conclusion': '$CONCLUSION',
        'started': started,
        'ended': '$NOW',
        'duration_s': int((e - s).total_seconds()),
        'phases': cur.get('phases', [])
    }
    history = data.get('history', [])
    history.insert(0, entry)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    history = [h for h in history if h.get('started', '') > cutoff]
    data['history'] = history[:200]
    data['current'] = None
print(json.dumps(data, indent=2))
" <<< "$CURRENT")
    update_gist "$UPDATED"
    echo "::notice::Dashboard archived run as $CONCLUSION"
    ;;

  *)
    echo "Usage: $0 {init|phase <name> <status>|finish <conclusion>}" >&2
    exit 1
    ;;
esac
