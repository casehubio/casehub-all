#!/usr/bin/env python3
"""
Regression tests for CI infrastructure scripts.

Run: python3 docs/test-ci-scripts.py
Runs in CI as part of the prepare job's consistency checks.
"""

import json
import os
import subprocess
import sys
import tempfile
import yaml

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PIPELINE_PATH = os.path.join(SCRIPT_DIR, 'pipeline.yaml')
WORKFLOW_PATH = os.path.join(SCRIPT_DIR, '..', '.github', 'workflows', 'mvn-all.yml')
INIT_SCRIPT = os.path.join(SCRIPT_DIR, 'init-dashboard-full.py')

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


# ── Pipeline topology tests ───────────────────────────────────────

with open(PIPELINE_PATH) as f:
    pipeline = yaml.safe_load(f)


@test('pipeline: all levels have id')
def _():
    for i, level in enumerate(pipeline['levels']):
        assert 'id' in level, f'level {i} missing id'


@test('pipeline: no duplicate phase names')
def _():
    names = []
    for level in pipeline['levels']:
        overrides = level.get('names', {})
        for stack in level.get('stacks', []):
            if isinstance(stack, dict):
                names.append(stack.get('name', stack['repos'][0]))
            elif isinstance(stack, str):
                names.append(stack)
            else:
                for repo in stack:
                    names.append(overrides.get(repo, repo))
    dupes = [n for n in names if names.count(n) > 1]
    assert not dupes, f'duplicate phase names: {set(dupes)}'


@test('pipeline: object stacks have repos and name')
def _():
    for level in pipeline['levels']:
        for stack in level.get('stacks', []):
            if isinstance(stack, dict):
                assert 'repos' in stack, f'object stack in {level["id"]} missing repos'
                assert 'name' in stack, f'object stack in {level["id"]} missing name'


# ── Init script tests ─────────────────────────────────────────────

def run_init(resume_from='', old_progress=None):
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump(old_progress or {}, f)
        old_path = f.name
    try:
        result = subprocess.run(
            [sys.executable, INIT_SCRIPT,
             '--run-id', '999', '--run-url', 'test', '--commit', 'abc',
             '--resume-from', resume_from, '--pipeline', PIPELINE_PATH,
             '--old-progress', old_path],
            capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, f'exit {result.returncode}: {result.stderr}'
        return json.loads(result.stdout)
    finally:
        os.unlink(old_path)


@test('init: produces correct phase count')
def _():
    data = run_init()
    phases = data['current']['phases']
    expected_names = []
    for level in pipeline['levels']:
        overrides = level.get('names', {})
        for stack in level.get('stacks', []):
            if isinstance(stack, dict):
                expected_names.append(stack.get('name', stack['repos'][0]))
            elif isinstance(stack, str):
                expected_names.append(stack)
            else:
                for repo in stack:
                    expected_names.append(overrides.get(repo, repo))
    assert len(phases) == len(expected_names), f'got {len(phases)}, expected {len(expected_names)}'


@test('init: name overrides applied')
def _():
    data = run_init()
    names = [p['name'] for p in data['current']['phases']]
    for level in pipeline['levels']:
        overrides = level.get('names', {})
        for repo, display in overrides.items():
            assert display in names, f'name override {repo}→{display} not in phases'


@test('init: object stacks produce correct names')
def _():
    data = run_init()
    names = [p['name'] for p in data['current']['phases']]
    for level in pipeline['levels']:
        for stack in level.get('stacks', []):
            if isinstance(stack, dict):
                expected = stack.get('name', stack['repos'][0])
                assert expected in names, f'object stack name {expected} not in phases'


@test('init: no garbage names (repos/name/maven_extra)')
def _():
    data = run_init()
    names = [p['name'] for p in data['current']['phases']]
    for bad in ['repos', 'name', 'maven_extra']:
        assert bad not in names, f'garbage phase name "{bad}" found'


@test('init: resume sets cached phases before resume level')
def _():
    green_phases = [{'name': 'platform', 'status': 'success', 'duration_s': 120, 'tests_run': 500}]
    old = {'current': {'run_id': 100, 'phases': green_phases, 'started': '2026-01-01T00:00:00Z'}, 'history': []}
    data = run_init(resume_from='L1', old_progress=old)
    platform = next(p for p in data['current']['phases'] if p['name'] == 'platform')
    assert platform.get('cached') is True, 'platform should be cached on resume from L1'
    assert platform.get('status') == 'success', 'cached phase should be success'


@test('init: cached phases carry forward data')
def _():
    green_phases = [{'name': 'platform', 'status': 'success', 'duration_s': 120, 'tests_run': 500}]
    old = {'current': {'run_id': 100, 'phases': green_phases, 'started': '2026-01-01T00:00:00Z'}, 'history': []}
    data = run_init(resume_from='L1', old_progress=old)
    platform = next(p for p in data['current']['phases'] if p['name'] == 'platform')
    assert platform.get('duration_s') == 120, f'expected 120, got {platform.get("duration_s")}'
    assert platform.get('tests_run') == 500, f'expected 500, got {platform.get("tests_run")}'


@test('init: cancelled build conclusion')
def _():
    partial_phases = [
        {'name': 'platform', 'status': 'success'},
        {'name': 'ledger', 'status': 'pending'},
    ]
    old = {'current': {'run_id': 100, 'phases': partial_phases, 'started': '2026-01-01T00:00:00Z'}, 'history': []}
    data = run_init(old_progress=old)
    archived = data['history'][0]
    assert archived['conclusion'] == 'cancelled', f'expected cancelled, got {archived["conclusion"]}'


@test('init: failed build conclusion')
def _():
    failed_phases = [
        {'name': 'platform', 'status': 'failure'},
        {'name': 'ledger', 'status': 'pending'},
    ]
    old = {'current': {'run_id': 100, 'phases': failed_phases, 'started': '2026-01-01T00:00:00Z'}, 'history': []}
    data = run_init(old_progress=old)
    archived = data['history'][0]
    assert archived['conclusion'] == 'failure', f'expected failure, got {archived["conclusion"]}'


@test('init: green build conclusion')
def _():
    all_success = [{'name': 'platform', 'status': 'success'}, {'name': 'ledger', 'status': 'success'}]
    old = {'current': {'run_id': 100, 'phases': all_success, 'started': '2026-01-01T00:00:00Z'}, 'history': []}
    data = run_init(old_progress=old)
    archived = data['history'][0]
    assert archived['conclusion'] == 'success', f'expected success, got {archived["conclusion"]}'


@test('init: resume_from preserved in history')
def _():
    resumed = [{'name': 'platform', 'status': 'success', 'cached': True}, {'name': 'ledger', 'status': 'success'}]
    old = {'current': {'run_id': 100, 'resume_from': 'L1', 'phases': resumed, 'started': '2026-01-01T00:00:00Z'}, 'history': []}
    data = run_init(old_progress=old)
    archived = data['history'][0]
    assert archived.get('resume_from') == 'L1', f'expected L1, got {archived.get("resume_from")}'


# ── Workflow consistency tests ────────────────────────────────────

if os.path.exists(WORKFLOW_PATH):
    with open(WORKFLOW_PATH) as f:
        workflow_text = f.read()
        workflow = yaml.safe_load(workflow_text)

    @test('workflow: all jobs with run-with-progress have CI_DASHBOARD_TOKEN')
    def _():
        jobs = workflow.get('jobs', {})
        for name, job in jobs.items():
            steps = job.get('steps', [])
            uses_progress = any('run-with-progress' in str(s.get('run', '')) for s in steps)
            if uses_progress:
                env = job.get('env', {})
                has_token = 'CI_DASHBOARD_TOKEN' in str(env)
                assert has_token, f'job "{name}" uses run-with-progress but missing CI_DASHBOARD_TOKEN in env'

    @test('workflow: finalize job exists and runs on success+failure')
    def _():
        jobs = workflow.get('jobs', {})
        assert 'finalize' in jobs, 'finalize job not found'
        fin = jobs['finalize']
        assert 'always()' in str(fin.get('if', '')), 'finalize should run always()'

    @test('workflow: resume levels match pipeline.yaml')
    def _():
        on_key = workflow.get('on') or workflow.get(True)
        assert on_key, 'workflow missing on/True key'
        resume_input = on_key['workflow_dispatch']['inputs']['resume_from']
        options = resume_input.get('options', [])
        pipeline_resume = [lv['id'] for lv in pipeline['levels'] if lv.get('resume')]
        for lvl in pipeline_resume:
            assert lvl in options, f'pipeline resume level {lvl} not in workflow options: {options}'


# ── Summary ───────────────────────────────────────────────────────

print()
if failures:
    print(f'FAILED: {len(failures)} test(s)')
    for f in failures:
        print(f'  - {f}')
    sys.exit(1)
else:
    print('All tests passed')
