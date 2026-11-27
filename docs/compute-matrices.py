#!/usr/bin/env python3
"""
compute-matrices.py — read pipeline.yaml, output GitHub Actions matrix JSON
per level as step outputs.

Called by the 'config' job in mvn-all.yml. Each output is a JSON string
suitable for fromJSON() in a matrix: definition.

Usage:
  python3 docs/compute-matrices.py >> "$GITHUB_OUTPUT"
  python3 docs/compute-matrices.py --preview     # print without GITHUB_OUTPUT format
"""

import json
import os
import sys
import argparse


def load_pipeline(path='docs/pipeline.yaml'):
    try:
        import yaml
    except ImportError:
        print('ERROR: PyYAML required. pip install pyyaml', file=sys.stderr)
        sys.exit(1)

    with open(path) as f:
        return yaml.safe_load(f)


def build_matrix(level):
    """Build a GitHub Actions matrix JSON for a level.

    Sliced levels (slices > 0) get a slice matrix: {slice: [a, b, c, ...]}.
    Non-sliced levels get an include matrix from their stacks.
    """
    level_id = level['id']
    stacks = level.get('stacks', [])

    if level.get('slices'):
        labels = [chr(ord('a') + i) for i in range(level['slices'])]
        return {'slice': labels}

    pom_overrides = level.get('pom', {})
    maven_extra = level.get('maven_extra', {})
    name_overrides = level.get('names', {})

    includes = []
    for stack in stacks:
        # Stack can be a list of repo names OR an object with repos/name/maven_extra
        if isinstance(stack, dict):
            repos = stack['repos']
            repos_str = ' '.join(repos)
            name = stack.get('name', ', '.join(repos))
            entry = {'name': name, 'repos': repos_str}
            if stack.get('maven_extra'):
                entry['maven_extra'] = stack['maven_extra']
            stack_poms = {}
            for repo in repos:
                if repo in pom_overrides:
                    stack_poms[repo] = pom_overrides[repo]
            if stack_poms:
                entry['pom_overrides'] = json.dumps(stack_poms)
        else:
            repos_str = ' '.join(stack)
            display_parts = [name_overrides.get(r, r) for r in stack]
            name = ', '.join(display_parts)
            entry = {'name': name, 'repos': repos_str}

            extras = []
            for repo in stack:
                if repo in maven_extra:
                    extras.append(maven_extra[repo])
            if extras:
                entry['maven_extra'] = ' '.join(extras)

            stack_poms = {}
            for repo in stack:
                if repo in pom_overrides:
                    stack_poms[repo] = pom_overrides[repo]
            if stack_poms:
                entry['pom_overrides'] = json.dumps(stack_poms)

        includes.append(entry)

    return {'include': includes}


def build_resume_levels(pipeline):
    """Extract valid resume_from values from pipeline."""
    return [level['id'] for level in pipeline.get('levels', [])
            if level.get('resume')]


def build_all_repos(pipeline):
    """Extract all unique repos from pipeline for submodule init."""
    repos = set()
    for level in pipeline.get('levels', []):
        if level.get('repos'):
            repos.update(level['repos'])
        for stack in level.get('stacks', []):
            if isinstance(stack, dict):
                repos.update(stack['repos'])
            elif not level.get('slices'):
                repos.update(stack)
    return sorted(repos)


def collect_test_slices(pipeline, test_mode):
    """Collect test slices that should be injected into the next level.

    Returns a dict: next_level_key → list of matrix entries.
    Only active when test_mode=run.
    """
    injections = {}
    if test_mode != 'run':
        return injections

    levels = pipeline.get('levels', [])
    for i, level in enumerate(levels):
        if i + 1 >= len(levels):
            continue
        next_key = levels[i + 1]['id'].lower()
        for stack in level.get('stacks', []):
            if isinstance(stack, dict) and 'test_slices' in stack:
                repos = stack['repos']
                pom_overrides = level.get('pom', {})
                pom = pom_overrides.get(repos[0], repos[0] + '/pom.xml')
                for ts in stack['test_slices']:
                    entry = {
                        'name': ts['name'],
                        'repos': ' '.join(repos),
                        'test_only': 'true',
                        'maven_extra': f"-pl {ts['modules']}",
                    }
                    if pom != repos[0] + '/pom.xml':
                        entry['pom_overrides'] = json.dumps({repos[0]: pom})
                    injections.setdefault(next_key, []).append(entry)
    return injections


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--preview', action='store_true',
                        help='Print outputs in human-readable format')
    parser.add_argument('--pipeline', default='docs/pipeline.yaml')
    parser.add_argument('--resume-from', default='',
                        help='Resume level (e.g. L1)')
    parser.add_argument('--resume-repos', default='',
                        help='Only rebuild these repos at the resume level (comma-separated)')
    parser.add_argument('--test-mode', default='skip',
                        help='Test mode (skip/run/dry)')
    args = parser.parse_args()

    pipeline = load_pipeline(args.pipeline)
    resume_level = args.resume_from.lower() if args.resume_from else ''
    resume_repos = set(r.strip() for r in args.resume_repos.split(',') if r.strip()) if args.resume_repos else set()

    test_slices = collect_test_slices(pipeline, args.test_mode)

    matrices = {}
    timeouts = {}
    for level in pipeline.get('levels', []):
        level_id = level['id']
        key = level_id.lower()

        if level.get('slices') and level_id != 'L0':
            matrices[key] = build_matrix(level)
        elif not level.get('slices'):
            matrix = build_matrix(level)

            # When test_mode=run and a stack has test_slices, compile-only at this level
            if args.test_mode == 'run':
                for entry in matrix.get('include', []):
                    for stack in level.get('stacks', []):
                        if isinstance(stack, dict) and 'test_slices' in stack:
                            repos_str = ' '.join(stack['repos'])
                            if entry.get('repos') == repos_str:
                                entry['skip_tests'] = 'true'

            if key == resume_level and resume_repos and 'include' in matrix:
                matrix['include'] = [e for e in matrix['include']
                                     if any(r in resume_repos for r in e['repos'].split())]

            # Inject test slices from previous level
            if key in test_slices:
                matrix.setdefault('include', []).extend(test_slices[key])

            matrices[key] = matrix

        timeouts[key] = level.get('timeout', 30)

    outputs = {
        'matrices': json.dumps(matrices),
        'timeouts': json.dumps(timeouts),
    }

    if args.preview:
        for key, value in sorted(outputs.items()):
            try:
                parsed = json.loads(value)
                print(f'{key}={json.dumps(parsed, indent=2)}')
            except (json.JSONDecodeError, TypeError):
                print(f'{key}={value}')
            print()
    else:
        for key, value in sorted(outputs.items()):
            print(f'{key}={value}')


if __name__ == '__main__':
    main()
