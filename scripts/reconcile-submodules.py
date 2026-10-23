#!/usr/bin/env python3
"""
Reconcile .gitmodules against parent/build/modules-*.csv.

Adds submodules present in the CSVs but missing from .gitmodules.
Removes submodules in .gitmodules but not in the CSVs.
Always includes 'parent' itself.

Usage:
  python3 scripts/reconcile-submodules.py
"""

import configparser
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CORE_CSV = ROOT / 'parent' / 'build' / 'modules-core.csv'
APP_CSV = ROOT / 'parent' / 'build' / 'modules-applications.csv'
GITMODULES = ROOT / '.gitmodules'

REPO_OVERRIDE = {
    'pages': 'casehubio/casehub-pages',
    'worker': 'casehubio/casehub-worker',
    'desiredstate': 'casehubio/casehub-desiredstate',
    'ras': 'casehubio/casehub-ras',
    'ops': 'casehubio/casehub-ops',
    'flow': 'mdproctor/flow',
}


def repo_url(name):
    slug = REPO_OVERRIDE.get(name, f'casehubio/{name}')
    return f'https://github.com/{slug}.git'


def load_csv(path):
    names = []
    if not path.exists():
        return names
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        names.append(line.split(',')[0])
    return names


def load_existing():
    existing = {}
    if not GITMODULES.exists():
        return existing
    cfg = configparser.ConfigParser()
    cfg.read(GITMODULES)
    for section in cfg.sections():
        if section.startswith('submodule "'):
            name = section[len('submodule "'):-1]
            path = cfg.get(section, 'path', fallback=name)
            url = cfg.get(section, 'url', fallback='')
            existing[name] = {'path': path, 'url': url}
    return existing


def main():
    desired = ['parent'] + load_csv(CORE_CSV) + load_csv(APP_CSV)
    desired_set = set(desired)
    existing = load_existing()

    added = []
    removed = []

    for name in desired:
        if name not in existing:
            url = repo_url(name)
            subprocess.run(
                ['git', 'submodule', 'add', '--name', name, url, name],
                cwd=ROOT, check=True
            )
            added.append(name)

    for name in list(existing.keys()):
        if name not in desired_set:
            path = existing[name]['path']
            subprocess.run(['git', 'rm', '-f', path], cwd=ROOT, check=True)
            subprocess.run(
                ['git', 'config', '-f', str(GITMODULES),
                 '--remove-section', f'submodule.{name}'],
                cwd=ROOT, check=False
            )
            removed.append(name)

    if added:
        print(f'Added: {", ".join(added)}')
    if removed:
        print(f'Removed: {", ".join(removed)}')
    if not added and not removed:
        print('Submodules already in sync with CSVs.')

    return 0


if __name__ == '__main__':
    sys.exit(main())
