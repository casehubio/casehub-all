#!/usr/bin/env python3
"""
init-dashboard-full.py — initialize CI dashboard for a new build run.
Archives the previous run's progress, then sets up the new run with
phase list derived from pipeline.yaml.

Called by the prepare job at the start of a full (non-resume) build.

Usage:
  python3 docs/init-dashboard-full.py \
    --run-id $GITHUB_RUN_ID \
    --run-url "$RUN_URL" \
    --commit "${GITHUB_SHA:0:7}" \
    --resume-from "$RESUME_FROM" \
    --retry-count 0 \
    --pipeline docs/pipeline.yaml \
    --dashboard-url "$CI_DASHBOARD_URL" \
    --dashboard-token "$CI_DASHBOARD_TOKEN"
"""

import argparse
import json
import sys
import urllib.request
from datetime import datetime, timezone


def load_phases(pipeline_path):
    import yaml
    with open(pipeline_path) as f:
        pipeline = yaml.safe_load(f)

    phases = []
    for level in pipeline.get('levels', []):
        if 'base' in level:
            phases.append(level['base'])
        for stack in level.get('stacks', []):
            phases.append(stack if isinstance(stack, str) else ', '.join(stack))
        if 'convergence' in level:
            phases.append(level['convergence'])
    return phases


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--run-id', required=True, type=int)
    parser.add_argument('--run-url', required=True)
    parser.add_argument('--commit', required=True)
    parser.add_argument('--resume-from', default='')
    parser.add_argument('--retry-count', type=int, default=0)
    parser.add_argument('--pipeline', default='docs/pipeline.yaml')
    parser.add_argument('--dashboard-url', required=True)
    parser.add_argument('--dashboard-token', required=True)
    args = parser.parse_args()

    now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    worker = args.dashboard_url

    # Fetch previous progress
    try:
        req = urllib.request.Request(f'{worker}/progress')
        with urllib.request.urlopen(req, timeout=10) as resp:
            old = json.loads(resp.read())
    except Exception:
        old = {}

    history = old.get('history', [])
    prev = old.get('current')
    if prev and prev.get('run_id'):
        phases = prev.get('phases', [])
        entry = {
            'run_id': prev.get('run_id'),
            'run_url': prev.get('run_url'),
            'commit': prev.get('commit'),
            'started': prev.get('started', now),
            'ended': now,
            'total_tests': sum(p.get('tests_run', 0) for p in phases),
            'total_modules': sum(p.get('modules', 0) for p in phases),
            'phases': phases,
        }
        failed = [p['name'] for p in phases if p.get('status') == 'failure']
        entry['conclusion'] = ('failure' if failed
                               else ('success' if any(p.get('status') == 'success' for p in phases)
                                     else 'unknown'))
        history.insert(0, entry)

    # Build new progress
    phase_list = load_phases(args.pipeline)
    phase_objects = [{'name': p, 'status': 'pending'} for p in phase_list]
    if phase_objects:
        phase_objects[0]['status'] = 'in_progress'
        phase_objects[0]['started'] = now

    data = {
        'current': {
            'run_id': args.run_id,
            'run_url': args.run_url,
            'commit': args.commit,
            'started': now,
            'resume_from': args.resume_from or None,
            'retry_count': args.retry_count,
            'phases': phase_objects,
        },
        'history': history[:200],
        '_archives': old.get('_archives', []),
    }

    # Push to dashboard
    payload = json.dumps(data, indent=2).encode()
    req = urllib.request.Request(
        f'{worker}/progress',
        data=payload,
        method='PUT',
        headers={
            'Authorization': f'Bearer {args.dashboard_token}',
            'Content-Type': 'application/json',
            'X-Run-Id': str(args.run_id),
            'X-Init': 'true',
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
    except Exception as e:
        print(f'::warning::Dashboard init failed: {e}', file=sys.stderr)

    print('::notice::Dashboard: https://casehub.org/casehub-all/')


if __name__ == '__main__':
    main()
