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
#   RESUME_FROM, RETRY_COUNT — workflow inputs (optional)
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
stats = {}

# ── Module count + slowest modules ──────────────────────────────────
try:
    reactor_lines = re.findall(
        r'^\[INFO\]\s+(.+?)\s+\.{2,}\s+(SUCCESS|FAILURE|SKIPPED)\s+\[\s*([0-9.]+)\s*s\s*\]',
        log, re.MULTILINE)
    stats['modules'] = len(reactor_lines)
    if reactor_lines:
        by_time = sorted(reactor_lines, key=lambda x: float(x[2]), reverse=True)
        stats['slowest_modules'] = [
            {'name': m[0].strip(), 'seconds': round(float(m[2]), 1)}
            for m in by_time[:5]
        ]
    failed = [m[0].strip() for m in reactor_lines if m[1] == 'FAILURE']
    if failed:
        stats['failed_modules'] = failed
except Exception:
    pass

# ── Test summary ────────────────────────────────────────────────────
try:
    stats['tests_run'] = sum(int(m) for m in re.findall(r'Tests run: (\d+)', log))
    stats['tests_failed'] = (
        sum(int(m) for m in re.findall(r'Failures: (\d+)', log)) +
        sum(int(m) for m in re.findall(r'Errors: (\d+)', log))
    )
    stats['tests_skipped'] = sum(int(m) for m in re.findall(r'Skipped: (\d+)', log))
except Exception:
    pass

# ── Flaky tests (surefire 3.5.5 Flakes: N) ─────────────────────────
try:
    stats['tests_flaky'] = sum(int(m) for m in re.findall(r'Flakes: (\d+)', log))
except Exception:
    pass

# ── Slowest tests ───────────────────────────────────────────────────
try:
    test_lines = re.findall(
        r'Time elapsed:\s+([0-9.]+)\s*s\s*.*?--\s*in\s+([\w.]+)',
        log)
    if test_lines:
        by_time = sorted(test_lines, key=lambda x: float(x[0]), reverse=True)
        stats['slowest_tests'] = [
            {'test': t[1].split('.')[-1], 'seconds': round(float(t[0]), 1)}
            for t in by_time[:5]
        ]
except Exception:
    pass

# ── Total time ──────────────────────────────────────────────────────
try:
    time_match = re.search(r'Total time:\s+([0-9:.]+)\s*(s|min)', log)
    if time_match:
        t = time_match.group(1)
        unit = time_match.group(2)
        if unit == 'min':
            parts = t.split(':')
            stats['maven_time_s'] = int(float(parts[0])) * 60 + (int(float(parts[1])) if len(parts) > 1 else 0)
        else:
            stats['maven_time_s'] = int(float(t))
except Exception:
    pass

# ── Timeouts ────────────────────────────────────────────────────────
try:
    stats['timeouts'] = len(re.findall(
        r'TimeoutException|timed out|forkedProcessTimeoutInSeconds',
        log, re.IGNORECASE))
except Exception:
    pass

# ── Compilation warnings (exclude known noise) ─────────────────────
try:
    noise = re.compile(r'LogManager|byte-buddy|Overriding managed|Some problems were encountered|bootstrap class path')
    warnings = [l for l in log.split('\n')
                if l.startswith('[WARNING]') and not noise.search(l)]
    stats['compilation_warnings'] = len(warnings)
except Exception:
    pass

# ── Deprecation warnings ───────────────────────────────────────────
try:
    stats['deprecation_warnings'] = len(re.findall(
        r'deprecated|Xlint:deprecation', log, re.IGNORECASE))
except Exception:
    pass

# ── Quarkus boot times ─────────────────────────────────────────────
try:
    boots = re.findall(r'started in\s+([0-9.]+)s', log)
    if boots:
        times = [float(b) for b in boots]
        stats['quarkus_boots'] = {
            'count': len(times),
            'avg_s': round(sum(times) / len(times), 2),
            'max_s': round(max(times), 2)
        }
except Exception:
    pass

# ── Self-healing / robustness events ───────────────────────────────
try:
    corrupt_jars = len(re.findall(r'Corrupt JAR', log))
    corrupt_jandex = len(re.findall(r'Corrupt Jandex', log))
    retries = len(re.findall(r'retrying once|Convergence failed', log, re.IGNORECASE))
    if corrupt_jars or corrupt_jandex or retries:
        stats['self_healing'] = {
            'corrupt_jars_deleted': corrupt_jars,
            'corrupt_jandex_deleted': corrupt_jandex,
            'retries': retries
        }
except Exception:
    pass

# ── Artifact size (best effort) ────────────────────────────────────
try:
    sizes = re.findall(r'Uploading.*?(\d+(?:\.\d+)?)\s*(KB|MB|GB)', log)
    if sizes:
        total_mb = 0
        for val, unit in sizes:
            v = float(val)
            if unit == 'KB': total_mb += v / 1024
            elif unit == 'MB': total_mb += v
            elif unit == 'GB': total_mb += v * 1024
        stats['artifact_size_mb'] = round(total_mb, 1)
except Exception:
    pass

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
    phases = cur.get('phases', [])
    total_tests = sum(p.get('tests_run', 0) for p in phases)
    total_modules = sum(p.get('modules', 0) for p in phases)
    total_flaky = sum(p.get('tests_flaky', 0) for p in phases)
    total_warnings = sum(p.get('compilation_warnings', 0) for p in phases)
    total_timeouts = sum(p.get('timeouts', 0) for p in phases)
    healing = {'corrupt_jars_deleted': 0, 'corrupt_jandex_deleted': 0, 'retries': 0}
    for p in phases:
        sh = p.get('self_healing', {})
        healing['corrupt_jars_deleted'] += sh.get('corrupt_jars_deleted', 0)
        healing['corrupt_jandex_deleted'] += sh.get('corrupt_jandex_deleted', 0)
        healing['retries'] += sh.get('retries', 0)
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
        'total_flaky': total_flaky,
        'total_warnings': total_warnings,
        'total_timeouts': total_timeouts,
        'self_healing': healing if any(healing.values()) else None,
        'resume_from': cur.get('resume_from'),
        'retry_count': cur.get('retry_count', 0),
        'phases': phases
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
