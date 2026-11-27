#!/usr/bin/env python3
"""
Tests for auto-retry self-healing resume logic.

Run: python3 docs/test-auto-retry.py
"""

import json
import os
import sys
import yaml

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PIPELINE_PATH = os.path.join(SCRIPT_DIR, 'pipeline.yaml')

failures = []

def test(name):
    def decorator(fn):
        try:
            fn()
            print(f'  PASS  {name}')
        except AssertionError as e:
            failures.append(f'{name}: {e}')
            print(f'  FAIL  {name}: {e}')
        except Exception as e:
            failures.append(f'{name}: {e}')
            print(f'  FAIL  {name}: {type(e).__name__}: {e}')
    return decorator


with open(PIPELINE_PATH) as f:
    pipeline = yaml.safe_load(f)


# Import the resume level finder (will be created)
sys.path.insert(0, SCRIPT_DIR)
try:
    from heal_resume import find_heal_level, classify_error, map_class_to_repo
except ImportError:
    # Module doesn't exist yet — all tests should fail (TDD red phase)
    def find_heal_level(*a, **kw): raise NotImplementedError('heal_resume.py not created yet')
    def classify_error(*a, **kw): raise NotImplementedError('heal_resume.py not created yet')
    def map_class_to_repo(*a, **kw): raise NotImplementedError('heal_resume.py not created yet')


# ── Error classification tests ────────────────────────────────────

@test('classify: ClassNotFoundException → upstream_artifact')
def _():
    error = 'java.lang.ClassNotFoundException: io.casehub.pages.push.PushConnectionListener'
    result = classify_error(error)
    assert result['type'] == 'upstream_artifact', f'expected upstream_artifact, got {result["type"]}'
    assert result['class'] == 'io.casehub.pages.push.PushConnectionListener'


@test('classify: compilation error → code_error')
def _():
    error = 'cannot find symbol: method doSomething()'
    result = classify_error(error)
    assert result['type'] == 'code_error'


@test('classify: 429 rate limit → infrastructure')
def _():
    error = 'status code: 429, reason phrase: Too Many Requests'
    result = classify_error(error)
    assert result['type'] == 'infrastructure'


@test('classify: corrupt JAR → infrastructure')
def _():
    error = 'invalid CEN header (bad signature)'
    result = classify_error(error)
    assert result['type'] == 'infrastructure'


# ── Class-to-repo mapping tests ───────────────────────────────────

@test('map: io.casehub.pages.push.PushConnectionListener → pages')
def _():
    repo = map_class_to_repo('io.casehub.pages.push.PushConnectionListener')
    assert repo == 'pages', f'expected pages, got {repo}'


@test('map: io.casehub.engine.api.Worker → engine')
def _():
    repo = map_class_to_repo('io.casehub.engine.api.Worker')
    assert repo == 'engine', f'expected engine, got {repo}'


@test('map: io.casehub.neocortex.cognition.memory.RetentionConfig → neocortex')
def _():
    repo = map_class_to_repo('io.casehub.neocortex.cognition.memory.RetentionConfig')
    assert repo == 'neocortex', f'expected neocortex, got {repo}'


@test('map: io.casehub.platform.api.SomeClass → platform')
def _():
    repo = map_class_to_repo('io.casehub.platform.api.SomeClass')
    assert repo == 'platform', f'expected platform, got {repo}'


@test('map: unknown package → None')
def _():
    repo = map_class_to_repo('com.example.SomeClass')
    assert repo is None, f'expected None, got {repo}'


# ── Heal level tests ─────────────────────────────────────────────

@test('heal: ClassNotFoundException pages at L6 → resume from L1')
def _():
    error = 'java.lang.ClassNotFoundException: io.casehub.pages.push.PushConnectionListener'
    failed_level = 'L6'
    heal_level = find_heal_level(error, failed_level, pipeline)
    assert heal_level == 'L1', f'expected L1, got {heal_level}'


@test('heal: ClassNotFoundException engine at L5 → resume from L3')
def _():
    error = 'java.lang.ClassNotFoundException: io.casehub.engine.api.SomeClass'
    failed_level = 'L5'
    heal_level = find_heal_level(error, failed_level, pipeline)
    assert heal_level == 'L3', f'expected L3, got {heal_level}'


@test('heal: code error at L6 → resume from L6 (no upstream fix)')
def _():
    error = 'cannot find symbol: method doSomething()'
    failed_level = 'L6'
    heal_level = find_heal_level(error, failed_level, pipeline)
    assert heal_level == 'L6', f'expected L6, got {heal_level}'


@test('heal: 429 at L4 → resume from L4 (retry in place)')
def _():
    error = 'status code: 429, reason phrase: Too Many Requests'
    failed_level = 'L4'
    heal_level = find_heal_level(error, failed_level, pipeline)
    assert heal_level == 'L4', f'expected L4, got {heal_level}'


@test('heal: upstream class at same level → resume from that level')
def _():
    error = 'java.lang.ClassNotFoundException: io.casehub.platform.api.SomeClass'
    failed_level = 'L4'
    heal_level = find_heal_level(error, failed_level, pipeline)
    assert heal_level == 'L0', f'expected L0, got {heal_level}'


# ── Summary ───────────────────────────────────────────────────────

print()
if failures:
    print(f'FAILED: {len(failures)} test(s)')
    for f in failures:
        print(f'  - {f}')
    sys.exit(1)
else:
    print('All tests passed')
