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

    includes = []
    for stack in stacks:
        repos_str = ' '.join(stack)
        name = ', '.join(stack)
        entry = {'name': name, 'repos': repos_str}

        extras = []
        for repo in stack:
            if repo in maven_extra:
                extras.append(maven_extra[repo])
        if extras:
            entry['maven_extra'] = ' '.join(extras)

        poms = {}
        for repo in stack:
            if repo in pom_overrides:
                poms[repo] = pom_overrides[repo]
        if poms:
            entry['pom_overrides'] = json.dumps(poms)

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
            if not level.get('slices'):
                repos.update(stack)
    return sorted(repos)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--preview', action='store_true',
                        help='Print outputs in human-readable format')
    parser.add_argument('--pipeline', default='docs/pipeline.yaml')
    args = parser.parse_args()

    pipeline = load_pipeline(args.pipeline)

    outputs = {}
    for level in pipeline.get('levels', []):
        level_id = level['id']
        key = level_id.lower()

        if level.get('slices') and level_id != 'L0':
            outputs[f'{key}-slice-matrix'] = json.dumps(build_matrix(level))

        if not level.get('slices'):
            outputs[f'{key}-matrix'] = json.dumps(build_matrix(level))

        timeout = level.get('timeout', 30)
        outputs[f'{key}-timeout'] = str(timeout)

    outputs['resume-levels'] = json.dumps(build_resume_levels(pipeline))
    outputs['all-repos'] = ' '.join(build_all_repos(pipeline))

    # Job name list for finalize needs: chain
    job_names = []
    for level in pipeline.get('levels', []):
        lid = level['id']
        num = lid[1:]
        if level.get('slices') and lid != 'L0':
            repo = level['repos'][0]
            job_names.extend([f'{repo}-base', f'{repo}-slices'])
        else:
            job_names.append(f'level{num}')
    outputs['job-names'] = json.dumps(job_names)

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
