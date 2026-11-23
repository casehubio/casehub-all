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


def assign_modules(modules, dependents, deps_of, n_slices, repo_dir):
    """Assign modules to base + N slices with balanced distribution.

    Strategy:
    1. Base = modules depended on by many others (threshold: >2 direct
       dependents within the repo). These are foundational — platform-api,
       testing, config-core, etc. Also includes their transitive deps.
    2. After removing base, find connected components in the remaining graph.
    3. Bin-pack components into N slices by total estimated time.

    Why connected components: modules in the same component depend on each
    other, so they MUST be in the same slice (slices run on separate runners
    and can't share artifacts). By packing whole components, we guarantee
    each slice is self-contained (plus base).

    The dependent-count threshold (>2) is a balance knob: lower = smaller
    base + more components, higher = larger base + fewer but bigger components.
    With platform's tree, >2 gives a reasonable split.
    """
    # Step 1: identify base — modules with many dependents (shared by many)
    # Threshold: a module with >2 direct dependents is "foundational"
    base = set()
    for mod in modules:
        if len(dependents.get(mod, set())) > 2:
            base.add(mod)

    # Spring/REST/GraphQL/MCP generators read Jandex indexes from sibling
    # target/ dirs via FILESYSTEM PATH, not Maven dependency. They MUST be
    # in the same Maven invocation as their paired Quarkus module. Force
    # all generator-using modules and their Quarkus pairs into base.
    for mod in modules:
        mod_dir = os.path.join(repo_dir, mod)
        pom_path = os.path.join(mod_dir, 'pom.xml')
        if not os.path.isfile(pom_path):
            continue
        try:
            tree = ET.parse(pom_path)
            for plugin in tree.findall('.//m:plugin', NS):
                aid = plugin.find('m:artifactId', NS)
                if aid is not None and aid.text and 'generator' in aid.text:
                    # This module uses a generator — force it and its deps
                    # into base so the Jandex index is available on the
                    # same filesystem
                    base.add(mod)
                    for dep in deps_of.get(mod, set()):
                        base.add(dep)
                    break
        except ET.ParseError:
            pass

    # Pull transitive deps of base modules into base
    changed = True
    while changed:
        changed = False
        for mod in list(base):
            for dep in deps_of.get(mod, set()):
                if dep not in base:
                    base.add(dep)
                    changed = True

    # Step 2: remaining modules — find connected components
    # Two modules are connected if one depends on the other (intra-remaining)
    remaining = [m for m in modules if m not in base]
    if not remaining:
        return base, [[] for _ in range(n_slices)], [0.0] * n_slices

    # Build adjacency for remaining modules only
    adj = defaultdict(set)
    for mod in remaining:
        for dep in deps_of.get(mod, set()):
            if dep in remaining and dep != mod:
                adj[mod].add(dep)
                adj[dep].add(mod)
        for child in dependents.get(mod, set()):
            if child in remaining and child != mod:
                adj[mod].add(child)
                adj[child].add(mod)

    # Find connected components via BFS
    visited = set()
    components = []
    for mod in remaining:
        if mod in visited:
            continue
        component = []
        queue = [mod]
        while queue:
            m = queue.pop()
            if m in visited:
                continue
            visited.add(m)
            component.append(m)
            for neighbor in adj.get(m, set()):
                if neighbor not in visited:
                    queue.append(neighbor)
        components.append(component)

    # Step 3: bin-pack components into N slices by total estimated time
    comp_times = []
    for comp in components:
        total = sum(estimate_build_time(os.path.join(repo_dir, m)) for m in comp)
        comp_times.append((comp, total))
    comp_times.sort(key=lambda x: x[1], reverse=True)

    slices = [[] for _ in range(n_slices)]
    slice_times = [0.0] * n_slices

    for comp, time in comp_times:
        lightest = min(range(n_slices), key=lambda i: slice_times[i])
        slices[lightest].extend(comp)
        slice_times[lightest] += time

    return base, slices, slice_times


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

    # 3. Assign modules: small base (shared deps) + balanced slices
    base_modules, slices, slice_times = assign_modules(
        modules, dependents, deps_of, n_slices, repo_dir)

    # 4. Build result
    result = {
        'level': level_id,
        'repo': repo_name,
        'total_modules': len(modules),
        'base': sorted(list(base_modules)),
        'base_count': len(base_modules),
        'slices': {},
    }

    total_slice_time = sum(slice_times)
    mean_time = total_slice_time / n_slices if n_slices > 0 else 0
    base_time = sum(estimate_build_time(os.path.join(repo_dir, m)) for m in base_modules)

    # Print summary
    print(f'\n  {level_id} ({repo_name}): {len(modules)} modules → '
          f'base({len(base_modules)}, est {base_time:.0f} min) + {n_slices} slices')

    for i, slice_mods in enumerate(slices):
        label = SLICE_LABELS[i]
        result['slices'][label] = sorted(slice_mods)
        print(f'    slice {label}: {len(slice_mods)} modules, '
              f'est {slice_times[i]:.1f} min')

        # Warn if imbalanced (>30% heavier than mean)
        if mean_time > 0 and slice_times[i] > mean_time * 1.3:
            print(f'    ⚠ slice {label} is {slice_times[i]/mean_time:.0%} of mean '
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
