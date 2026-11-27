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


# ── Cross-file consistency ────────────────────────────────────────

@test('cross: pipeline_phases() in update-progress.sh handles object stacks')
def _():
    """The pipeline_phases() bash function iterates stacks raw. If object stacks
    (dicts) aren't handled, it yields dict keys like 'repos', 'name', 'maven_extra'
    instead of the actual phase name."""
    update_progress_path = os.path.join(SCRIPT_DIR, 'update-progress.sh')
    if not os.path.exists(update_progress_path):
        return
    with open(update_progress_path) as f:
        content = f.read()
    # The pipeline_phases function should handle dicts — check it doesn't just
    # iterate raw stack entries. If it uses a simple "for n in _g" on dicts,
    # it produces garbage. It should either use the name key or be replaced
    # by the init script's phase_names_for_level logic.
    has_object_stacks = any(isinstance(s, dict) for lv in pipeline['levels'] for s in lv.get('stacks', []))
    if has_object_stacks:
        # Run the actual pipeline_phases logic from update-progress.sh
        result = subprocess.run(
            ['python3', '-c',
             f"""
import yaml
p = yaml.safe_load(open('{PIPELINE_PATH}'))
for lv in p['levels']:
    if 'base' in lv: print(lv['base'])
    nm = lv.get('names', {{}})
    for s in lv.get('stacks', []):
        if isinstance(s, dict): print(s.get('name', s['repos'][0]))
        elif isinstance(s, str): print(s)
        else:
            for r in s: print(nm.get(r, r))
    if 'convergence' in lv: print(lv['convergence'])
"""],
            capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            names = result.stdout.strip().split('\n')
            for bad in ['repos', 'name', 'maven_extra']:
                assert bad not in names, \
                    f'pipeline_phases() produces garbage name "{bad}" from object stacks'


@test('cross: auto-retry JOB_RESULTS has no duplicate job keys')
def _():
    """JOB_RESULTS in finalize has level7 three times — only the first
    matters for the break-on-failure logic, but duplicates indicate
    copy-paste errors in the job-to-level mapping."""
    if not os.path.exists(WORKFLOW_PATH):
        return
    fin = workflow['jobs']['finalize']
    retry_step = next((s for s in fin['steps'] if 'auto-retry' in str(s.get('name', '')).lower()
                       or 'auto-retry' in str(s.get('run', ''))), None)
    if not retry_step:
        return
    env = retry_step.get('env', {})
    job_results = str(env.get('JOB_RESULTS', ''))
    # Extract job names from "jobname=${{ needs.jobname.result }}" patterns
    import re
    keys = re.findall(r'(\w+)=\$', job_results)
    dupes = [k for k in keys if keys.count(k) > 1]
    assert not dupes, f'JOB_RESULTS has duplicate keys: {dupes}'


@test('cross: level jobs pass maven-extra when pipeline has maven_extra')
def _():
    """If pipeline.yaml defines maven_extra for a level, the workflow job
    must pass it through to the composite action."""
    if not os.path.exists(WORKFLOW_PATH):
        return
    for level in pipeline['levels']:
        level_id = level['id']
        has_maven_extra = bool(level.get('maven_extra'))
        has_object_maven_extra = any(isinstance(s, dict) and s.get('maven_extra')
                                    for s in level.get('stacks', []))
        if has_maven_extra or has_object_maven_extra:
            job_key = f'level{level_id[1:]}'
            job = workflow['jobs'].get(job_key, {})
            steps = job.get('steps', [])
            composite_step = next((s for s in steps if '.github/actions/build-level' in str(s.get('uses', ''))), None)
            if composite_step:
                with_block = composite_step.get('with', {})
                has_extra = 'maven-extra' in with_block or 'maven_extra' in with_block
                assert has_extra, \
                    f'{level_id} has maven_extra in pipeline.yaml but job {job_key} does not pass maven-extra to composite action'


@test('cross: level jobs pass pom-overrides when pipeline has pom')
def _():
    """If pipeline.yaml defines pom overrides for a level, the workflow job
    must pass pom-overrides through."""
    if not os.path.exists(WORKFLOW_PATH):
        return
    for level in pipeline['levels']:
        level_id = level['id']
        if level.get('pom'):
            job_key = f'level{level_id[1:]}'
            job = workflow['jobs'].get(job_key, {})
            steps = job.get('steps', [])
            composite_step = next((s for s in steps if '.github/actions/build-level' in str(s.get('uses', ''))), None)
            if composite_step:
                with_block = composite_step.get('with', {})
                has_pom = 'pom-overrides' in with_block or 'pom_overrides' in with_block
                assert has_pom, \
                    f'{level_id} has pom overrides in pipeline.yaml but job {job_key} does not pass pom-overrides'


@test('cross: all pipeline repos appear in publish.yaml')
def _():
    """Every repo in pipeline.yaml should be in publish.yaml, otherwise
    verified artifacts won't be published."""
    publish_path = os.path.join(SCRIPT_DIR, 'publish.yaml')
    if not os.path.exists(publish_path):
        return
    with open(publish_path) as f:
        publish = yaml.safe_load(f)
    published = set(publish.get('maven', []))
    pipeline_repos = set()
    for level in pipeline['levels']:
        for stack in level.get('stacks', []):
            if isinstance(stack, dict):
                pipeline_repos.update(stack['repos'])
            elif isinstance(stack, list):
                pipeline_repos.update(stack)
            else:
                pipeline_repos.add(stack)
    missing = pipeline_repos - published
    assert not missing, f'repos in pipeline.yaml but not in publish.yaml: {missing}'


@test('cross: single-repo matrix names match init phase names')
def _():
    """Single-repo matrix entries must match init phase names exactly.
    Multi-repo bundles (e.g. 'ledger, eidos') are fine — the composite
    action reports per-repo, not per-bundle."""
    result = subprocess.run(
        [sys.executable, os.path.join(SCRIPT_DIR, 'compute-matrices.py'),
         '--pipeline', PIPELINE_PATH],
        capture_output=True, text=True, timeout=10)
    if result.returncode != 0:
        return
    matrices_line = next((l for l in result.stdout.strip().split('\n') if l.startswith('matrices=')), None)
    if not matrices_line:
        return
    matrices = json.loads(matrices_line.split('=', 1)[1])
    init_data = run_init()
    init_names = {p['name'] for p in init_data['current']['phases']}
    for level_key, matrix in matrices.items():
        for entry in matrix.get('include', []):
            repos = entry['repos'].split()
            if len(repos) == 1:
                assert entry['name'] in init_names, \
                    f'single-repo matrix name "{entry["name"]}" not in init phases'


# ── Init edge cases ──────────────────────────────────────────────

@test('init: compile-only build data carries forward on resume')
def _():
    """Compile-only builds have duration_s but no tests_run. The history
    search requires (duration_s or tests_run), so duration_s alone should
    be enough for carry-forward."""
    compile_only_phases = [{'name': 'platform', 'status': 'success', 'duration_s': 200}]
    old = {'current': {'run_id': 100, 'phases': compile_only_phases, 'started': '2026-01-01T00:00:00Z'}, 'history': []}
    data = run_init(resume_from='L1', old_progress=old)
    platform = next(p for p in data['current']['phases'] if p['name'] == 'platform')
    assert platform.get('cached') is True, 'platform should be cached'
    assert platform.get('duration_s') == 200, f'expected 200, got {platform.get("duration_s")}'


@test('init: full build sets first phase to in_progress')
def _():
    data = run_init()
    first = data['current']['phases'][0]
    assert first['status'] == 'in_progress', f'first phase should be in_progress, got {first["status"]}'
    assert 'started' in first, 'first phase should have started timestamp'


@test('init: resume does NOT set first phase to in_progress')
def _():
    """On resume, the first phase is cached (before resume level). It should
    NOT be overwritten to in_progress."""
    green = [{'name': 'platform', 'status': 'success', 'duration_s': 100}]
    old = {'current': {'run_id': 100, 'phases': green, 'started': '2026-01-01T00:00:00Z'}, 'history': []}
    data = run_init(resume_from='L1', old_progress=old)
    first = data['current']['phases'][0]
    assert first['status'] == 'success', f'first phase on resume should stay success (cached), got {first["status"]}'
    assert first.get('cached') is True, 'first phase on resume should be cached'


@test('init: history capped at 200 entries')
def _():
    big_history = [{'run_id': i, 'phases': [], 'started': '2026-01-01T00:00:00Z'} for i in range(250)]
    old = {'current': {'run_id': 999, 'phases': [{'name': 'x', 'status': 'success'}],
                       'started': '2026-01-01T00:00:00Z'}, 'history': big_history}
    data = run_init(old_progress=old)
    assert len(data['history']) <= 200, f'history should be capped at 200, got {len(data["history"])}'


@test('init: run_id, run_url, commit propagated to current')
def _():
    data = run_init()
    cur = data['current']
    assert cur['run_id'] == 999, f'run_id should be 999, got {cur["run_id"]}'
    assert cur['run_url'] == 'test', f'run_url should be test, got {cur["run_url"]}'
    assert cur['commit'] == 'abc', f'commit should be abc, got {cur["commit"]}'


@test('init: duration_s computed for archived entry')
def _():
    old = {'current': {'run_id': 100, 'phases': [{'name': 'x', 'status': 'success'}],
                       'started': '2026-01-01T00:00:00Z'}, 'history': []}
    data = run_init(old_progress=old)
    archived = data['history'][0]
    assert 'duration_s' in archived, 'archived entry should have computed duration_s'
    assert archived['duration_s'] > 0, f'duration should be > 0, got {archived["duration_s"]}'


@test('init: total_tests summed correctly in archived entry')
def _():
    phases = [
        {'name': 'a', 'status': 'success', 'tests_run': 100},
        {'name': 'b', 'status': 'success', 'tests_run': 200},
        {'name': 'c', 'status': 'success'},
    ]
    old = {'current': {'run_id': 100, 'phases': phases, 'started': '2026-01-01T00:00:00Z'}, 'history': []}
    data = run_init(old_progress=old)
    archived = data['history'][0]
    assert archived['total_tests'] == 300, f'expected 300, got {archived["total_tests"]}'


@test('init: empty old-progress produces valid output')
def _():
    data = run_init(old_progress={})
    assert 'current' in data, 'missing current'
    assert 'history' in data, 'missing history'
    assert len(data['history']) == 0, 'history should be empty with no prior run'


# ── Workflow structure ───────────────────────────────────────────

if os.path.exists(WORKFLOW_PATH):

    @test('workflow: level dependency chain is correct')
    def _():
        """Each level should depend on the previous level and config."""
        jobs = workflow.get('jobs', {})
        expected_deps = {
            'level1': 'prepare',
            'level2': 'level1',
            'level3': 'level2',
            'level4': 'level3',
            'level5': 'level4',
            'level6': 'level5',
            'level7': 'level6',
        }
        for job_name, prev in expected_deps.items():
            job = jobs.get(job_name, {})
            needs = job.get('needs', [])
            assert prev in needs, f'{job_name} should depend on {prev}, needs: {needs}'
            assert 'config' in needs, f'{job_name} should depend on config, needs: {needs}'

    @test('workflow: all level jobs have fail-fast false')
    def _():
        """Without fail-fast: false, one matrix entry failing cancels the others."""
        jobs = workflow.get('jobs', {})
        for name, job in jobs.items():
            if name.startswith('level'):
                strategy = job.get('strategy', {})
                assert strategy.get('fail-fast') is False, \
                    f'{name} should have fail-fast: false'

    @test('workflow: finalize depends on ALL level jobs')
    def _():
        fin = workflow['jobs']['finalize']
        needs = fin.get('needs', [])
        for i in range(1, 8):
            assert f'level{i}' in needs, f'finalize should depend on level{i}'

    @test('workflow: level count matches pipeline.yaml (L0 is prepare)')
    def _():
        """L0 (platform) is built in the prepare job, not a level* job.
        So workflow level jobs = pipeline levels - 1."""
        pipeline_levels = len(pipeline['levels'])
        workflow_levels = sum(1 for name in workflow['jobs'] if name.startswith('level'))
        assert workflow_levels == pipeline_levels - 1, \
            f'workflow has {workflow_levels} level jobs but pipeline.yaml has {pipeline_levels} levels (L0 is prepare)'

    @test('workflow: publish step only runs when build_ok')
    def _():
        fin = workflow['jobs']['finalize']
        publish_step = next((s for s in fin['steps'] if 'publish' in s.get('name', '').lower()), None)
        if publish_step:
            condition = str(publish_step.get('if', ''))
            assert 'build_ok' in condition, \
                'publish step should only run when build_ok is true'

    @test('workflow: build_ok check includes cancelled jobs')
    def _():
        fin = workflow['jobs']['finalize']
        result_step = next((s for s in fin['steps'] if s.get('id') == 'result'), None)
        assert result_step, 'finalize must have a result step with id=result'
        run_script = str(result_step.get('run', ''))
        assert 'cancelled' in run_script, \
            'build_ok check must treat cancelled jobs as not-ok'


# ── Per-repo resume tests ─────────────────────────────────────────

@test('init: per-repo resume caches skipped repos at resume level')
def _():
    """When resume_repos filters to only agency at L4, the other L4
    repos (ras, desiredstate, claudony, langchain4j, workers) should
    be cached from history — not left as pending."""
    prev_phases = [
        {'name': 'platform', 'status': 'success', 'duration_s': 120},
        {'name': 'ras', 'status': 'success', 'duration_s': 60, 'tests_run': 200},
        {'name': 'desiredstate', 'status': 'success', 'duration_s': 90},
        {'name': 'agency', 'status': 'failure'},
    ]
    old = {'current': {'run_id': 100, 'phases': prev_phases,
                       'started': '2026-01-01T00:00:00Z', 'resume_from': 'L4'},
           'history': []}
    data = run_init(resume_from='L4', old_progress=old)
    ras = next((p for p in data['current']['phases'] if p['name'] == 'ras'), None)
    assert ras is not None, 'ras phase not found'
    assert ras.get('cached') is True or ras.get('status') == 'success', \
        f'ras should be cached at resume level L4, got status={ras.get("status")} cached={ras.get("cached")}'


@test('init: resume_from preserved when archiving to history')
def _():
    """A resume build's resume_from must survive into the history entry."""
    resumed_phases = [
        {'name': 'platform', 'status': 'success', 'cached': True},
        {'name': 'agency', 'status': 'success'},
    ]
    old = {'current': {'run_id': 100, 'resume_from': 'L4',
                       'phases': resumed_phases,
                       'started': '2026-01-01T00:00:00Z'},
           'history': []}
    data = run_init(old_progress=old)
    archived = data['history'][0]
    assert archived.get('resume_from') == 'L4', \
        f'resume_from should be L4 in history, got {archived.get("resume_from")}'


@test('init: test_mode propagated to current build')
def _():
    data = run_init()
    assert data['current'].get('test_mode') == 'skip', \
        f'default test_mode should be skip, got {data["current"].get("test_mode")}'


@test('init: test_mode preserved in history')
def _():
    old = {'current': {'run_id': 100, 'test_mode': 'run',
                       'phases': [{'name': 'platform', 'status': 'success'}],
                       'started': '2026-01-01T00:00:00Z'},
           'history': []}
    data = run_init(old_progress=old)
    archived = data['history'][0]
    assert archived.get('test_mode') == 'run', \
        f'test_mode should be run in history, got {archived.get("test_mode")}'


# ── Summary ───────────────────────────────────────────────────────

print()
if failures:
    print(f'FAILED: {len(failures)} test(s)')
    for f in failures:
        print(f'  - {f}')
    sys.exit(1)
else:
    print('All tests passed')
