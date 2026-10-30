#!/bin/bash
# CI progress updater — writes to a GitHub Gist (no git history pollution).
#
# Usage:
#   update-progress.sh init                          Start a new build
#   update-progress.sh phase <name> <status>         Update a phase
#   update-progress.sh phase <name> <status> <log>   Update with stats parsed from Maven log
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

parse_maven_stats() {
  local logfile="$1"
  python3 -c "
import re, json, sys

log = open('$logfile').read()

# Module count from reactor summary
modules = len(re.findall(r'^\[INFO\] [\w. -]+ \.+ (SUCCESS|FAILURE|SKIPPED)', log, re.MULTILINE))

# Test summary — aggregate all 'Tests run:' lines
tests_run = sum(int(m) for m in re.findall(r'Tests run: (\d+)', log))
failures = sum(int(m) for m in re.findall(r'Failures: (\d+)', log))
errors = sum(int(m) for m in re.findall(r'Errors: (\d+)', log))
skipped = sum(int(m) for m in re.findall(r'Skipped: (\d+)', log))

# Total time
time_match = re.search(r'Total time:\s+([0-9:.]+)\s*(s|min)', log)
total_time_s = 0
if time_match:
    t = time_match.group(1)
    unit = time_match.group(2)
    if unit == 'min':
        parts = t.split(':')
        total_time_s = int(float(parts[0])) * 60 + (int(float(parts[1])) if len(parts) > 1 else 0)
    else:
        total_time_s = int(float(t))

# Flaky tests (surefire 3.5.5 reports Flakes: N)
flakes = sum(int(m) for m in re.findall(r'Flakes: (\d+)', log))

# Timeouts
timeouts = len(re.findall(r'TimeoutException|timed out|forkedProcessTimeoutInSeconds', log, re.IGNORECASE))

# Failed modules from reactor summary
failed_modules = re.findall(r'^\[INFO\] ([\w. -]+?) \.+ FAILURE', log, re.MULTILINE)

# Robustness events — artifact validation, retries, self-healing
corrupt_jars = len(re.findall(r'Corrupt JAR', log))
corrupt_jandex = len(re.findall(r'Corrupt Jandex', log))
retries = len(re.findall(r'retrying once|retry', log, re.IGNORECASE))

stats = {
    'modules': modules,
    'tests_run': tests_run,
    'tests_failed': failures + errors,
    'tests_skipped': skipped,
    'tests_flaky': flakes,
    'timeouts': timeouts,
    'maven_time_s': total_time_s
}
if failed_modules:
    stats['failed_modules'] = [m.strip() for m in failed_modules]
if corrupt_jars or corrupt_jandex or retries:
    stats['self_healing'] = {
        'corrupt_jars_deleted': corrupt_jars,
        'corrupt_jandex_deleted': corrupt_jandex,
        'retries': retries
    }
print(json.dumps(stats))
" 2>/dev/null || echo '{}'
}

case "${1:-}" in
  init)
    NOW=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    RUN_URL="${GITHUB_SERVER_URL:-https://github.com}/${GITHUB_REPOSITORY:-casehubio/casehub-all}/actions/runs/${GITHUB_RUN_ID:-0}"

    CURRENT=$(fetch_gist)
    UPDATED=$(python3 -c "
import json, sys
data = json.loads(sys.stdin.read())
resume_from = '${RESUME_FROM:-}'
retry_count = '${RETRY_COUNT:-0}'
data['current'] = {
    'run_id': ${GITHUB_RUN_ID:-0},
    'run_url': '$RUN_URL',
    'commit': '${GITHUB_SHA:0:7}',
    'started': '$NOW',
    'resume_from': resume_from or None,
    'retry_count': int(retry_count) if retry_count else 0,
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
    LOGFILE="${4:-}"
    NOW=$(date -u +%Y-%m-%dT%H:%M:%SZ)

    STATS='{}'
    if [ -n "$LOGFILE" ] && [ -f "$LOGFILE" ]; then
      STATS=$(parse_maven_stats "$LOGFILE")
    fi

    CURRENT=$(fetch_gist)
    UPDATED=$(python3 -c "
import json, sys
from datetime import datetime, timezone
data = json.loads(sys.stdin.read())
stats = json.loads('$STATS')
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
                if stats:
                    p.update(stats)
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
    total_tests = sum(p.get('tests_run', 0) for p in cur.get('phases', []))
    total_modules = sum(p.get('modules', 0) for p in cur.get('phases', []))
    entry = {
        'run_id': cur.get('run_id'),
        'run_url': cur.get('run_url'),
        'commit': cur.get('commit'),
        'conclusion': '$CONCLUSION',
        'started': started,
        'ended': '$NOW',
        'duration_s': int((e - s).total_seconds()),
        'total_tests': total_tests,
        'total_modules': total_modules,
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
    echo "Usage: $0 {init|phase <name> <status> [logfile]|finish <conclusion>}" >&2
    exit 1
    ;;
esac
