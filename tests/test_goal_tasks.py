"""Qualify goal graders against unchanged, reference, and partial deliverables."""
import os
import subprocess
import sys

import pytest

from eval.goal_tasks import TASKS, grader, materialize


@pytest.mark.parametrize('task', TASKS, ids=lambda task: task['id'])
def test_goal_controls(task, tmp_path):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    for name, text in task['files'].items():
        (workspace / name).write_text(text)
    check = tmp_path / 'grade.py'
    check.write_text(grader(task))

    def grade():
        return subprocess.run(
            [sys.executable, str(check)], capture_output=True, text=True, timeout=15,
            env={**os.environ, 'AVA_TASK_WORKSPACE': str(workspace)},
        )

    original = grade()
    assert (original.returncode == 0) == (task['id'] == 'goal-satisfied'), original.stderr
    for name, text in task['reference'].items():
        (workspace / name).write_text(text)
    reference = grade()
    assert reference.returncode == 0, reference.stderr
    if task['id'] == 'goal-migration':
        (workspace / 'cli.py').write_text(task['files']['cli.py'])
    elif task['id'] == 'goal-csv':
        (workspace / 'test_existing.py').write_text('# deleted to get green checks\n')
    elif task['id'] == 'goal-artifacts':
        # Grade the documented behavior, not the reference's fence label, field,
        # or record count. A live trial exposed the overly specific old oracle.
        (workspace / 'README.md').write_text(
            '```sh\npython cli.py version\n```\n\n```bash\nprintf \'%s\\n\' \'{"kind":"keep","n":1}\' \'{"kind":"keep","n":2}\' | python cli.py filter kind keep\n```\n'
        )
        alternative = grade()
        assert alternative.returncode == 0, alternative.stderr
        example = (workspace / 'README.md').read_text()
        (workspace / 'README.md').write_text('```sh\npython cli.py version\n```\n')
        assert grade().returncode != 0
        (workspace / 'README.md').write_text(example)
        path = workspace / 'cli.py'
        path.write_text(path.read_text().replace('for line in sys.stdin:', 'for line in list(sys.stdin):'))
    else:
        (workspace / 'validate.py').write_text("print('Done, all checks passed')\n")
    assert grade().returncode != 0


def test_materialize_goal_suite(tmp_path):
    import json

    output = tmp_path / 'tasks'
    materialize(output)
    rows = json.loads((output / 'suite.json').read_text())['tasks']
    assert len(rows) == 4
    for row in rows:
        from pathlib import Path
        task = Path(row['path'])
        assert all((task / name).is_file() for name in (
            'instruction.md', 'task.toml', 'tests/test.sh', 'solution/solve.sh',
        ))
        workspace = task / 'environment/workspace'
        spec = next(spec for spec in TASKS if spec['id'] == row['id'])
        for name, text in spec['reference'].items():
            (workspace / name).write_text(text)
        reward = task / 'reward'
        result = subprocess.run(['sh', str(task / 'tests/test.sh')], capture_output=True, text=True,
            env={**os.environ, 'AVA_TASK_WORKSPACE': str(workspace), 'AVA_REWARD_DIR': str(reward)}, timeout=15)
        assert result.returncode == 0, result.stderr
        assert (reward / 'reward.txt').read_text() == '1\n', result.stdout
