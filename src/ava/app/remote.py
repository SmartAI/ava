"""SSH entry point for identity-checked Git and an owned interactive terminal."""

from __future__ import annotations

import argparse
import json
import os
import selectors
import shutil
import signal
import subprocess
import sys
import termios
import tty
from pathlib import Path

import psutil

from ava.base import AvaError, ava_home


def terminal(root: Path) -> int:
    if not os.isatty(0):
        raise ValueError("An SSH pseudo-terminal is required.")
    shell = shutil.which(os.environ.get("SHELL", "/bin/sh"))
    if not shell:
        raise ValueError("The remote login shell could not be found.")
    master, slave = os.openpty()
    process: subprocess.Popen | None = None
    process_identity: psutil.Process | None = None
    original = termios.tcgetattr(0)
    stopping = False
    resized = True

    def stop(*_):
        nonlocal stopping
        stopping = True

    def resize(*_):
        nonlocal resized
        resized = True

    previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGHUP, signal.SIGTERM)}
    previous[signal.SIGWINCH] = signal.signal(signal.SIGWINCH, resize)
    try:
        termios.tcsetwinsize(slave, termios.tcgetwinsize(0))
        environment = dict(os.environ, TERM="xterm-256color", COLORTERM="truecolor", TERM_PROGRAM="Ava", PWD=str(root))
        for name in ("COLUMNS", "LINES"):
            environment.pop(name, None)
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).parent / "desktop" / "_terminal_child.py"), shell, "-l", "-i"],
            cwd=root, env=environment, stdin=slave, stdout=slave, stderr=slave, start_new_session=True,
        )
        process_identity = psutil.Process(process.pid)
        process_identity.create_time()
        os.close(slave)
        slave = -1
        tty.setraw(0, termios.TCSANOW)
        for fd in (0, 1, master):
            os.set_blocking(fd, False)
        incoming = bytearray()
        outgoing = bytearray()
        eof = False
        with selectors.DefaultSelector() as selector:
            def watch(fd, events):
                try:
                    selector.unregister(fd)
                except KeyError:
                    pass
                if events:
                    selector.register(fd, events)

            while not stopping:
                if resized:
                    resized = False
                    termios.tcsetwinsize(master, termios.tcgetwinsize(0))
                if eof and not outgoing:
                    break
                watch(0, selectors.EVENT_READ if len(incoming) < 1024 * 1024 else 0)
                watch(1, selectors.EVENT_WRITE if outgoing else 0)
                watch(master, (selectors.EVENT_READ if not eof and len(outgoing) < 256 * 1024 else 0)
                      | (selectors.EVENT_WRITE if incoming and not eof else 0))
                for key, events in selector.select(0.1):
                    fd = key.fd
                    try:
                        if events & selectors.EVENT_READ:
                            data = os.read(fd, 32 * 1024)
                            if not data:
                                if fd == 0:
                                    stopping = True
                                else:
                                    eof = True
                            elif fd == 0:
                                incoming.extend(data)
                            else:
                                outgoing.extend(data)
                        if events & selectors.EVENT_WRITE:
                            pending = outgoing if fd == 1 else incoming
                            count = os.write(fd, pending)
                            del pending[:count]
                    except BlockingIOError:
                        pass
                    except OSError:
                        if fd == master:
                            eof = True
                            incoming.clear()
                        else:
                            stopping = True
                if process.poll() is not None and not outgoing:
                    # Drain PTY output before displaying the final exit status.
                    try:
                        outgoing.extend(os.read(master, 32 * 1024))
                    except (OSError, BlockingIOError):
                        break
                    if not outgoing:
                        break
        return process.poll() or 0
    finally:
        if process is not None:
            try:
                # Job-control groups can outlive their login shell. They still
                # belong to its POSIX session, even after reparenting to init.
                targets = []
                if process_identity is not None and (not psutil.pid_exists(process.pid) or process_identity.is_running()):
                    for candidate in psutil.process_iter():
                        try:
                            if os.getsid(candidate.pid) == process.pid:
                                candidate.create_time()
                                targets.append(candidate)
                        except (OSError, psutil.Error):
                            pass
            except psutil.Error:
                targets = []
            for target in reversed(targets):
                try:
                    target.send_signal(signal.SIGHUP)
                except psutil.Error:
                    pass
            _, alive = psutil.wait_procs(targets, timeout=0.8)
            for target in alive:
                try:
                    target.kill()
                except psutil.Error:
                    pass
            process.wait(timeout=2)
        for fd in (master, slave):
            if fd >= 0:
                os.close(fd)
        try:
            termios.tcsetattr(0, termios.TCSANOW, original)
        except (OSError, termios.error):
            pass
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def main() -> int:
    parser = argparse.ArgumentParser(description="Private Ava SSH workspace entry point")
    parser.add_argument("--machine-id", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("action", choices=("git", "terminal"))
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    try:
        identity = json.loads((ava_home() / "machine.json").read_text())
        if identity.get("machine_id") != args.machine_id:
            raise ValueError("Machine identity changed. Reconnect this machine before opening its workspace.")
        root = Path(args.project).expanduser().resolve(strict=True)
        if not root.is_dir():
            raise ValueError("The project directory is no longer available.")
        if args.action == "git":
            os.chdir(root)
            os.environ.update(GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0")
            os.execvp("git", ["git", *args.arguments])
        return terminal(root)
    except (OSError, termios.error, ValueError, AvaError) as error:
        print(f"Ava remote workspace: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
