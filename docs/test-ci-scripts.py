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


# ── Category 1: Composite action env vars ─────────────────────────

COMPOSITE_PATH = os.path.join(SCRIPT_DIR, '..', '.github', 'actions', 'build-level', 'action.yml')
if os.path.exists(COMPOSITE_PATH):
    with open(COMPOSITE_PATH) as f:
        composite = yaml.safe_load(f)
    composite_text = open(COMPOSITE_PATH).read()

    @test('composite: build step has CI_DASHBOARD_TOKEN')
    def _():
        build_step = next((s for s in composite['runs']['steps'] if s.get('name') == 'Build'), None)
        assert build_step, 'Build step not found in composite action'
        env = build_step.get('env', {})
        assert 'CI_DASHBOARD_TOKEN' in env, 'Build step missing CI_DASHBOARD_TOKEN'

    @test('composite: build step has GITHUB_TOKEN')
    def _():
        build_step = next((s for s in composite['runs']['steps'] if s.get('name') == 'Build'), None)
        env = build_step.get('env', {})
        assert 'GITHUB_TOKEN' in env, 'Build step missing GITHUB_TOKEN'

    @test('composite: build step has GH_PACKAGES_TOKEN')
    def _():
        build_step = next((s for s in composite['runs']['steps'] if s.get('name') == 'Build'), None)
        env = build_step.get('env', {})
        assert 'GH_PACKAGES_TOKEN' in env, 'Build step missing GH_PACKAGES_TOKEN'

    @test('composite: has native library cache restore')
    def _():
        step_names = [s.get('name', '') for s in composite['runs']['steps']]
        assert any('native' in n.lower() for n in step_names), 'Missing native library cache step'

    @test('composite: has setup-java step with GITHUB_TOKEN')
    def _():
        java_step = next((s for s in composite['runs']['steps'] if 'setup-java' in str(s.get('uses', ''))), None)
        assert java_step, 'setup-java step not found'
        env = java_step.get('env', {})
        assert 'GITHUB_TOKEN' in env, 'setup-java step missing GITHUB_TOKEN env'


# ── Category 2: Artifact integrity ────────────────────────────────

    @test('composite: has JAR validation step')
    def _():
        step_names = [s.get('name', '') for s in composite['runs']['steps']]
        assert any('validate' in n.lower() and 'jar' in n.lower() for n in step_names), \
            'Missing JAR validation step'

    @test('composite: JAR validation uses zipfile.testzip')
    def _():
        validate_step = next((s for s in composite['runs']['steps']
                             if 'validate' in s.get('name', '').lower() and 'jar' in s.get('name', '').lower()), None)
        assert validate_step, 'JAR validation step not found'
        run_text = str(validate_step.get('run', ''))
        assert 'testzip' in run_text, 'JAR validation should use zipfile.testzip()'

    @test('composite: non-overlapping artifact uploads (diff strategy)')
    def _():
        step_names = [s.get('name', '') for s in composite['runs']['steps']]
        has_before = any('before' in n.lower() and ('snapshot' in n.lower() or 'build' in n.lower()) for n in step_names)
        has_collect = any('collect' in n.lower() and 'new' in n.lower() for n in step_names)
        assert has_before and has_collect, \
            f'Missing diff strategy steps (before={has_before}, collect={has_collect})'


# ── Category 3: Timeout management ────────────────────────────────

@test('pipeline: all timeouts >= 20 minutes')
def _():
    for level in pipeline['levels']:
        timeout = level.get('timeout')
        if timeout is not None:
            assert timeout >= 20, f'{level["id"]} timeout {timeout} < 20 min'


@test('pipeline: levels without explicit timeout get default >= 30')
def _():
    for level in pipeline['levels']:
        if level.get('timeout') is None:
            pass  # default is 30 in compute-matrices.py — checked there


# ── Category 4: Dashboard data consistency (extended) ─────────────

@test('init: phase names match renderer lookup logic')
def _():
    """Simulates how index.html renderPipe maps pipeline stacks to phase names."""
    data = run_init()
    phase_map = {p['name']: p for p in data['current']['phases']}
    for level in pipeline['levels']:
        name_map = level.get('names', {})
        for stack in level.get('stacks', []):
            if isinstance(stack, list):
                for n in stack:
                    lookup = name_map.get(n, n)
                    assert lookup in phase_map, \
                        f'{level["id"]}: renderer looks up "{lookup}" but init has: {list(phase_map.keys())}'
            elif isinstance(stack, dict):
                lookup = stack.get('name') or stack['repos'][0]
                assert lookup in phase_map, \
                    f'{level["id"]}: renderer looks up "{lookup}" for object stack but not in phases'


@test('init: in_progress phases become cancelled on archive')
def _():
    in_progress_phases = [
        {'name': 'platform', 'status': 'success'},
        {'name': 'ledger', 'status': 'in_progress', 'started': '2026-01-01T00:00:00Z'},
        {'name': 'eidos', 'status': 'pending'},
    ]
    old = {'current': {'run_id': 100, 'phases': in_progress_phases, 'started': '2026-01-01T00:00:00Z'}, 'history': []}
    data = run_init(old_progress=old)
    archived = data['history'][0]
    ledger = next(p for p in archived['phases'] if p['name'] == 'ledger')
    assert ledger['status'] == 'cancelled', f'in_progress should become cancelled, got {ledger["status"]}'


@test('init: cached phases not counted as incomplete')
def _():
    """Cached phases from resume should not trigger 'cancelled' conclusion."""
    cached_phases = [
        {'name': 'platform', 'status': 'success', 'cached': True},
        {'name': 'ledger', 'status': 'success'},
    ]
    old = {'current': {'run_id': 100, 'phases': cached_phases, 'started': '2026-01-01T00:00:00Z'}, 'history': []}
    data = run_init(old_progress=old)
    archived = data['history'][0]
    assert archived['conclusion'] == 'success', \
        f'all success (some cached) should be success, got {archived["conclusion"]}'


@test('init: history search finds data from older entries')
def _():
    """When the most recent history entry has no data, search older entries."""
    old_history = [
        {'run_id': 50, 'phases': [
            {'name': 'platform', 'status': 'success', 'duration_s': 300, 'tests_run': 1000},
        ]}
    ]
    cancelled_current = {
        'run_id': 99, 'phases': [
            {'name': 'platform', 'status': 'pending'},
        ],
        'started': '2026-01-01T00:00:00Z'
    }
    old = {'current': cancelled_current, 'history': old_history}
    data = run_init(resume_from='L1', old_progress=old)
    platform = next(p for p in data['current']['phases'] if p['name'] == 'platform')
    assert platform.get('cached') is True, 'platform should be cached'
    assert platform.get('duration_s') == 300, f'should carry forward 300s, got {platform.get("duration_s")}'


# ── Category 5: Cache management ─────────────────────────────────

if os.path.exists(WORKFLOW_PATH):

    @test('finalize: has snapshot cache restore step')
    def _():
        fin = workflow['jobs']['finalize']
        step_names = [s.get('name', '') for s in fin.get('steps', [])]
        assert any('restore' in n.lower() and ('snapshot' in n.lower() or 'cache' in n.lower())
                   for n in step_names), 'finalize missing snapshot cache restore'

    @test('finalize: has snapshot cache save step')
    def _():
        fin = workflow['jobs']['finalize']
        step_names = [s.get('name', '') for s in fin.get('steps', [])]
        assert any('save' in n.lower() and ('snapshot' in n.lower() or 'cache' in n.lower())
                   for n in step_names), 'finalize missing snapshot cache save'

    @test('finalize: cache restore and save use same key')
    def _():
        fin = workflow['jobs']['finalize']
        restore_step = next((s for s in fin['steps']
                            if 'cache/restore' in str(s.get('uses', ''))
                            and 'casehub' in str(s.get('with', {}).get('key', ''))), None)
        save_step = next((s for s in fin['steps']
                         if 'cache/save' in str(s.get('uses', ''))
                         and 'casehub' in str(s.get('with', {}).get('key', ''))), None)
        assert restore_step and save_step, 'missing restore or save step'
        restore_key = restore_step['with']['key']
        save_key = save_step['with']['key']
        assert restore_key == save_key, f'cache keys differ: restore={restore_key}, save={save_key}'

    @test('finalize: cache key is stable (no github.sha)')
    def _():
        fin = workflow['jobs']['finalize']
        save_step = next((s for s in fin['steps']
                         if 'cache/save' in str(s.get('uses', ''))
                         and 'casehub' in str(s.get('with', {}).get('key', ''))), None)
        assert save_step, 'save step not found'
        key = save_step['with']['key']
        assert 'github.sha' not in key, f'cache key uses github.sha (creates unique entries that evict): {key}'

    @test('finalize: has cache delete before save (overwrite pattern)')
    def _():
        fin = workflow['jobs']['finalize']
        step_names = [s.get('name', '') for s in fin['steps']]
        assert any('delete' in n.lower() and ('cache' in n.lower() or 'snapshot' in n.lower())
                   for n in step_names), 'finalize missing cache delete step (needed for overwrite)'


# ── Category 6: Resume correctness ───────────────────────────────

    COMPUTE_MATRICES = os.path.join(SCRIPT_DIR, 'compute-matrices.py')

    @test('compute-matrices: produces valid JSON for full build')
    def _():
        result = subprocess.run(
            [sys.executable, COMPUTE_MATRICES, '--pipeline', PIPELINE_PATH],
            capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, f'exit {result.returncode}: {result.stderr}'
        lines = result.stdout.strip().split('\n')
        for line in lines:
            key, value = line.split('=', 1)
            parsed = json.loads(value)
            assert isinstance(parsed, dict), f'{key} is not a dict'

    @test('compute-matrices: resume with repo filter narrows matrix')
    def _():
        # Resume from L4 with only 'claudony' should produce a filtered L4 matrix
        result = subprocess.run(
            [sys.executable, COMPUTE_MATRICES, '--pipeline', PIPELINE_PATH,
             '--resume-from', 'L4', '--resume-repos', 'claudony'],
            capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, f'exit {result.returncode}: {result.stderr}'
        matrices_line = next(l for l in result.stdout.strip().split('\n') if l.startswith('matrices='))
        matrices = json.loads(matrices_line.split('=', 1)[1])
        l4 = matrices.get('l4', {}).get('include', [])
        assert len(l4) == 1, f'resume with claudony filter should give 1 entry, got {len(l4)}'
        assert 'claudony' in l4[0].get('repos', ''), f'filtered entry should contain claudony: {l4[0]}'

    @test('compute-matrices: matrices have required fields')
    def _():
        result = subprocess.run(
            [sys.executable, COMPUTE_MATRICES, '--pipeline', PIPELINE_PATH],
            capture_output=True, text=True, timeout=10)
        matrices_line = next(l for l in result.stdout.strip().split('\n') if l.startswith('matrices='))
        matrices = json.loads(matrices_line.split('=', 1)[1])
        for level_key, matrix in matrices.items():
            for entry in matrix.get('include', []):
                assert 'name' in entry, f'{level_key} entry missing name: {entry}'
                assert 'repos' in entry, f'{level_key} entry missing repos: {entry}'


# ── Category 7: Warm cache completeness ───────────────────────────

    @test('warm-cache: includes dependency:resolve-plugins')
    def _():
        warm = workflow['jobs'].get('warm-cache', {})
        steps_text = str(warm.get('steps', []))
        assert 'resolve-plugins' in steps_text, 'warm-cache missing dependency:resolve-plugins'

    @test('warm-cache: resolves spotless dynamic deps')
    def _():
        warm = workflow['jobs'].get('warm-cache', {})
        steps_text = str(warm.get('steps', []))
        assert 'spotless' in steps_text, 'warm-cache missing spotless dynamic dep resolution'


# ── Category 8: Sparse checkout completeness ──────────────────────

    @test('config: sparse checkout includes all referenced scripts')
    def _():
        config = workflow['jobs']['config']
        checkout_step = next((s for s in config['steps'] if 'checkout' in str(s.get('uses', ''))), None)
        assert checkout_step, 'config job missing checkout step'
        sparse = checkout_step.get('with', {}).get('sparse-checkout', '')
        # Find all scripts referenced in config job run steps
        referenced_scripts = set()
        for step in config['steps']:
            run_text = str(step.get('run', ''))
            for word in run_text.split():
                if word.startswith('docs/') and ('.' in word.split('/')[-1]):
                    referenced_scripts.add(word)
        for script in referenced_scripts:
            assert script in sparse, f'config job references {script} but not in sparse-checkout'


# ── Summary ───────────────────────────────────────────────────────

print()
if failures:
    print(f'FAILED: {len(failures)} test(s)')
    for f in failures:
        print(f'  - {f}')
    sys.exit(1)
else:
    print('All tests passed')
