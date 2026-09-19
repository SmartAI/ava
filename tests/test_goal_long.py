"""Long-task selection must produce pinned, auditable tasks before inference."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from eval import goal_long
from eval.benchmark import AgentSpec, agent_config, normalize


def fixture_source(tmp_path, monkeypatch):
    source = tmp_path / 'upstream'
    source.mkdir()
    files = {
        'LICENSE': 'Fixture license\n',
        'case/instruction.md': 'Implement the evaluator.\n',
        'case/task.toml': 'schema_version = "1.1"\nartifacts = []\n[agent]\ntimeout_sec = 1800\n[verifier]\ntimeout_sec = 2400\n[environment]\ndocker_image = "source:tag"\n',
        'case/environment/Dockerfile': 'FROM source:tag\n',
        'case/environment/interp.py': 'print("immutable semantics")\n',
        'case/tests/test.sh': '#!/bin/sh\nexit 0\n',
        'case/solution/solve.sh': '#!/bin/sh\nexit 0\n',
    }
    for name, content in files.items():
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    for args in [('init', '-q'), ('add', '.'),
                 ('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'fixture')]:
        subprocess.run(['git', '-C', str(source), *args], check=True, capture_output=True)
    revision = subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
    catalog = tmp_path / 'catalog.json'
    catalog.write_text(json.dumps({'revision': revision, 'repository': 'fixture', 'name': 'long-test',
        'tasks': [{'id': 'case', 'split': 'development', 'image': 'example/image@sha256:' + 'a'*64,
                   'immutable': {'interp.py': 'environment/interp.py'}}]}))
    monkeypatch.setattr(goal_long, 'CATALOG', catalog)
    return source


def test_long_tasks_preserve_oracle_pin_image_and_guard_reference(tmp_path, monkeypatch):
    source = fixture_source(tmp_path, monkeypatch)
    output = tmp_path / 'prepared'
    manifest = json.loads(goal_long.prepare(source, output).read_text())
    task = Path(manifest['tasks'][0]['path'])
    config = tomllib.loads((task / 'task.toml').read_text())
    assert config['environment']['docker_image'].endswith('a'*64)
    assert config['agent']['timeout_sec'] == 1800
    assert config['verifier']['timeout_sec'] == 2400
    assert config['artifacts'] == [goal_long.ARCHIVE]
    assert config['verifier']['collect'][0]['command'] == goal_long.COLLECT
    assert (task / 'tests/upstream-test.sh').read_bytes() == (source / 'case/tests/test.sh').read_bytes()
    assert (task / 'solution/solve.sh').read_bytes() == (source / 'case/solution/solve.sh').read_bytes()
    assert (task / 'instruction.md').read_text().startswith((source / 'case/instruction.md').read_text())
    assert (task / 'LICENSE.upstream').read_text() == 'Fixture license\n'
    # Exercise the generated guard against a local stand-in for the container's /app.
    workspace = task / 'environment'
    script = (task / 'tests/check-reference.py').read_text().replace('Path("/app")', f'Path({str(workspace)!r})')
    checked = subprocess.run([sys.executable, '-c', script], capture_output=True)
    assert checked.returncode == 0, checked.stderr
    (workspace / 'interp.py').write_text('print("fake passing oracle")\n')
    rejected = subprocess.run([sys.executable, '-c', script], capture_output=True)
    assert rejected.returncode != 0 and b'Immutable reference changed' in rejected.stderr
    with pytest.raises(FileExistsError):
        goal_long.prepare(source, output)


def test_long_preparation_rejects_dirty_unknown_or_unpinned_sources(tmp_path, monkeypatch):
    source = fixture_source(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match='selection'):
        goal_long.prepare(source, tmp_path / 'unknown', ['missing'])
    (source / 'case/instruction.md').write_text('changed objective')
    with pytest.raises(ValueError, match='not clean'):
        goal_long.prepare(source, tmp_path / 'dirty')
    assert not (tmp_path / 'dirty').exists()
    catalog = json.loads(goal_long.CATALOG.read_text())
    catalog['revision'] = '0'*40
    goal_long.CATALOG.write_text(json.dumps(catalog))
    with pytest.raises(ValueError, match='revision'):
        goal_long.prepare(source, tmp_path / 'wrong-revision')


def test_generic_continuation_control_is_not_goal_mode():
    ordinary = dict(id='a', kind='ava', model='codex/gpt-6-astra', effort='medium')
    spec = AgentSpec(**ordinary, continuations=2)
    assert spec.continuations == 2
    config = agent_config(spec, Path('/frozen'), 1800)
    assert config['kwargs']['continuations'] == 2
    assert config['override_timeout_sec'] == 1800
    for value in (True, -1, 11, '2'):
        with pytest.raises(ValueError):
            AgentSpec(**ordinary, continuations=value)
    with pytest.raises(ValueError, match='ordinary Ava'):
        AgentSpec(**ordinary, goal=True, continuations=2)
    with pytest.raises(ValueError, match='ordinary Ava'):
        AgentSpec(id='pi', kind='pi', model='codex/gpt-6-astra', effort='medium', continuations=2)


@pytest.mark.parametrize('fail_second', [False, True])
def test_continuations_use_one_agent_prompt_and_scratch(home, project, tmp_path, monkeypatch, fail_second):
    from ava.app import cli
    from ava.session import Log, OpenMode, PromptResolved
    from ava.session.inspect import inspect_session
    from eval.integrations.continuation import run

    monkeypatch.chdir(project)
    script = tmp_path / 'mock.txt'
    script.write_text('text done\ndone\n')
    monkeypatch.setenv('AVA_MOCK_SCRIPT', str(script))
    prompt = tmp_path / 'prompt.txt'
    prompt.write_text('Keep this exact experimental prompt.\n')
    logs = tmp_path / 'logs'
    session = tmp_path / 'session.jsonl'
    original = cli._run_one_shot
    seen = []

    async def observed(agent, item):
        scratch = agent._state.scratchpad
        assert scratch is not None
        if seen:
            assert agent is seen[0][0] and scratch == seen[0][1]
            assert (scratch / 'probe').read_text() == 'keep across turns'
        else:
            (scratch / 'probe').write_text('keep across turns')
        seen.append((agent, scratch))
        if fail_second and len(seen) == 2:
            return cli.EXIT_ERROR
        return await original(agent, item)

    monkeypatch.setattr(cli, '_run_one_shot', observed)
    code = run(['2', str(logs), '-p', '--provider', 'mock', '--model', 'smoke-v1',
                '--no-compact', '--session', str(session), '--system-prompt-file', str(prompt),
                'Do not change the workspace.'])
    assert code == (cli.EXIT_ERROR if fail_second else cli.EXIT_OK)
    assert len(seen) == (2 if fail_second else 3)
    assert cli._run_one_shot is observed
    assert not seen[0][1].exists(), 'scratch must close only after the complete sequence'
    assert json.loads((logs / 'continuations.json').read_text())['continuations_sent'] == (1 if fail_second else 2)
    assert (logs / 'checkpoint-0.tar.gz').is_file()
    assert (logs / 'checkpoint-1.tar.gz').exists() is (not fail_second)
    assert inspect_session(session)['model_attempts'] == (1 if fail_second else 3)
    log = Log.open(session, OpenMode.read_only)
    try:
        prompts = [event.payload.system_prompt for event in log.loaded_events
                   if isinstance(event.payload, PromptResolved)]
        assert prompts == [prompt.read_text()]
    finally:
        log.close()


def test_failed_timeout_cleanup_invalidates_the_measurement():
    raw = {'exception_info': {'exception_type': 'AgentTimeoutError'},
           'verifier_result': {'rewards': {'reward': 1}},
           'agent_result': {'metadata': {'agent_failed': True}}}
    task = {'sha256': 'frozen', 'split': 'development', 'origin': 'fixture'}
    timed = normalize(raw, {}, task)
    assert timed['status'] == 'agent_timeout' and timed['correct'] is False
    assert timed['reward'] == 1  # Artifact verification and on-time completion are different.
    raw['agent_result']['metadata']['process_cleanup_error'] = 'processes survived'
    invalid = normalize(raw, {}, task)
    assert invalid['status'] == 'infrastructure_error' and invalid['correct'] is None


@pytest.mark.skipif(os.environ.get('AVA_EVAL_DOCKER_TESTS') != '1', reason='explicit Docker qualification')
def test_timeout_guard_stops_detached_work_but_preserves_existing_processes():
    image = json.loads(goal_long.CATALOG.read_text())['tasks'][0]['image']
    container = subprocess.check_output(['docker', 'run', '-d', '--rm', '--platform', 'linux/amd64',
                                        '--entrypoint', 'python3', image,
                                        '-c', 'import time; time.sleep(300)'], text=True).strip()

    def execute(code):
        return subprocess.check_output(['docker', 'exec', container, 'python3', '-c', code], text=True).strip()

    def guard(action, check=True):
        return subprocess.run(['docker', 'exec', container, 'python3', '/tmp/guard.py',
                               action, '/tmp/pids.json'], capture_output=True, text=True, check=check)

    try:
        subprocess.run(['docker', 'cp', 'eval/integrations/process_guard.py', container+':/tmp/guard.py'], check=True)
        preserved = int(execute("import subprocess; p=subprocess.Popen(['sleep','300'], start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); print(p.pid)"))
        guard('record')
        original = execute("from pathlib import Path; print(Path('/tmp/pids.json').read_text())")
        worker = "import subprocess,os,time,json; from pathlib import Path; p=subprocess.Popen(['sleep','300'],start_new_session=True); Path('/tmp/owned.json').write_text(json.dumps([os.getpid(),p.pid])); time.sleep(300)"
        execute(f"import subprocess,time; from pathlib import Path; subprocess.Popen(['python3','-c',{worker!r}],start_new_session=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\nready=Path('/tmp/owned.json'); deadline=time.monotonic()+5\nwhile (not ready.exists() or not ready.stat().st_size) and time.monotonic()<deadline: time.sleep(0.02)\nassert ready.exists() and ready.stat().st_size")
        execute("import json; from pathlib import Path; p=Path('/tmp/pids.json'); v=json.loads(p.read_text()); v['namespace']='wrong'; p.write_text(json.dumps(v))")
        rejected = guard('stop', check=False)
        assert rejected.returncode != 0 and 'different PID namespace' in rejected.stderr
        execute(f"from pathlib import Path; Path('/tmp/pids.json').write_text({original!r})")
        stopped = json.loads(guard('stop').stdout)
        owned = json.loads(execute("from pathlib import Path; print(Path('/tmp/owned.json').read_text())"))
        assert set(owned) <= set(stopped['killed_pids'])
        assert preserved not in stopped['killed_pids']
        execute(f"import os; os.kill({preserved},0)")
    finally:
        subprocess.run(['docker', 'stop', container], capture_output=True)
