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


def load_pipeline(pipeline_path):
    import yaml
    with open(pipeline_path) as f:
        return yaml.safe_load(f)


def phase_names_for_level(level):
    names = []
    if 'base' in level:
        names.append(level['base'])
    name_overrides = level.get('names', {})
    for stack in level.get('stacks', []):
        if isinstance(stack, dict):
            names.append(stack.get('name', ', '.join(stack['repos'])))
        elif isinstance(stack, str):
            names.append(stack)
        else:
            for repo in stack:
                names.append(name_overrides.get(repo, repo))
    if 'convergence' in level:
        names.append(level['convergence'])
    return names


def load_phases(pipeline):
    phases = []
    for level in pipeline.get('levels', []):
        phases.extend(phase_names_for_level(level))
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
            'resume_from': prev.get('resume_from'),
            'total_tests': sum(p.get('tests_run', 0) for p in phases),
            'total_modules': sum(p.get('modules', 0) for p in phases),
            'total_flaky': sum(p.get('tests_flaky', 0) for p in phases),
            'phases': phases,
        }
        failed = [p['name'] for p in phases if p.get('status') == 'failure']
        entry['conclusion'] = ('failure' if failed
                               else ('success' if any(p.get('status') == 'success' for p in phases)
                                     else 'unknown'))
        dur = prev.get('duration_s')
        if not dur and prev.get('started'):
            try:
                from datetime import datetime as dt
                s = dt.fromisoformat(prev['started'].replace('Z', '+00:00'))
                e = dt.fromisoformat(now.replace('Z', '+00:00'))
                dur = int((e - s).total_seconds())
            except Exception:
                pass
        if dur:
            entry['duration_s'] = dur
        history.insert(0, entry)

    pipeline = load_pipeline(args.pipeline)
    phase_list = load_phases(pipeline)

    # On resume: carry forward previous run's phase data for levels before resume point
    prev_phase_map = {}
    cached_phases = set()
    if args.resume_from and history:
        prev_entry = history[0]
        for p in prev_entry.get('phases', []):
            prev_phase_map[p['name']] = p

        resume_level_idx = None
        for i, level in enumerate(pipeline.get('levels', [])):
            if level.get('id') == args.resume_from:
                resume_level_idx = i
                break

        if resume_level_idx is not None:
            for level in pipeline.get('levels', [])[:resume_level_idx]:
                for name in phase_names_for_level(level):
                    cached_phases.add(name)

    phase_objects = []
    for p_name in phase_list:
        if p_name in cached_phases and p_name in prev_phase_map:
            prev = dict(prev_phase_map[p_name])
            prev['name'] = p_name
            prev['cached'] = True
            if prev.get('status') != 'success':
                prev['status'] = 'success'
            phase_objects.append(prev)
        else:
            phase_objects.append({'name': p_name, 'status': 'pending'})

    if phase_objects and not args.resume_from:
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
