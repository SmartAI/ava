"""Bridge OpenSSH's host-key prompt to the desktop's private connection IPC.

OpenSSH performs verification and writes known_hosts on the same connection the
user approved. Passwords, private-key passphrases and changed-key prompts are
never accepted here.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import select
import shlex
import socket
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path


def host_key(prompt: str) -> dict[str, str] | None:
    if len(prompt) > 16_384:
        return None
    host = re.match(r"The authenticity of host '([^\n]+)' can't be established", prompt)
    key = re.search(r"^([A-Z0-9_-]+) key fingerprint is:?[ \t]+(SHA256:[A-Za-z0-9+/]{43})\.?$", prompt, re.MULTILINE)
    if not host or not key or not prompt.rstrip().endswith("(yes/no/[fingerprint])?"):
        return None
    return {"host": host[1], "algorithm": key[1], "fingerprint": key[2]}


@contextmanager
def prompt_environment():
    # macOS's default temporary path can exceed AF_UNIX's 104-byte path limit.
    with tempfile.TemporaryDirectory(prefix="ava-ssh-", dir="/tmp") as directory:
        path = str(Path(directory) / "prompt.sock")
        launcher = Path(directory) / "askpass"
        launcher.write_text("#!/bin/sh\nexec " + shlex.join([sys.executable, str(Path(__file__).resolve())]) + ' "$@"\n')
        launcher.chmod(0o700)
        stopped = threading.Event()

        def reply(request: str) -> bool:
            deadline = time.monotonic() + 180
            pending = b""
            while not stopped.is_set() and time.monotonic() < deadline:
                if not select.select([sys.stdin], [], [], 0.2)[0]:
                    continue
                data = os.read(sys.stdin.fileno(), 4096)
                if not data:
                    return False
                pending += data
                if len(pending) > 4096:
                    return False
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    response = json.loads(line)
                    if isinstance(response, dict) and response.get("request") == request:
                        return response.get("accept") is True
            return False

        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(path)
            listener.listen(4)
            listener.settimeout(0.2)

            def serve():
                while not stopped.is_set():
                    try:
                        channel, _ = listener.accept()
                    except TimeoutError:
                        continue
                    except OSError:
                        break
                    with channel:
                        channel.settimeout(1)
                        try:
                            with channel.makefile("rb") as source:
                                raw = source.readline(32_769)
                            if not raw.endswith(b"\n") or len(raw) > 32_768:
                                continue
                            prompt = json.loads(raw)
                            key = host_key(prompt) if isinstance(prompt, str) else None
                            if key is None:
                                channel.sendall(b"no\n")
                                continue
                            request = secrets.token_hex(16)
                            print(json.dumps({"event": "host_key", "request": request, **key}), flush=True)
                            accepted = reply(request)
                            channel.sendall(b"yes\n" if accepted else b"no\n")
                        except (OSError, ValueError):
                            # EOF, cancellation or malformed IPC never grants trust.
                            continue

            worker = threading.Thread(target=serve, daemon=True, name="ssh-host-key")
            worker.start()
            try:
                yield dict(os.environ, SSH_ASKPASS=str(launcher), SSH_ASKPASS_REQUIRE="force",
                           AVA_SSH_ASKPASS_SOCKET=path)
            finally:
                stopped.set()
                worker.join(2)


def main() -> int:
    if len(sys.argv) != 2 or host_key(sys.argv[1]) is None:
        return 1
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
            channel.settimeout(190)
            channel.connect(os.environ["AVA_SSH_ASKPASS_SOCKET"])
            channel.sendall((json.dumps(sys.argv[1]) + "\n").encode())
            with channel.makefile("rb") as source:
                accepted = source.readline(5) == b"yes\n"
            print("yes" if accepted else "no", flush=True)
            return 0
    except (KeyError, OSError):
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
