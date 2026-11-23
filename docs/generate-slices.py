#!/usr/bin/env python3
"""
generate-slices.py — auto-assign repo modules to parallel aggregator POMs.

Reads pipeline.yaml for levels with slices: N, parses the repo's POM to
discover all modules and their intra-repo dependencies, then bin-packs
independent modules into N balanced slices.

Why this exists:
  Static aggregator POMs drift when repos add modules. Platform had 243
  modules but static slices only covered 173 — 70 modules silently untested.
  This script runs at build time so every module is always assigned.

Algorithm:
  1. Parse <module> entries from the repo's root pom.xml
  2. For each module, parse its pom.xml for io.casehub dependencies
  3. Build intra-repo dependency graph
  4. Topological sort: modules depended on by others go to "base"
  5. Remaining modules are independent — bin-pack into N slices
     balanced by estimated build time
  6. Generate aggregator-{base,slice-a,slice-b,...}.xml

Build time estimation (when no dashboard history is available):
  - Module has src/main/webui/ or package.json → 10 min (frontend)
  - Module has @QuarkusTest in test sources     → 3 min (integration)
  - Module has test sources                     → 1 min (unit tests)
  - Module has no test sources                  → 0.5 min (compile only)

Usage:
  python3 docs/generate-slices.py                    # generate for all sliceable levels
  python3 docs/generate-slices.py --dry-run           # print assignments without writing
  python3 docs/generate-slices.py --level L0          # generate for one level only
"""

import xml.etree.ElementTree as ET
import os
import sys
import json
import argparse
from collections import defaultdict
from pathlib import Path

NS = {'m': 'http://maven.apache.org/POM/4.0.0'}
SLICE_LABELS = 'abcdefghijklmnopqrstuvwxyz'


def parse_modules(repo_dir):
    """Parse all <module> entries from the repo's root pom.xml."""
    pom_path = os.path.join(repo_dir, 'pom.xml')
    if not os.path.isfile(pom_path):
        return []
    tree = ET.parse(pom_path)
    modules = []
    for mod in tree.findall('.//m:modules/m:module', NS):
        if mod.text:
            modules.append(mod.text)
    return modules


def get_artifact_id(module_dir):
    """Read the artifactId from a module's pom.xml."""
    pom_path = os.path.join(module_dir, 'pom.xml')
    if not os.path.isfile(pom_path):
        return None
    try:
        tree = ET.parse(pom_path)
        aid = tree.find('m:artifactId', NS)
        return aid.text if aid is not None else None
    except ET.ParseError:
        return None


def get_casehub_deps(module_dir):
    """Get io.casehub dependency artifactIds from a module's pom.xml."""
    pom_path = os.path.join(module_dir, 'pom.xml')
    if not os.path.isfile(pom_path):
        return set()
    try:
        tree = ET.parse(pom_path)
        deps = set()
        for dep in tree.findall('.//m:dependencies/m:dependency', NS):
            g = dep.find('m:groupId', NS)
            a = dep.find('m:artifactId', NS)
            if g is not None and a is not None and g.text == 'io.casehub':
                deps.add(a.text)
        return deps
    except ET.ParseError:
        return set()


def estimate_build_time(module_dir):
    """Estimate build time in minutes based on module characteristics.

    Why these heuristics: without dashboard history (first run, new module),
    we need a reasonable estimate. Frontend modules with node_modules take
    ~10 min. Quarkus integration tests boot the container (~3 min).
    Plain unit tests are fast (~1 min). Compile-only is trivial (~0.5 min).
    """
    # Check for frontend (package.json or webui directory)
    if (os.path.isfile(os.path.join(module_dir, 'package.json')) or
            os.path.isdir(os.path.join(module_dir, 'src', 'main', 'webui'))):
        return 10.0

    test_dir = os.path.join(module_dir, 'src', 'test')
    if not os.path.isdir(test_dir):
        return 0.5  # compile only, no tests

    # Check for @QuarkusTest in test sources — indicates integration test
    # with Quarkus boot overhead
    has_quarkus_test = False
    for root, dirs, files in os.walk(test_dir):
        for f in files:
            if f.endswith('.java'):
                try:
                    content = open(os.path.join(root, f), 'r').read()
                    if '@QuarkusTest' in content or '@QuarkusIntegrationTest' in content:
                        has_quarkus_test = True
                        break
                except (IOError, UnicodeDecodeError):
                    pass
        if has_quarkus_test:
            break

    return 3.0 if has_quarkus_test else 1.0


def build_dependency_graph(repo_dir, modules):
    """Build intra-repo dependency graph.

    Returns:
      artifact_to_module: maps artifactId → module directory name
      dependents: maps module → set of modules that depend on it
      deps_of: maps module → set of modules it depends on
    """
    artifact_to_module = {}
    module_artifacts = {}

    for mod in modules:
        mod_dir = os.path.join(repo_dir, mod)
        aid = get_artifact_id(mod_dir)
        if aid:
            artifact_to_module[aid] = mod
            module_artifacts[mod] = aid

    # Build dependency edges (only intra-repo deps)
    dependents = defaultdict(set)  # module → who depends on it
    deps_of = defaultdict(set)     # module → what it depends on
    repo_artifact_ids = set(artifact_to_module.keys())

    for mod in modules:
        mod_dir = os.path.join(repo_dir, mod)
        deps = get_casehub_deps(mod_dir)
        for dep_aid in deps:
            if dep_aid in artifact_to_module:
                dep_mod = artifact_to_module[dep_aid]
                if dep_mod != mod:  # skip self-deps
                    dependents[dep_mod].add(mod)
                    deps_of[mod].add(dep_mod)

    return artifact_to_module, dependents, deps_of


def find_base_modules(modules, dependents, deps_of):
    """Find modules that must build first (the "base" set).

    A module is "base" if at least one other module depends on it AND
    it has dependents in multiple future slices. In practice: anything
    with dependents goes to base, to ensure slices can build independently.
    """
    base = set()
    for mod in modules:
        if dependents.get(mod):
            base.add(mod)
            # Also pull in transitive deps of base modules — if A is base
            # and A depends on B, B must also be in base
            queue = list(deps_of.get(mod, set()))
            while queue:
                dep = queue.pop()
                if dep not in base:
                    base.add(dep)
                    queue.extend(deps_of.get(dep, set()))
    return base


def topo_sort(modules, deps_of):
    """Topological sort of modules respecting dependencies."""
    visited = set()
    result = []

    def visit(mod):
        if mod in visited:
            return
        visited.add(mod)
        for dep in deps_of.get(mod, set()):
            if dep in modules:
                visit(dep)
        result.append(mod)

    for mod in sorted(modules):
        visit(mod)
    return result


def bin_pack(modules_with_times, n_slices):
    """Bin-pack modules into N slices balanced by estimated build time.

    Uses greedy "longest processing time first" — sort by time descending,
    assign each to the lightest slice. This gives a good balance without
    needing an optimal (NP-hard) solution.
    """
    if n_slices <= 0:
        return [list(modules_with_times)]

    # Sort by time descending (heaviest first for better balance)
    sorted_mods = sorted(modules_with_times, key=lambda x: x[1], reverse=True)

    slices = [[] for _ in range(n_slices)]
    slice_times = [0.0] * n_slices

    for mod, time in sorted_mods:
        # Assign to lightest slice
        lightest = min(range(n_slices), key=lambda i: slice_times[i])
        slices[lightest].append(mod)
        slice_times[lightest] += time

    return slices


def generate_aggregator_pom(artifact_id, name, modules, repo_dir):
    """Generate an aggregator POM XML string."""
    module_entries = []
    for mod in topo_sort(set(modules), defaultdict(set)):
        module_entries.append(f'    <module>../../{repo_dir}/{mod}</module>')

    return f'''<?xml version="1.0" encoding="UTF-8"?>
<!-- AUTO-GENERATED by generate-slices.py — do not edit manually.
     Modules are assigned dynamically from the repo's pom.xml at build time.
     To change the slice count, edit docs/pipeline.yaml (slices: N). -->
<project xmlns="http://maven.apache.org/POM/4.0.0"
         xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
         xsi:schemaLocation="http://maven.apache.org/POM/4.0.0 https://maven.apache.org/xsd/maven-4.0.0.xsd">
  <modelVersion>4.0.0</modelVersion>

  <groupId>io.casehub</groupId>
  <artifactId>{artifact_id}</artifactId>
  <version>0.4-SNAPSHOT</version>
  <packaging>pom</packaging>

  <name>{name}</name>

  <modules>
{chr(10).join(module_entries)}
  </modules>
</project>
'''


def process_level(level, repo_root, output_dir, dry_run=False):
    """Process a single sliceable level."""
    level_id = level['id']
    repos = level.get('repos', [])
    n_slices = level.get('slices', 0)

    if not repos or len(repos) != 1 or n_slices <= 0:
        return None

    repo_name = repos[0]
    repo_dir = os.path.join(repo_root, repo_name)

    if not os.path.isdir(repo_dir):
        print(f'  SKIP {level_id}: repo dir {repo_dir} not found')
        return None

    # 1. Parse all modules
    modules = parse_modules(repo_dir)
    if not modules:
        print(f'  SKIP {level_id}: no modules in {repo_dir}/pom.xml')
        return None

    # 2. Build dependency graph
    artifact_to_module, dependents, deps_of = build_dependency_graph(repo_dir, modules)

    # 3. Find base modules (depended on by others)
    base_modules = find_base_modules(modules, dependents, deps_of)
    slice_modules = [m for m in modules if m not in base_modules]

    # 4. Estimate build times for slice modules
    modules_with_times = []
    for mod in slice_modules:
        mod_dir = os.path.join(repo_dir, mod)
        time_est = estimate_build_time(mod_dir)
        modules_with_times.append((mod, time_est))

    # 5. Bin-pack into N slices
    slices = bin_pack(modules_with_times, n_slices)

    # 6. Build result
    result = {
        'level': level_id,
        'repo': repo_name,
        'total_modules': len(modules),
        'base': sorted(list(base_modules)),
        'base_count': len(base_modules),
        'slices': {},
    }

    total_time = sum(t for _, t in modules_with_times)
    mean_time = total_time / n_slices if n_slices > 0 else 0

    # Print summary
    print(f'\n  {level_id} ({repo_name}): {len(modules)} modules → '
          f'base({len(base_modules)}) + {n_slices} slices')
    print(f'    base: {len(base_modules)} modules')

    for i, slice_mods in enumerate(slices):
        label = SLICE_LABELS[i]
        slice_time = sum(t for _, t in modules_with_times if _  in [m for m in slice_mods])
        slice_time = sum(estimate_build_time(os.path.join(repo_dir, m)) for m in slice_mods)
        result['slices'][label] = sorted(slice_mods)
        print(f'    slice {label}: {len(slice_mods)} modules, '
              f'est {slice_time:.1f} min')

        # Warn if imbalanced (>30% heavier than mean)
        if mean_time > 0 and slice_time > mean_time * 1.3:
            print(f'    ⚠ slice {label} is {slice_time/mean_time:.0%} of mean '
                  f'— consider increasing slice count')

    if dry_run:
        return result

    # 7. Generate aggregator POMs
    slice_dir = os.path.join(output_dir, f'{repo_name}-slices')
    os.makedirs(slice_dir, exist_ok=True)

    # Base POM
    base_sorted = topo_sort(base_modules, deps_of)
    base_pom = generate_aggregator_pom(
        f'casehub-{repo_name}-aggregator-base',
        f'CaseHub :: {repo_name.title()} :: Base',
        base_sorted, repo_name)
    with open(os.path.join(slice_dir, 'aggregator-base.xml'), 'w') as f:
        f.write(base_pom)

    # Slice POMs
    for i, slice_mods in enumerate(slices):
        label = SLICE_LABELS[i]
        slice_pom = generate_aggregator_pom(
            f'casehub-{repo_name}-aggregator-slice-{label}',
            f'CaseHub :: {repo_name.title()} :: Slice {label.upper()}',
            slice_mods, repo_name)
        with open(os.path.join(slice_dir, f'aggregator-slice-{label}.xml'), 'w') as f:
            f.write(slice_pom)

    print(f'    → wrote {1 + len(slices)} POMs to {slice_dir}/')
    return result


def main():
    parser = argparse.ArgumentParser(description='Generate dynamic slice aggregator POMs')
    parser.add_argument('--dry-run', action='store_true',
                        help='Print assignments without writing POMs')
    parser.add_argument('--level', type=str, default=None,
                        help='Process only this level (e.g. L0)')
    parser.add_argument('--pipeline', type=str, default='docs/pipeline.yaml',
                        help='Path to pipeline.yaml')
    parser.add_argument('--output', type=str, default='build',
                        help='Output directory for aggregator POMs')
    args = parser.parse_args()

    # Import yaml — available on GitHub Actions runners (pre-installed)
    try:
        import yaml
    except ImportError:
        print('ERROR: PyYAML not installed. Run: pip install pyyaml', file=sys.stderr)
        sys.exit(1)

    # Parse pipeline.yaml
    if not os.path.isfile(args.pipeline):
        print(f'ERROR: {args.pipeline} not found', file=sys.stderr)
        sys.exit(1)

    with open(args.pipeline) as f:
        pipeline = yaml.safe_load(f)

    # Find repo root (parent of docs/)
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(args.pipeline)))
    if not repo_root:
        repo_root = '.'

    print(f'=== Dynamic slice assignment ===')
    print(f'  pipeline: {args.pipeline}')
    print(f'  repo root: {repo_root}')
    if args.dry_run:
        print(f'  mode: DRY RUN (no files written)')

    results = []
    for level in pipeline.get('levels', []):
        if args.level and level['id'] != args.level:
            continue
        if level.get('slices'):
            result = process_level(level, repo_root, args.output, dry_run=args.dry_run)
            if result:
                results.append(result)

    if not results:
        print('\n  No sliceable levels found (need repos: [name] + slices: N)')
        return

    # Write JSON summary
    if not args.dry_run:
        summary_path = os.path.join(args.output, 'slice-assignments.json')
        with open(summary_path, 'w') as f:
            json.dump(results, f, indent=2)
        print(f'\n  → summary: {summary_path}')

    print(f'\n=== Done: {len(results)} levels processed ===')


if __name__ == '__main__':
    main()
