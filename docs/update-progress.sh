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

WORKER_URL="${CI_DASHBOARD_URL:-https://casehub-ci-dashboard.casehub.workers.dev}"
WORKER_TOKEN="${CI_DASHBOARD_TOKEN:-}"

pipeline_phases() {
  local dir="${1:-$(cd "$(dirname "$0")" && pwd)}"
  python3 -c "
import yaml
p = yaml.safe_load(open('$dir/pipeline.yaml'))
for lv in p['levels']:
    if 'base' in lv: print(lv['base'])
    nm = lv.get('names', {})
    for s in lv.get('stacks', []):
        if isinstance(s, dict): print(s.get('name', s['repos'][0]))
        elif isinstance(s, str): print(s)
        else:
            for r in s: print(nm.get(r, r))
    if 'convergence' in lv: print(lv['convergence'])
"
}

fetch_progress() {
  curl -sf "$WORKER_URL/progress" 2>/dev/null || echo '{}'
}

update_progress() {
  local tmpfile
  tmpfile=$(mktemp)
  echo "$1" > "$tmpfile"
  curl -sf -X PUT "$WORKER_URL/progress" \
    -H "Authorization: Bearer $WORKER_TOKEN" \
    -H "Content-Type: application/json" \
    -H "X-Run-Id: ${GITHUB_RUN_ID:-0}" \
    -d @"$tmpfile" > /dev/null
  rm -f "$tmpfile"
}

update_phase_file() {
  local safe_name="$1"
  local content="$2"
  curl -sf -X PUT "$WORKER_URL/phase-$safe_name" \
    -H "Authorization: Bearer $WORKER_TOKEN" \
    -H "Content-Type: application/json" \
    -H "X-Run-Id: ${GITHUB_RUN_ID:-0}" \
    -d "$content" > /dev/null
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
except Exception as _e: print(f'::debug::stats parse: {_e}', file=__import__('sys').stderr)

try:
    stats['tests_run'] = sum(int(m) for m in re.findall(r'Tests run: (\d+)', log))
    stats['tests_failed'] = sum(int(m) for m in re.findall(r'Failures: (\d+)', log)) + sum(int(m) for m in re.findall(r'Errors: (\d+)', log))
    stats['tests_skipped'] = sum(int(m) for m in re.findall(r'Skipped: (\d+)', log))
except Exception as _e: print(f'::debug::stats parse: {_e}', file=__import__('sys').stderr)

try: stats['tests_flaky'] = sum(int(m) for m in re.findall(r'Flakes: (\d+)', log))
except Exception as _e: print(f'::debug::stats parse: {_e}', file=__import__('sys').stderr)

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
except Exception as _e: print(f'::debug::stats parse: {_e}', file=__import__('sys').stderr)

try:
    test_lines = re.findall(r'Time elapsed:\s+([0-9.]+)\s*s\s*.*?--\s*in\s+([\w.]+)', log)
    if test_lines:
        by_time = sorted(test_lines, key=lambda x: float(x[0]), reverse=True)
        stats['slowest_tests'] = [{'test': t[1].split('.')[-1], 'seconds': round(float(t[0]),1)} for t in by_time[:5]]
except Exception as _e: print(f'::debug::stats parse: {_e}', file=__import__('sys').stderr)

try:
    mod_lines = re.findall(r'^\[INFO\] ([\w. -]+?) \.+ SUCCESS \[\s*([0-9.]+)\s*s\]', log, re.MULTILINE)
    if mod_lines:
        by_time = sorted(mod_lines, key=lambda x: float(x[1]), reverse=True)
        stats['slowest_modules'] = [{'name': m[0].strip(), 'seconds': round(float(m[1]),1)} for m in by_time[:5]]
        stats['all_module_times'] = {m[0].strip(): round(float(m[1]),1) for m in mod_lines}
except Exception as _e: print(f'::debug::stats parse: {_e}', file=__import__('sys').stderr)

try:
    time_match = re.search(r'Total time:\s+([0-9:.]+)\s*(s|min)', log)
    if time_match:
        t, unit = time_match.group(1), time_match.group(2)
        if unit == 'min':
            parts = t.split(':')
            stats['maven_time_s'] = int(float(parts[0])) * 60 + (int(float(parts[1])) if len(parts)>1 else 0)
        else:
            stats['maven_time_s'] = int(float(t))
except Exception as _e: print(f'::debug::stats parse: {_e}', file=__import__('sys').stderr)

try: stats['timeouts'] = len(re.findall(r'TimeoutException|timed out', log, re.IGNORECASE))
except Exception as _e: print(f'::debug::stats parse: {_e}', file=__import__('sys').stderr)

try:
    noise = re.compile(r'LogManager|byte-buddy|Overriding managed|Some problems|bootstrap class')
    stats['compilation_warnings'] = len([l for l in log.split('\n') if l.startswith('[WARNING]') and not noise.search(l)])
except Exception as _e: print(f'::debug::stats parse: {_e}', file=__import__('sys').stderr)

try:
    failed_modules = re.findall(r'^\[INFO\] ([\w. -]+?) \.+ FAILURE', log, re.MULTILINE)
    if failed_modules: stats['failed_modules'] = [m.strip() for m in failed_modules]
except Exception as _e: print(f'::debug::stats parse: {_e}', file=__import__('sys').stderr)

try:
    loc_match = re.search(r'\[LOC\] java_src=(\d+) java_test=(\d+) yaml=(\d+) json=(\d+) ts=(\d+) xml=(\d+)', log)
    if loc_match:
        loc = {'java_src': int(loc_match.group(1)), 'java_test': int(loc_match.group(2)),
               'yaml': int(loc_match.group(3)), 'json': int(loc_match.group(4)),
               'ts': int(loc_match.group(5)), 'xml': int(loc_match.group(6))}
        loc['total'] = sum(loc.values())
        stats['loc'] = loc
except Exception as _e: print(f'::debug::stats parse: {_e}', file=__import__('sys').stderr)

try:
    corrupt_jars = len(re.findall(r'Corrupt JAR', log))
    corrupt_jandex = len(re.findall(r'Corrupt Jandex', log))
    retries = len(re.findall(r'retrying once|retry', log, re.IGNORECASE))
    if corrupt_jars or corrupt_jandex or retries:
        stats['self_healing'] = {'corrupt_jars_deleted': corrupt_jars, 'corrupt_jandex_deleted': corrupt_jandex, 'retries': retries}
except Exception as _e: print(f'::debug::stats parse: {_e}', file=__import__('sys').stderr)

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

    # Read existing phase file to preserve started timestamp
    TMPDIR_PF=$(mktemp -d)
    curl -sf "$WORKER_URL/phase-$SAFE_NAME" > "$TMPDIR_PF/existing.json" 2>/dev/null || echo '{}' > "$TMPDIR_PF/existing.json"
    echo "$STATS" > "$TMPDIR_PF/stats.json"

    PHASE_DATA=$(python3 -c "
import json
existing = json.load(open('$TMPDIR_PF/existing.json'))
stats = json.load(open('$TMPDIR_PF/stats.json'))
data = {'name': '$NAME', 'status': '$STATUS', 'updated': '$NOW', '_written_at': '$NOW'}
if '$STATUS' == 'in_progress':
    data['started'] = existing.get('started', '$NOW')
elif '$STATUS' in ('success', 'failure', 'skipped'):
    data['started'] = existing.get('started', '$NOW')
    data['ended'] = '$NOW'
    if data['started']:
        from datetime import datetime
        try:
            s = datetime.fromisoformat(data['started'].replace('Z','+00:00'))
            e = datetime.fromisoformat('$NOW'.replace('Z','+00:00'))
            data['duration_s'] = int((e - s).total_seconds())
        except Exception as _e: print(f'::debug::stats parse: {_e}', file=__import__('sys').stderr)
    data.update(stats)
print(json.dumps(data, indent=2))
")
    rm -rf "$TMPDIR_PF"
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
        if p['name'].lower() == '$NAME'.lower():
            p['status'] = '$STATUS'
            p['_written_at'] = '$NOW'
            if '$STATUS' == 'in_progress':
                p.setdefault('started', '$NOW')
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
    data['current']['_written_at'] = '$NOW'
print(json.dumps(data, indent=2))
" <<< "$CURRENT")
    update_progress "$UPDATED"
    ;;

  finish)
    CONCLUSION="${2:?conclusion required}"
    NOW=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    TMPDIR=$(mktemp -d)

    fetch_progress > "$TMPDIR/progress.json"
    RUN_ID=$(python3 -c "import json; print(json.load(open('$TMPDIR/progress.json')).get('current',{}).get('run_id',''))" 2>/dev/null || echo "")

    SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
    mkdir -p "$TMPDIR/phases"
    if [ -n "$RUN_ID" ]; then
      while IFS= read -r phase_display_name; do
        [ -z "$phase_display_name" ] && continue
        phase_name=$(echo "$phase_display_name" | tr ' :+/()' '------' | tr '[:upper:]' '[:lower:]')
        curl -sf "$WORKER_URL/phase-${RUN_ID}-${phase_name}" > "$TMPDIR/phases/${phase_name}.json" 2>/dev/null || echo "{}" > "$TMPDIR/phases/${phase_name}.json"
      done < <(pipeline_phases "$SCRIPT_DIR")
    fi

    python3 -c "
import json, os, glob
from datetime import datetime, timedelta, timezone

data = json.load(open('$TMPDIR/progress.json'))
cur = data.get('current')
if cur:
    for f in glob.glob('$TMPDIR/phases/*.json'):
        try:
            pd = json.load(open(f))
            if pd.get('name'):
                for p in cur.get('phases', []):
                    if p['name'].lower() == pd['name'].lower():
                        p.update({k:v for k,v in pd.items() if k not in ('name','updated')})
                        break
        except Exception as _e: print(f'::debug::stats parse: {_e}', file=__import__('sys').stderr)

    started = cur.get('started', '$NOW')
    s = datetime.fromisoformat(started.replace('Z','+00:00'))
    e = datetime.fromisoformat('$NOW'.replace('Z','+00:00'))
    phases = cur.get('phases', [])
    workflow_conclusion = '$CONCLUSION'
    for p in phases:
        if p.get('status') == 'in_progress':
            # Workflow says success → dashboard update was lost, phase actually passed
            p['status'] = 'success' if workflow_conclusion == 'success' else 'failure'
            p['ended'] = '$NOW'

    # Trust the workflow conclusion — it checks actual GitHub Actions job results
    # Dashboard phase updates can fail (network, timing) without affecting the build
    conclusion = workflow_conclusion

    entry = {
        'run_id': cur.get('run_id'), 'run_url': cur.get('run_url'),
        'commit': cur.get('commit'), 'conclusion': conclusion,
        'started': started, 'ended': '$NOW',
        'duration_s': int((e - s).total_seconds()),
        'total_tests': sum(p.get('tests_run', 0) for p in phases),
        'total_modules': sum(p.get('modules', 0) for p in phases),
        'total_flaky': sum(p.get('tests_flaky', 0) for p in phases),
        'total_warnings': sum(p.get('compilation_warnings', 0) for p in phases),
        'total_timeouts': sum(p.get('timeouts', 0) for p in phases),
        'phases': phases
    }
    history = data.get('history', [])
    history.insert(0, entry)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    data['history'] = [h for h in history if h.get('started', '') > cutoff][:200]
    data['current'] = None

json.dump(data, open('$TMPDIR/result.json', 'w'))
" 2>/dev/null
    update_progress "$(cat "$TMPDIR/result.json")"

    # Phase files are keyed by ${RUN_ID}-${phase_name} so they don't
    # collide across builds. No need to delete — they self-expire via
    # KV TTL (set server-side). Deleting them burned through the free-tier
    # 1,000 delete/day limit during retry-heavy builds.
    rm -rf "$TMPDIR"

    echo "::notice::Dashboard archived run as $CONCLUSION"
    ;;

  *)
    echo "Usage: $0 {phase-file <name> <status> [log]|phase <name> <status> [log]|finish <conclusion>}" >&2
    exit 1
    ;;
esac
