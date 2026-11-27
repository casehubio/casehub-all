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
import xml.etree.ElementTree as ET

POM_NS = {'m': 'http://maven.apache.org/POM/4.0.0'}
SLICE_LABELS = 'abcdefghijklmnopqrstuvwxyz'


def parse_modules(repo_dir):
    """Parse <module> entries from a repo's root pom.xml."""
    pom_path = os.path.join(repo_dir, 'pom.xml')
    if not os.path.isfile(pom_path):
        return []
    tree = ET.parse(pom_path)
    modules = []
    for mod in tree.findall('.//m:modules/m:module', POM_NS):
        if mod.text:
            modules.append(mod.text)
    if not modules:
        for mod in tree.findall('.//modules/module'):
            if mod.text:
                modules.append(mod.text)
    return modules


def has_tests(repo_dir, module):
    """Check if a module has test sources."""
    return os.path.isdir(os.path.join(repo_dir, module, 'src', 'test'))


def estimate_test_time(repo_dir, module):
    """Estimate test execution time in minutes.

    QuarkusTest/QuarkusIntegrationTest → 3 min (CDI boot overhead).
    Regular unit tests → 1 min.
    """
    test_dir = os.path.join(repo_dir, module, 'src', 'test')
    for root, dirs, files in os.walk(test_dir):
        for f in files:
            if f.endswith('.java'):
                try:
                    content = open(os.path.join(root, f), 'r').read()
                    if '@QuarkusTest' in content or '@QuarkusIntegrationTest' in content:
                        return 3.0
                except (IOError, UnicodeDecodeError):
                    pass
    return 1.0


def auto_distribute_test_slices(repo_dir, repo_name, n_slices, pin=None):
    """Discover test modules and bin-pack into N balanced test slices.

    Returns list of dicts: [{'name': 'repo-tests-a', 'modules': 'mod1,mod2'}, ...]
    """
    all_modules = parse_modules(repo_dir)
    test_modules = [m for m in all_modules if has_tests(repo_dir, m)]

    if not test_modules:
        return []

    pinned_slices = {}
    remaining = list(test_modules)
    if pin:
        for p in pin:
            idx = p['slice']
            pinned_mods = [m for m in p['modules'] if m in test_modules]
            if pinned_mods:
                pinned_slices[idx] = pinned_mods
                remaining = [m for m in remaining if m not in pinned_mods]

    n_auto = n_slices - len(pinned_slices)
    if n_auto <= 0:
        n_auto = 1

    times = [(m, estimate_test_time(repo_dir, m)) for m in remaining]
    times.sort(key=lambda x: x[1], reverse=True)

    auto_slices = [[] for _ in range(n_auto)]
    auto_times = [0.0] * n_auto
    for mod, t in times:
        lightest = min(range(n_auto), key=lambda i: auto_times[i])
        auto_slices[lightest].append(mod)
        auto_times[lightest] += t

    result = []
    label_idx = 0
    for i in range(n_slices):
        if i in pinned_slices:
            mods = pinned_slices[i]
        elif auto_slices:
            mods = auto_slices.pop(0)
        else:
            continue
        if mods:
            label = SLICE_LABELS[label_idx]
            result.append({
                'name': f'{repo_name}-tests-{label}',
                'modules': ','.join(mods),
            })
            label_idx += 1

    return result


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


def collect_test_slices(pipeline, test_mode, repo_root=None):
    """Collect test slices that should be injected into the next level.

    Supports three formats for test_slices:
      - list of dicts (manual): [{name, modules}, ...] — backward compat
      - integer (auto): N — auto-distribute into N slices
      - object (auto with pinning): {count: N, pin: [{slice, modules}]}

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
            if not isinstance(stack, dict) or 'test_slices' not in stack:
                continue

            repos = stack['repos']
            pom_overrides = level.get('pom', {})
            pom = pom_overrides.get(repos[0], repos[0] + '/pom.xml')
            ts_config = stack['test_slices']

            if isinstance(ts_config, list):
                slices = ts_config
            elif isinstance(ts_config, int):
                if repo_root:
                    repo_dir = os.path.join(repo_root, repos[0])
                    slices = auto_distribute_test_slices(repo_dir, repos[0], ts_config)
                else:
                    slices = []
            elif isinstance(ts_config, dict):
                count = ts_config.get('count', 2)
                pin = ts_config.get('pin')
                if repo_root:
                    repo_dir = os.path.join(repo_root, repos[0])
                    slices = auto_distribute_test_slices(repo_dir, repos[0], count, pin=pin)
                else:
                    slices = []
            else:
                continue

            for ts in slices:
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
    parser.add_argument('--repo-root', default=None,
                        help='Root directory containing repo subdirectories (for auto test_slices)')
    args = parser.parse_args()

    pipeline = load_pipeline(args.pipeline)
    resume_level = args.resume_from.lower() if args.resume_from else ''
    resume_repos = set(r.strip() for r in args.resume_repos.split(',') if r.strip()) if args.resume_repos else set()

    repo_root = args.repo_root
    if not repo_root:
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(args.pipeline)))

    test_slices = collect_test_slices(pipeline, args.test_mode, repo_root=repo_root)

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
