#!/usr/bin/env python3
"""
init-dashboard-full.py — build CI dashboard init payload from pipeline.yaml.

Outputs JSON to stdout. The caller pipes it to curl for the actual write.
This separation keeps the script testable without network access.

Usage:
  python3 docs/init-dashboard-full.py \
    --run-id 12345 --run-url "https://..." --commit "abc1234" \
    --resume-from "" --retry-count 0 \
    --pipeline docs/pipeline.yaml \
    --old-progress /tmp/old-progress.json \
  | curl -sf -X PUT "$WORKER/progress" -H "..." -d @-
"""

import argparse
import json
import sys
from datetime import datetime, timezone


def load_phases(pipeline_path):
    import yaml
    with open(pipeline_path) as f:
        pipeline = yaml.safe_load(f)

    phases = []
    for level in pipeline.get('levels', []):
        if 'base' in level:
            phases.append(level['base'])
        name_overrides = level.get('names', {})
        for stack in level.get('stacks', []):
            if isinstance(stack, str):
                phases.append(stack)
            else:
                for repo in stack:
                    phases.append(name_overrides.get(repo, repo))
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
    parser.add_argument('--old-progress', default=None)
    args = parser.parse_args()

    now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

    old = {}
    if args.old_progress:
        try:
            with open(args.old_progress) as f:
                old = json.load(f)
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

    json.dump(data, sys.stdout, indent=2)


if __name__ == '__main__':
    main()
