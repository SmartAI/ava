"""Prepare pinned upstream long-horizon candidates; do not invent forced turn boundaries.

Upstream sources stay in local storage. Instructions/graders are preserved except
for an explicit immutable-reference guard; archive the workspace before grading.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

from eval.benchmark import file_hash, read_json, tree_hash, write_json

CATALOG = Path(__file__).with_name('goal-long-cases.json')
ARCHIVE = '/tmp/goal-long-workspace.tar.gz'
COLLECT = f'tar -C /app -czf {ARCHIVE} .'


def prepare(source: Path, output: Path, tasks: list[str] | None = None) -> Path:
    catalog = read_json(CATALOG)
    source = source.resolve()
    revision = subprocess.check_output(
        ['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True,
    ).strip()
    if revision != catalog['revision']:
        raise ValueError(f"Expected upstream revision {catalog['revision']}, got {revision}")
    selected = [task for task in catalog['tasks'] if tasks is None or task['id'] in tasks]
    if not selected or (tasks is not None and set(tasks) != {task['id'] for task in selected}):
        raise ValueError('Unknown or empty long-task selection')
    names = [task['id'] for task in selected]
    changed = subprocess.check_output(
        ['git', '-C', str(source), 'status', '--porcelain', '--', *names, 'LICENSE'], text=True,
    )
    if changed:
        raise ValueError(f'Upstream task source is not clean: {changed}')
    # Validate the complete selection before materializing any deliverable.
    recipes = []
    for task in selected:
        root = source / task['id']
        for required in ['instruction.md', 'task.toml', 'environment/Dockerfile',
                         'solution/solve.sh', 'tests/test.sh']:
            if not (root / required).is_file():
                raise ValueError(f'Missing upstream task asset: {root / required}')
        image = task['image']
        if not re.fullmatch(r'[a-z0-9/.-]+@sha256:[0-9a-f]{64}', image):
            raise ValueError('Task images must be immutable digest references')
        original = (root / 'task.toml').read_text()
        config = tomllib.loads(original)
        if (original.count('artifacts = []') != 1 or config['verifier'].get('collect')
                or config['verifier'].get('environment_mode')):
            raise ValueError('Expected unadapted upstream task configuration')
        if len(re.findall(r'^docker_image = .*$', original, re.M)) != 1:
            raise ValueError('Expected exactly one upstream image declaration')
        hashes = {name: file_hash(root / path) for name, path in task['immutable'].items()}
        recipes.append((task, root, original, hashes))
    license_text = (source / 'LICENSE').read_text()
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    rows = []
    for task, root, original, hashes in recipes:
        target = output / 'tasks' / task['id']
        shutil.copytree(root, target)
        (target / 'LICENSE.upstream').write_text(license_text)
        # Even an explicit rebuild must use the same prebuilt, digest-pinned image.
        header = (root / 'environment/Dockerfile').read_text().split('FROM ', 1)[0]
        (target / 'environment/Dockerfile').write_text(header + f"FROM {task['image']}\nWORKDIR /app\n")
        config = original.replace('artifacts = []', f'artifacts = ["{ARCHIVE}"]', 1)
        config = re.sub(r'^docker_image = .*$', f"docker_image = {json.dumps(task['image'])}", config, flags=re.M)
        config += '\n[[verifier.collect]]\ncommand = ' + json.dumps(COLLECT) + '\ntimeout_sec = 120.0\n'
        (target / 'task.toml').write_text(config)
        if hashes:
            original_test = target / 'tests/test.sh'
            original_test.rename(target / 'tests/upstream-test.sh')
            (target / 'tests/check-reference.py').write_text(
                'import hashlib\nfrom pathlib import Path\n'
                + f'EXPECTED = {hashes!r}\n'
                + 'for name, expected in EXPECTED.items():\n'
                + '    path = Path("/app") / name\n'
                + '    assert path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == expected, '
                  'f"Immutable reference changed: {name}"\n'
            )
            original_test.write_text(
                '#!/bin/bash\nset -eu\nmkdir -p /logs/verifier\n'
                'if ! python3 /tests/check-reference.py; then\n'
                '  echo 0 > /logs/verifier/reward.txt\n  exit 0\nfi\n'
                'exec bash /tests/upstream-test.sh\n'
            )
            with (target / 'instruction.md').open('a') as stream:
                stream.write('\nEvaluation contract: keep the supplied reference files unchanged: '
                             + ', '.join(hashes) + '. Implement the requested deliverable, not the reference.\n')
        write_json(target / 'provenance.json', {
            'repository': catalog['repository'], 'revision': revision,
            'upstream_task_sha256': tree_hash(root), 'image': task['image'],
            'upstream_grader_sha256': file_hash(root / 'tests/test.sh'),
            'immutable_references': hashes, 'preparer_sha256': file_hash(Path(__file__)),
            'changes': ['pin prebuilt image', 'archive /app before verification']
                       + (['guard immutable reference and disclose it in instruction'] if hashes else []),
        })
        rows.append({'id': task['id'], 'path': str(target), 'split': task['split'],
                     'origin': f"terminal-bench-2@{revision}", 'sha256': tree_hash(target)})
    manifest = output / 'suite.json'
    write_json(manifest, {'schema_version': 1, 'name': catalog['name'], 'tasks': rows})
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--task', action='append', dest='tasks')
    args = parser.parse_args()
    print(prepare(args.source, args.output, args.tasks))
