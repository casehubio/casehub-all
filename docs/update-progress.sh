#!/bin/bash
# Update docs/progress.json in the casehub-all repo via GitHub API.
#
# Usage:
#   update-progress.sh init                 — create current build entry
#   update-progress.sh phase <name> <status> [duration_s]  — update a phase
#   update-progress.sh finish <conclusion>  — move current to history
#
# Environment:
#   GITHUB_RUN_ID, GITHUB_SHA, GITHUB_TOKEN (set by GitHub Actions)
#
# The script fetches the current progress.json, updates it with python3,
# and commits back via the GitHub contents API.

set -euo pipefail

REPO="casehubio/casehub-all"
FILE_PATH="docs/progress.json"
BRANCH="main"

fetch_file() {
  gh api "repos/$REPO/contents/$FILE_PATH?ref=$BRANCH" --jq '{content: .content, sha: .sha}' 2>/dev/null || echo '{"content":"","sha":""}'
}

commit_file() {
  local content_b64="$1"
  local file_sha="$2"
  local message="$3"

  local body
  body=$(python3 -c "
import json, sys
d = {'message': sys.argv[1], 'content': sys.argv[2], 'branch': '$BRANCH'}
if sys.argv[3]: d['sha'] = sys.argv[3]
print(json.dumps(d))
" "$message" "$content_b64" "$file_sha")

  gh api "repos/$REPO/contents/$FILE_PATH" -X PUT --input - <<< "$body" > /dev/null
}

update_json() {
  local action="$1"
  shift

  local file_data
  file_data=$(fetch_file)
  local file_sha
  file_sha=$(echo "$file_data" | python3 -c "import sys,json; print(json.load(sys.stdin).get('sha',''))")
  local current_b64
  current_b64=$(echo "$file_data" | python3 -c "import sys,json; print(json.load(sys.stdin).get('content',''))")

  local new_b64
  new_b64=$(python3 -c "
import json, base64, sys
from datetime import datetime, timezone

action = sys.argv[1]
args = sys.argv[2:]

# Decode existing
b64_in = '''$current_b64'''
if b64_in.strip():
    data = json.loads(base64.b64decode(b64_in))
else:
    data = {'current': None, 'history': []}

now = datetime.now(timezone.utc).isoformat()
run_id = '${GITHUB_RUN_ID:-0}'
run_url = 'https://github.com/$REPO/actions/runs/' + run_id
commit = '${GITHUB_SHA:-unknown}'

if action == 'init':
    phases = [
        'Prepare', 'Slice A', 'Slice B', 'Slice C', 'Slice D',
        'Convergence',
        'G1: Core libs', 'G2: Domain', 'G3: Middleware',
        'G4: Extensions', 'G5+G6: Apps'
    ]
    data['current'] = {
        'run_id': int(run_id),
        'run_url': run_url,
        'commit': commit,
        'started': now,
        'phases': [{'name': p, 'status': 'pending'} for p in phases]
    }

elif action == 'phase' and data.get('current'):
    name = args[0] if len(args) > 0 else ''
    status = args[1] if len(args) > 1 else 'success'
    duration = int(args[2]) if len(args) > 2 else None

    for p in data['current']['phases']:
        if p['name'] == name:
            p['status'] = status
            if status == 'in_progress':
                p['started'] = now
            elif status in ('success', 'failure', 'skipped'):
                p['ended'] = now
                if duration is not None:
                    p['duration_s'] = duration
                elif 'started' in p:
                    from datetime import datetime as dt
                    try:
                        start = dt.fromisoformat(p['started'])
                        end = dt.fromisoformat(now)
                        p['duration_s'] = int((end - start).total_seconds())
                    except: pass
            break

elif action == 'finish' and data.get('current'):
    conclusion = args[0] if len(args) > 0 else 'failure'
    cur = data['current']
    entry = {
        'run_id': cur.get('run_id'),
        'conclusion': conclusion,
        'started': cur.get('started'),
        'ended': now,
        'commit': cur.get('commit', '')
    }
    if cur.get('started'):
        try:
            from datetime import datetime as dt
            s = dt.fromisoformat(cur['started'])
            e = dt.fromisoformat(now)
            entry['duration_s'] = int((e - s).total_seconds())
        except: pass
    data['history'].append(entry)
    data['history'] = data['history'][-20:]  # keep last 20
    data['current'] = None

result = base64.b64encode(json.dumps(data, indent=2).encode()).decode()
print(result)
")

  local msg="ci: update progress ($action${1:+ $1})"
  commit_file "$new_b64" "$file_sha" "$msg"
}

case "${1:-}" in
  init)    update_json init ;;
  phase)   update_json phase "${2:-}" "${3:-success}" "${4:-}" ;;
  finish)  update_json finish "${2:-failure}" ;;
  *)       echo "Usage: $0 {init|phase <name> <status> [duration]|finish <conclusion>}" >&2; exit 1 ;;
esac
