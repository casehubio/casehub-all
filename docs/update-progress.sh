#!/bin/bash
# CI progress updater — writes to a GitHub Gist (no git history pollution).
#
# Parallel-safe: any phase can write to its own gist file (phase-<name>.json)
# to avoid contention. The finish command merges all phase files into the
# main progress.json before archiving to history.
#
# Usage:
#   update-progress.sh phase-file <name> <status> [logfile]  Write to own file (parallel-safe)
#   update-progress.sh phase <name> <status> [logfile]       Update progress.json directly (sequential only)
#   update-progress.sh finish <conclusion>                   Merge phase files + archive to history
#
# The init step is inlined in the workflow (before checkout).

set -euo pipefail

GIST_ID="2858101a4be664207373bf9491005f7d"

fetch_progress() {
  gh api "gists/$GIST_ID" --jq '.files["progress.json"].content' 2>/dev/null || echo '{}'
}

update_progress() {
  local content="$1"
  local escaped
  escaped=$(python3 -c 'import sys,json; print(json.dumps(sys.stdin.read()))' <<< "$content")
  gh api "gists/$GIST_ID" -X PATCH \
    --input <(printf '{"files":{"progress.json":{"content":%s}}}' "$escaped") > /dev/null
}

update_phase_file() {
  local safe_name="$1"
  local content="$2"
  local filename="phase-${safe_name}.json"
  local escaped
  escaped=$(python3 -c 'import sys,json; print(json.dumps(sys.stdin.read()))' <<< "$content")
  gh api "gists/$GIST_ID" -X PATCH \
    --input <(printf '{"files":{"%s":{"content":%s}}}' "$filename" "$escaped") > /dev/null
}

parse_maven_stats() {
  local logfile="$1"
  python3 -c "
import re, json

log = open('$logfile').read()
stats = {}

try:
    mods = re.findall(r'^\[INFO\] [\w. -]+ \.+ (SUCCESS|FAILURE|SKIPPED)', log, re.MULTILINE)
    stats['modules'] = len(mods)
except: pass

try:
    stats['tests_run'] = sum(int(m) for m in re.findall(r'Tests run: (\d+)', log))
    stats['tests_failed'] = sum(int(m) for m in re.findall(r'Failures: (\d+)', log)) + sum(int(m) for m in re.findall(r'Errors: (\d+)', log))
    stats['tests_skipped'] = sum(int(m) for m in re.findall(r'Skipped: (\d+)', log))
except: pass

try: stats['tests_flaky'] = sum(int(m) for m in re.findall(r'Flakes: (\d+)', log))
except: pass

try:
    errors = []
    lines = log.split('\n')
    for i, line in enumerate(lines):
        if '<<< FAILURE!' in line or '<<< ERROR!' in line:
            m = re.search(r'\]\s+([\w.]+)\s+--\s+Time elapsed', line)
            if m:
                parts = m.group(1).split('.')
                test_name = parts[-2]+'.'+parts[-1] if len(parts)>=2 else m.group(1)
                msg = ''
                for j in range(i+1, min(i+3, len(lines))):
                    s = lines[j].strip()
                    if s and not s.startswith('[') and not s.startswith('at '):
                        msg = s[:120]; break
                errors.append({'test': test_name, 'error': msg})
    if errors: stats['test_errors'] = errors[:5]
except: pass

try:
    test_lines = re.findall(r'Time elapsed:\s+([0-9.]+)\s*s\s*.*?--\s*in\s+([\w.]+)', log)
    if test_lines:
        by_time = sorted(test_lines, key=lambda x: float(x[0]), reverse=True)
        stats['slowest_tests'] = [{'test': t[1].split('.')[-1], 'seconds': round(float(t[0]),1)} for t in by_time[:5]]
except: pass

try:
    mod_lines = re.findall(r'^\[INFO\] ([\w. -]+?) \.+ SUCCESS \[\s*([0-9.]+)\s*s\]', log, re.MULTILINE)
    if mod_lines:
        by_time = sorted(mod_lines, key=lambda x: float(x[1]), reverse=True)
        stats['slowest_modules'] = [{'name': m[0].strip(), 'seconds': round(float(m[1]),1)} for m in by_time[:5]]
except: pass

try:
    time_match = re.search(r'Total time:\s+([0-9:.]+)\s*(s|min)', log)
    if time_match:
        t, unit = time_match.group(1), time_match.group(2)
        if unit == 'min':
            parts = t.split(':')
            stats['maven_time_s'] = int(float(parts[0])) * 60 + (int(float(parts[1])) if len(parts)>1 else 0)
        else:
            stats['maven_time_s'] = int(float(t))
except: pass

try: stats['timeouts'] = len(re.findall(r'TimeoutException|timed out', log, re.IGNORECASE))
except: pass

try:
    noise = re.compile(r'LogManager|byte-buddy|Overriding managed|Some problems|bootstrap class')
    stats['compilation_warnings'] = len([l for l in log.split('\n') if l.startswith('[WARNING]') and not noise.search(l)])
except: pass

try:
    failed_modules = re.findall(r'^\[INFO\] ([\w. -]+?) \.+ FAILURE', log, re.MULTILINE)
    if failed_modules: stats['failed_modules'] = [m.strip() for m in failed_modules]
except: pass

try:
    corrupt_jars = len(re.findall(r'Corrupt JAR', log))
    corrupt_jandex = len(re.findall(r'Corrupt Jandex', log))
    retries = len(re.findall(r'retrying once|retry', log, re.IGNORECASE))
    if corrupt_jars or corrupt_jandex or retries:
        stats['self_healing'] = {'corrupt_jars_deleted': corrupt_jars, 'corrupt_jandex_deleted': corrupt_jandex, 'retries': retries}
except: pass

print(json.dumps(stats))
" 2>/dev/null || echo '{}'
}

case "${1:-}" in
  phase-file)
    NAME="${2:?phase name required}"
    STATUS="${3:?status required}"
    LOGFILE="${4:-}"
    NOW=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    RUN_ID="${GITHUB_RUN_ID:-0}"
    SAFE_NAME="${RUN_ID}-$(echo "$NAME" | tr ' :+/()' '------' | tr '[:upper:]' '[:lower:]')"

    STATS='{}'
    if [ -n "$LOGFILE" ] && [ -f "$LOGFILE" ]; then
      STATS=$(parse_maven_stats "$LOGFILE")
    fi

    PHASE_DATA=$(python3 -c "
import json
stats = json.loads('''$STATS''')
data = {'name': '$NAME', 'status': '$STATUS', 'updated': '$NOW'}
if '$STATUS' == 'in_progress':
    data['started'] = '$NOW'
elif '$STATUS' in ('success', 'failure', 'skipped'):
    data['ended'] = '$NOW'
    data.update(stats)
print(json.dumps(data, indent=2))
")
    update_phase_file "$SAFE_NAME" "$PHASE_DATA"
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

    CURRENT=$(fetch_progress)
    UPDATED=$(python3 -c "
import json, sys
from datetime import datetime, timezone
data = json.loads(sys.stdin.read())
stats = json.loads('''$STATS''')
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
    update_progress "$UPDATED"
    ;;

  finish)
    CONCLUSION="${2:?conclusion required}"
    NOW=$(date -u +%Y-%m-%dT%H:%M:%SZ)

    # Read ALL gist files in one call
    GIST_FULL=$(gh api "gists/$GIST_ID" --jq '.files | to_entries | map({key: .key, value: .value.content}) | from_entries')

    UPDATED=$(python3 -c "
import json, sys
from datetime import datetime, timedelta, timezone

files = json.loads(sys.stdin.read())
data = json.loads(files.get('progress.json', '{}'))
cur = data.get('current')
if cur:
    # Merge phase files for THIS run only
    run_prefix = 'phase-' + str(cur.get('run_id', '')) + '-'
    for key, content in files.items():
        if key.startswith(run_prefix) and key.endswith('.json'):
            try:
                pd = json.loads(content)
                for p in cur.get('phases', []):
                    if p['name'] == pd.get('name'):
                        p.update({k:v for k,v in pd.items() if k not in ('name','updated')})
                        break
            except: pass

    started = cur.get('started', '$NOW')
    s = datetime.fromisoformat(started.replace('Z','+00:00'))
    e = datetime.fromisoformat('$NOW'.replace('Z','+00:00'))
    phases = cur.get('phases', [])
    total_tests = sum(p.get('tests_run', 0) for p in phases)
    total_modules = sum(p.get('modules', 0) for p in phases)
    total_flaky = sum(p.get('tests_flaky', 0) for p in phases)
    total_warnings = sum(p.get('compilation_warnings', 0) for p in phases)
    total_timeouts = sum(p.get('timeouts', 0) for p in phases)
    healing = {'corrupt_jars_deleted':0,'corrupt_jandex_deleted':0,'retries':0}
    for p in phases:
        sh = p.get('self_healing', {})
        for k in healing: healing[k] += sh.get(k, 0)

    entry = {
        'run_id': cur.get('run_id'), 'run_url': cur.get('run_url'),
        'commit': cur.get('commit'), 'conclusion': '$CONCLUSION',
        'started': started, 'ended': '$NOW',
        'duration_s': int((e - s).total_seconds()),
        'total_tests': total_tests, 'total_modules': total_modules,
        'total_flaky': total_flaky, 'total_warnings': total_warnings,
        'total_timeouts': total_timeouts,
        'self_healing': healing if any(healing.values()) else None,
        'phases': phases
    }
    history = data.get('history', [])
    history.insert(0, entry)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    history = [h for h in history if h.get('started', '') > cutoff]
    data['history'] = history[:200]
    data['current'] = None

print(json.dumps(data, indent=2))
" <<< "$GIST_FULL")
    update_progress "$UPDATED"

    # Clean up phase files
    CLEANUP=$(python3 -c "
import json, sys
files = json.loads(sys.stdin.read())
nulls = {k: None for k in files if k.startswith('phase-') and k.endswith('.json')}
print(json.dumps({'files': nulls}))
" <<< "$GIST_FULL")
    gh api "gists/$GIST_ID" -X PATCH --input - <<< "$CLEANUP" > /dev/null 2>&1 || true

    echo "::notice::Dashboard archived run as $CONCLUSION"
    ;;

  *)
    echo "Usage: $0 {phase-file <name> <status> [log]|phase <name> <status> [log]|finish <conclusion>}" >&2
    exit 1
    ;;
esac
