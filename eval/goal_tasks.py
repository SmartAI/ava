"""Materialize the small goal golden set in the existing Harbor task format."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

TASKS = [
    {
        "id": "goal-migration", "split": "development",
        "instruction": "Migrate lookup(id) to lookup(id, *, namespace). Update library callers, CLI, and the runnable README example. Keep CLI output unchanged (Ada for id 1 in team). Remove the old one-argument API. Both team and other contain id 1, with names Ada and Grace respectively. Preserve test_existing.py.",
        "files": {
            "lookup.py": "DATA = {'team': {1: 'Ada'}, 'other': {1: 'Grace'}}\ndef lookup(id):\n    return DATA['team'][id]\ndef label(id):\n    return lookup(id)\n",
            "cli.py": "from lookup import lookup\nprint(lookup(1))\n",
            "README.md": "```python\nfrom lookup import lookup\nprint(lookup(1))\n```\n",
            "test_existing.py": "from lookup import label\nassert label(1) == 'Ada'\n",
        },
        "reference": {
            "lookup.py": "DATA = {'team': {1: 'Ada'}, 'other': {1: 'Grace'}}\ndef lookup(id, *, namespace):\n    return DATA[namespace][id]\ndef label(id):\n    return lookup(id, namespace='team')\n",
            "cli.py": "from lookup import lookup\nprint(lookup(1, namespace='team'))\n",
            "README.md": "```python\nfrom lookup import lookup\nprint(lookup(1, namespace='team'))\n```\n",
        },
        "check": """from lookup import lookup, label
assert lookup(1, namespace='team') == 'Ada'
assert lookup(1, namespace='other') == 'Grace'
assert label(1) == 'Ada'
try:
    lookup(1)
except TypeError:
    pass
else:
    raise AssertionError('old API still accepted')
assert run('cli.py').stdout.strip() == 'Ada'
example = re.search(r'```python\\n(.*?)```', Path('README.md').read_text(), re.S)
assert example
exec(example[1], {})
""",
    },
    {
        "id": "goal-csv", "split": "development",
        "instruction": "Fix total.py: read CSV rows (label, amount) from stdin, print their exact decimal sum, printing 0 for empty input. Handle quoted labels containing commas and negative decimal amounts. Keep the no-argument CLI and preserve test_existing.py. Verify every requirement, not just existing green tests.",
        "files": {
            "total.py": "import sys\nprint(sum(int(line.strip().split(',')[1]) for line in sys.stdin))\n",
            "test_existing.py": "import subprocess, sys\nr = subprocess.run([sys.executable, 'total.py'], input='a,2\\nb,3\\n', text=True, capture_output=True)\nassert r.returncode == 0 and r.stdout.strip() == '5'\n",
        },
        "reference": {"total.py": "import csv, sys\nfrom decimal import Decimal\nprint(sum((Decimal(row[1]) for row in csv.reader(sys.stdin)), Decimal(0)))\n"},
        "check": """from decimal import Decimal
for text, expected in [('', '0'), ('"x,y",1.25\\nz,-2.50\\n', '-1.25'), ('a,0.1\\nb,0.2\\n', '0.3')]:
    result = run('total.py', input=text)
    assert result.returncode == 0, result.stderr
    assert Decimal(result.stdout.strip()) == Decimal(expected)
""",
    },
    {
        "id": "goal-satisfied", "split": "development",
        "instruction": "Ensure validate.py reads JSON from stdin, accepts an object with a string name with exit 0, and rejects missing or nonstring names and malformed JSON with exit 1. Change nothing if already correct. Verify the behavior.",
        "files": {"validate.py": "import json, sys\ntry:\n    value = json.load(sys.stdin)\n    valid = isinstance(value, dict) and isinstance(value.get('name'), str)\nexcept ValueError:\n    valid = False\nsys.exit(0 if valid else 1)\n"},
        "reference": {},
        "check": """for text, code in [(' {"name":"Ada"}', 0), ('{}', 1), ('{"name":1}', 1), ('{', 1)]:
    assert run('validate.py', input=text).returncode == code
for name, original in STARTER.items():
    assert Path(name).read_text() == original, 'unnecessary modification'
""",
    },
    {
        "id": "goal-artifacts", "split": "validation",
        "instruction": "Add `python cli.py filter FIELD VALUE`: read JSON lines from stdin, select records whose FIELD equals the string VALUE, preserve order, emit JSON lines, and exit nonzero on malformed input. Stream input: emit each matching output immediately, even before stdin closes. Add filter to --help and a runnable shell example in README.md. Preserve the existing version command and test_existing.py.",
        "files": {
            "cli.py": "import sys\nif sys.argv[1:] == ['version']:\n    print('1.0')\nelse:\n    print('usage: cli.py version')\n",
            "README.md": "# Records CLI\nUse python cli.py version.\n",
            "test_existing.py": "import subprocess, sys\nr = subprocess.run([sys.executable, 'cli.py', 'version'], capture_output=True, text=True)\nassert r.returncode == 0 and r.stdout.strip() == '1.0'\n",
        },
        "reference": {
            "cli.py": "import argparse, json, sys\np = argparse.ArgumentParser()\ns = p.add_subparsers(dest='command', required=True)\ns.add_parser('version')\nf = s.add_parser('filter')\nf.add_argument('field')\nf.add_argument('value')\na = p.parse_args()\nif a.command == 'version':\n    print('1.0')\nelse:\n    try:\n        for line in sys.stdin:\n            row = json.loads(line)\n            if row.get(a.field) == a.value:\n                print(json.dumps(row), flush=True)\n    except (ValueError, AttributeError):\n        sys.exit(1)\n",
            "README.md": "# Records CLI\n```sh\nprintf '%s\\n' '{\"team\":\"a\"}' | python cli.py filter team a\n```\n",
        },
        "check": """import selectors
rows = [{'team':'a', 'i':3}, {'team':'b', 'i':2}, {'team':'a', 'i':1}]
r = run('cli.py', 'filter', 'team', 'a', input=''.join(json.dumps(row)+'\\n' for row in rows))
assert r.returncode == 0, r.stderr
assert [json.loads(line) for line in r.stdout.splitlines()] == [rows[0], rows[2]]
assert run('cli.py', 'filter', 'team', 'a', input='{\\n').returncode != 0
assert 'filter' in run('cli.py', '--help').stdout
examples = re.findall(r'```[^\\n]*\\n(.*?)```', Path('README.md').read_text(), re.S)
examples += re.findall(r'(?<!`)`([^`\\n]+)`(?!`)', Path('README.md').read_text())
valid_example = False
for example in examples:
    if 'filter' not in example:
        continue
    try:
        r = subprocess.run(['sh', '-c', example], capture_output=True, text=True, timeout=5)
        # A runnable filter example need not be the first fence, use a particular
        # fence language/field name, or emit exactly one JSON record.
        if r.returncode == 0 and all(isinstance(json.loads(line), dict) for line in r.stdout.splitlines()):
            valid_example = True
    except (ValueError, subprocess.TimeoutExpired):
        pass
assert valid_example, 'README needs a runnable filter example'
# A buffering implementation cannot produce output while stdin remains open.
p = subprocess.Popen([sys.executable, 'cli.py', 'filter', 'team', 'a'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
try:
    p.stdin.write(b'{"team":"a"}\\n'); p.stdin.flush()
    with selectors.DefaultSelector() as selector:
        selector.register(p.stdout, selectors.EVENT_READ)
        assert selector.select(3), 'filter buffered until EOF'
    assert json.loads(p.stdout.readline()) == {'team':'a'}
finally:
    p.kill(); p.communicate()
""",
    },
]


def grader(task: dict) -> str:
    return '''import json, os, re, subprocess, sys
from pathlib import Path
os.chdir(os.environ.get('AVA_TASK_WORKSPACE', '/app'))
sys.path.insert(0, os.getcwd())
def run(*args, input=''):
    return subprocess.run([sys.executable, *args], input=input, text=True, capture_output=True, timeout=5)
''' + f"STARTER = {task['files']!r}\n" + '''if 'test_existing.py' in STARTER:
    assert Path('test_existing.py').read_text() == STARTER['test_existing.py']
    assert run('test_existing.py').returncode == 0
''' + task['check']


def materialize(output: Path) -> None:
    output.mkdir(parents=True, exist_ok=False)
    rows = []
    for task in TASKS:
        root = output / task['id']
        for group, files in [('environment/workspace', task['files']), ('solution/fixed', task['reference'])]:
            for name, text in files.items():
                path = root / group / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text)
        for directory in ['tests', 'solution']:
            (root / directory).mkdir(parents=True, exist_ok=True)
        (root / 'instruction.md').write_text(task['instruction'] + '\nWorkspace: /app. Python standard library only.\n')
        (root / 'task.toml').write_text('schema_version = "1.4"\n[agent]\ntimeout_sec = 180.0\n[verifier]\ntimeout_sec = 30.0\n[environment]\nbuild_timeout_sec = 300.0\ncpus = 1\nmemory_mb = 1024\nstorage_mb = 2048\n')
        (root / 'environment/Dockerfile').write_text('FROM python:3.12-slim\nWORKDIR /app\nCOPY workspace/ /app/\n')
        (root / 'tests/grade.py').write_text(grader(task))
        (root / 'tests/test.sh').write_text('#!/bin/sh\nset -eu\ntest_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)\nreward_dir=${AVA_REWARD_DIR:-/logs/verifier}\nmkdir -p "$reward_dir"\nif python "$test_dir/grade.py"; then echo 1 > "$reward_dir/reward.txt"; else echo 0 > "$reward_dir/reward.txt"; fi\n')
        (root / 'solution/solve.sh').write_text('#!/bin/sh\nset -eu\nbase=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)\nif [ -d "$base/fixed" ]; then cp -R "$base/fixed/." "${AVA_TASK_WORKSPACE:-/app}/"; fi\n')
        rows.append({'id': task['id'], 'path': str(root.resolve()), 'split': task['split'], 'origin': 'ava-goal-golden-v1'})
    (output / 'suite.json').write_text(json.dumps({'schema_version': 1, 'name': 'goal-golden-v1', 'tasks': rows}, indent=2) + '\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    materialize(parser.parse_args().output)
