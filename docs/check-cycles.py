#!/usr/bin/env python3
"""Detect circular dependencies in the casehub repo dependency graph.

Reads modules-core.csv and modules-applications.csv, builds a directed
graph, and runs topological sort (Kahn's algorithm). Exits 0 if acyclic,
exits 1 listing any cycle found.
"""
import csv
import sys
from collections import defaultdict, deque
from pathlib import Path

def load_graph(csv_path):
    graph = defaultdict(set)
    nodes = set()
    if not csv_path.exists():
        return graph, nodes
    with open(csv_path) as f:
        for row in csv.reader(f):
            if not row or row[0].startswith('#'):
                continue
            name = row[0].strip()
            nodes.add(name)
            for dep in row[1:]:
                dep = dep.strip()
                if dep:
                    nodes.add(dep)
                    graph[dep].add(name)  # dep → name (dep must build first)
    return graph, nodes

def find_cycle(graph, nodes):
    in_degree = defaultdict(int)
    for node in nodes:
        in_degree.setdefault(node, 0)
    for src, targets in graph.items():
        for t in targets:
            in_degree[t] += 1

    queue = deque(n for n in nodes if in_degree[n] == 0)
    visited = 0

    while queue:
        node = queue.popleft()
        visited += 1
        for neighbor in graph.get(node, []):
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                queue.append(neighbor)

    if visited == len(nodes):
        return None  # acyclic

    # Find nodes in cycle (those with remaining in-degree > 0)
    cycle_nodes = [n for n in nodes if in_degree[n] > 0]
    return cycle_nodes

def main():
    base = Path(__file__).resolve().parent.parent
    core_csv = base / 'parent' / 'build' / 'modules-core.csv'
    apps_csv = base / 'parent' / 'build' / 'modules-applications.csv'

    graph = defaultdict(set)
    nodes = set()

    for csv_path in [core_csv, apps_csv]:
        g, n = load_graph(csv_path)
        nodes |= n
        for k, v in g.items():
            graph[k] |= v

    cycle = find_cycle(graph, nodes)
    if cycle is None:
        print(f"✓ Dependency graph is acyclic ({len(nodes)} repos)")
        return 0
    else:
        print(f"✗ Circular dependency detected among: {', '.join(sorted(cycle))}", file=sys.stderr)
        return 1

if __name__ == '__main__':
    sys.exit(main())
