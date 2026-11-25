#!/usr/bin/env python3
"""
check-pipeline-consistency.py — validate that pipeline.yaml, CSVs, submodules,
and mvn-all.yml all agree on which repos exist and how they're organized.

Catches drift like a repo added to modules-applications.csv but missing from
pipeline.yaml (e.g. distributions), or a matrix definition in mvn-all.yml
that doesn't match pipeline.yaml stacks.

Exit 0 if consistent, exit 1 with specific diffs if not.

Usage:
  python3 docs/check-pipeline-consistency.py                    # from casehub-all root
  python3 docs/check-pipeline-consistency.py --fix-hint         # suggest fixes
  python3 docs/check-pipeline-consistency.py --parent ../parent # custom parent path
"""

import os
import re
import sys
import argparse
import configparser
from pathlib import Path


def parse_csv_repos(csv_path):
    repos = set()
    with open(csv_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            name = line.split(',')[0].strip()
            if name:
                repos.add(name)
    return repos


def parse_pipeline_yaml(pipeline_path):
    try:
        import yaml
    except ImportError:
        print('ERROR: PyYAML not installed. Run: pip install pyyaml', file=sys.stderr)
        sys.exit(1)

    with open(pipeline_path) as f:
        pipeline = yaml.safe_load(f)

    repos = set()
    levels = []
    resume_levels = set()

    for level in pipeline.get('levels', []):
        level_id = level['id']
        level_repos = set()

        if level.get('repos'):
            level_repos.update(level['repos'])
        for stack in level.get('stacks', []):
            level_repos.update(stack)

        # Slice labels (like "Platform A") aren't repo names — skip them
        # when repos: is set, the stacks contain display names not dirs
        if level.get('repos') and level.get('slices'):
            level_repos = set(level['repos'])

        repos.update(level_repos)
        levels.append({
            'id': level_id,
            'repos': level_repos,
            'stacks': level.get('stacks', []),
            'slices': level.get('slices'),
            'resume': level.get('resume', False),
            'raw_repos': level.get('repos'),
        })

        if level.get('resume'):
            resume_levels.add(level_id)

    return repos, levels, resume_levels


def parse_gitmodules(repo_root):
    gitmodules_path = os.path.join(repo_root, '.gitmodules')
    if not os.path.isfile(gitmodules_path):
        return set()

    config = configparser.ConfigParser()
    config.read(gitmodules_path)

    submodules = set()
    for section in config.sections():
        if section.startswith('submodule '):
            path = config.get(section, 'path', fallback=None)
            if path:
                submodules.add(path)
    return submodules


def parse_workflow_resume_levels(workflow_path):
    resume_levels = set()
    with open(workflow_path) as f:
        content = f.read()

    # Find resume_from options in workflow_dispatch inputs
    in_resume = False
    in_options = False
    for line in content.split('\n'):
        stripped = line.strip()
        if 'resume_from:' in stripped:
            in_resume = True
            continue
        if in_resume and 'options:' in stripped:
            in_options = True
            continue
        if in_options:
            if stripped.startswith('- '):
                val = stripped[2:].strip().strip("'\"")
                if val:
                    resume_levels.add(val)
            elif not stripped.startswith('-') and not stripped.startswith('#'):
                break
        if in_resume and not in_options and 'type:' in stripped:
            continue
        if in_resume and not in_options and stripped and not stripped.startswith('#') and 'description:' not in stripped and 'default:' not in stripped and 'type:' not in stripped and 'options:' not in stripped:
            in_resume = False

    return resume_levels


def parse_workflow_matrices(workflow_path):
    """Extract matrix definitions per level from mvn-all.yml."""
    matrices = {}
    with open(workflow_path) as f:
        content = f.read()

    # Find level job names and their matrix includes
    # Pattern: level job has name like "L1: ${{ matrix.name }}" or "L7: drafthouse"
    current_level = None
    current_repos = []
    in_matrix = False
    in_include = False

    for line in content.split('\n'):
        # Match job name pattern to identify level
        name_match = re.match(r'\s+name:\s*"(L\d+):.*"', line)
        if name_match:
            if current_level and current_repos:
                matrices[current_level] = set(current_repos)
            current_level = name_match.group(1)
            current_repos = []
            in_matrix = False
            in_include = False
            continue

        # Match matrix repos
        if current_level:
            repos_match = re.match(r'\s+repos:\s*["\']?(.+?)["\']?\s*$', line)
            if repos_match and not line.strip().startswith('#'):
                current_repos.extend(repos_match.group(1).split())

            repo_match = re.match(r'\s+repo:\s*\[(.+)\]', line)
            if repo_match:
                for r in repo_match.group(1).split(','):
                    r = r.strip().strip("'\"")
                    if r:
                        current_repos.append(r)

            # Single-job levels: "for repo in scaffold fsitrading"
            for_match = re.search(r'for repo in (.+?);', line)
            if for_match and 'matrix' not in for_match.group(1) and 'REPOS' not in for_match.group(1):
                for r in for_match.group(1).strip().split():
                    if r not in ('${{', '}}') and not r.startswith('$'):
                        current_repos.append(r)

            # Hardcoded submodule init: "git submodule update --init --depth=1 parent platform drafthouse"
            init_match = re.search(r'git submodule update --init.*parent platform (.+)', line)
            if init_match and 'matrix' not in init_match.group(1):
                for r in init_match.group(1).strip().split():
                    if r not in current_repos and not r.startswith('$'):
                        current_repos.append(r)

    if current_level and current_repos:
        matrices[current_level] = set(current_repos)

    return matrices


def main():
    parser = argparse.ArgumentParser(description='Check pipeline consistency')
    parser.add_argument('--parent', type=str, default=None,
                        help='Path to parent repo (default: ../parent or parent/)')
    parser.add_argument('--fix-hint', action='store_true',
                        help='Suggest fixes for inconsistencies')
    args = parser.parse_args()

    # Find repo root
    script_dir = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.dirname(script_dir)

    # Find parent repo
    parent_dir = args.parent
    if not parent_dir:
        if os.path.isdir(os.path.join(repo_root, 'parent', 'build')):
            parent_dir = os.path.join(repo_root, 'parent')
        elif os.path.isdir(os.path.join(repo_root, '..', 'parent', 'build')):
            parent_dir = os.path.join(repo_root, '..', 'parent')
        else:
            print('ERROR: Cannot find parent repo. Use --parent.', file=sys.stderr)
            sys.exit(1)

    pipeline_path = os.path.join(repo_root, 'docs', 'pipeline.yaml')
    workflow_path = os.path.join(repo_root, '.github', 'workflows', 'mvn-all.yml')
    core_csv = os.path.join(parent_dir, 'build', 'modules-core.csv')
    apps_csv = os.path.join(parent_dir, 'build', 'modules-applications.csv')

    errors = []
    warnings = []

    # 1. Parse all sources
    print('=== Pipeline consistency check ===\n')

    csv_repos = set()
    if os.path.isfile(core_csv):
        csv_repos |= parse_csv_repos(core_csv)
        print(f'  modules-core.csv: {len(parse_csv_repos(core_csv))} repos')
    else:
        errors.append(f'Missing: {core_csv}')

    if os.path.isfile(apps_csv):
        csv_repos |= parse_csv_repos(apps_csv)
        print(f'  modules-applications.csv: {len(parse_csv_repos(apps_csv))} repos')
    else:
        errors.append(f'Missing: {apps_csv}')

    pipeline_repos, levels, resume_levels = parse_pipeline_yaml(pipeline_path)
    print(f'  pipeline.yaml: {len(pipeline_repos)} repos across {len(levels)} levels')

    submodules = parse_gitmodules(repo_root)
    print(f'  .gitmodules: {len(submodules)} submodules')

    workflow_resume = parse_workflow_resume_levels(workflow_path)
    workflow_matrices = parse_workflow_matrices(workflow_path)
    print(f'  mvn-all.yml: {len(workflow_matrices)} level matrices, resume levels: {sorted(workflow_resume)}')

    # "parent" is special — it's the BOM, built in prepare, not a pipeline level
    csv_repos_no_parent = csv_repos - {'parent'}

    print()

    # 2. Cross-validate

    # CSV repos vs pipeline.yaml
    in_csv_not_pipeline = csv_repos_no_parent - pipeline_repos
    in_pipeline_not_csv = pipeline_repos - csv_repos

    if in_csv_not_pipeline:
        for r in sorted(in_csv_not_pipeline):
            errors.append(f'In CSVs but not pipeline.yaml: {r}')
            if args.fix_hint:
                errors[-1] += f' → add {r} to a level in docs/pipeline.yaml'

    if in_pipeline_not_csv:
        for r in sorted(in_pipeline_not_csv):
            errors.append(f'In pipeline.yaml but not CSVs: {r}')
            if args.fix_hint:
                errors[-1] += f' → add {r} to parent/build/modules-*.csv'

    # Pipeline repos vs submodules
    in_pipeline_not_submodule = pipeline_repos - submodules
    if in_pipeline_not_submodule:
        for r in sorted(in_pipeline_not_submodule):
            errors.append(f'In pipeline.yaml but not a submodule: {r}')

    # Submodules that are in CSVs but not pipeline (broader check)
    csv_submodules = csv_repos_no_parent & submodules
    not_in_pipeline = csv_submodules - pipeline_repos
    if not_in_pipeline:
        for r in sorted(not_in_pipeline):
            if r not in [e.split(': ')[-1].split(' →')[0] for e in errors]:
                errors.append(f'Submodule in CSVs but missing from pipeline.yaml: {r}')

    # Resume levels: pipeline.yaml vs mvn-all.yml
    if resume_levels != workflow_resume:
        only_pipeline = resume_levels - workflow_resume
        only_workflow = workflow_resume - resume_levels
        if only_pipeline:
            errors.append(f'Resume levels in pipeline.yaml but not mvn-all.yml: {sorted(only_pipeline)}')
        if only_workflow:
            errors.append(f'Resume levels in mvn-all.yml but not pipeline.yaml: {sorted(only_workflow)}')

    # Pipeline stacks vs workflow matrices (repo assignments)
    for level in levels:
        level_id = level['id']
        if level.get('slices'):
            continue  # sliced levels don't have matrix repos to compare

        # Collect all repos from pipeline stacks
        pipeline_level_repos = set()
        for stack in level['stacks']:
            pipeline_level_repos.update(stack)

        # Compare with workflow matrix
        if level_id in workflow_matrices:
            workflow_level_repos = workflow_matrices[level_id]
            if pipeline_level_repos != workflow_level_repos:
                only_pipeline = pipeline_level_repos - workflow_level_repos
                only_workflow = workflow_level_repos - pipeline_level_repos
                if only_pipeline:
                    errors.append(f'{level_id}: in pipeline.yaml stacks but not mvn-all.yml matrix: {sorted(only_pipeline)}')
                if only_workflow:
                    warnings.append(f'{level_id}: in mvn-all.yml matrix but not pipeline.yaml stacks: {sorted(only_workflow)}')
        else:
            if pipeline_level_repos:
                warnings.append(f'{level_id}: has repos in pipeline.yaml but no matrix found in mvn-all.yml')

    # 3. Report
    print()
    if warnings:
        print(f'WARNINGS ({len(warnings)}):')
        for w in warnings:
            print(f'  ⚠ {w}')
        print()

    if errors:
        print(f'ERRORS ({len(errors)}):')
        for e in errors:
            print(f'  ✗ {e}')
        print(f'\n=== FAILED: {len(errors)} inconsistencies ===')
        return 1
    else:
        print('=== PASSED: all sources consistent ===')
        return 0


if __name__ == '__main__':
    sys.exit(main())
