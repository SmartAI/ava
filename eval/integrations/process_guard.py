"""Stop failed benchmark work before grading, including detached tool processes.

Only runs inside the same Docker PID namespace in which the baseline was recorded.
Successful trials deliberately keep background services for their task graders.
"""
from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path


def processes() -> dict[int, tuple[int, str]]:
    result = {}
    for directory in Path('/proc').iterdir():
        if not directory.name.isdigit():
            continue
        try:
            fields = dict(line.split(':', 1) for line in (directory / 'status').read_text().splitlines())
            result[int(directory.name)] = (int(fields['PPid']), fields['State'].strip()[0])
        except (OSError, KeyError):
            continue
    return result


def run(action: str, path: Path) -> None:
    if not Path('/.dockerenv').is_file():
        raise RuntimeError('Process guard requires a disposable Docker container')
    root, init_root = os.stat('/'), os.stat('/proc/1/root')
    if (root.st_dev, root.st_ino) != (init_root.st_dev, init_root.st_ino):
        raise RuntimeError('Process guard requires a container-local PID 1 root')
    namespace = os.readlink('/proc/self/ns/pid')
    if action == 'record':
        path.write_text(json.dumps({'namespace': namespace, 'pids': list(processes())}))
        return
    if action != 'stop':
        raise ValueError('Expected record or stop')
    baseline = json.loads(path.read_text())
    if baseline['namespace'] != namespace:
        raise RuntimeError('Refusing to clean a different PID namespace')
    protected = set(baseline['pids']) | {1}
    pid = os.getpid()
    current = processes()
    while pid and pid not in protected:
        protected.add(pid)
        pid = current.get(pid, (0, ''))[0]
    killed = set()
    # Re-scan to catch forked/orphaned children; zombies cannot execute or spend.
    for _ in range(20):
        remaining = {pid for pid, (_, state) in processes().items()
                     if pid not in protected and state != 'Z'}
        if not remaining:
            print(json.dumps({'killed_pids': sorted(killed)}))
            return
        for pid in remaining:
            try:
                os.kill(pid, signal.SIGKILL)
                killed.add(pid)
            except ProcessLookupError:
                pass
        time.sleep(0.05)
    raise RuntimeError('Benchmark processes survived cleanup')


if __name__ == '__main__':
    run(sys.argv[1], Path(sys.argv[2]))
