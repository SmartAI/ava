"""User-owned launchd/systemd supervision for one Ava home. No elevated privileges."""

from __future__ import annotations

import hashlib
import os
import plistlib
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from ava.app.backend_state import startup_lock, write_private_file
from ava.base import AvaError, ErrorKind


def service_location(home: Path) -> tuple[str, Path]:
    identity = hashlib.sha256(str(home.resolve()).encode()).hexdigest()[:20]
    if sys.platform == "darwin":
        name = "com.ava.backend." + identity
        return name, Path.home() / "Library" / "LaunchAgents" / (name + ".plist")
    if sys.platform.startswith("linux"):
        name = "ava-backend-" + identity + ".service"
        config = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
        return name, config / "systemd" / "user" / name
    raise AvaError(ErrorKind.invalid_argument, "Background startup supports macOS and Linux.")


def run(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(arguments, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise AvaError(ErrorKind.io, "Cannot contact the user service manager.", str(error)) from error
    if check and result.returncode:
        raise AvaError(ErrorKind.io, "The user service manager rejected the operation.", result.stderr[-2000:].strip())
    return result


def service_status(home: Path) -> dict[str, Any]:
    name, path = service_location(home)
    result: dict[str, Any] = {
        "manager": "launchd" if sys.platform == "darwin" else "systemd",
        "installed": path.is_file(), "loaded": False, "active": False,
        "autostart": False, "linger": None, "pid": None, "name": name,
    }
    if sys.platform == "darwin":
        domain = f"gui/{os.getuid()}"
        status = run("/bin/launchctl", "print", domain + "/" + name, check=False)
        result["loaded"] = status.returncode == 0
        result["active"] = bool(re.search(r"(?m)^\s*state = running$", status.stdout))
        pid = re.search(r"(?m)^\s*pid = (\d+)$", status.stdout)
        result["pid"] = int(pid[1]) if pid else None
        disabled = run("/bin/launchctl", "print-disabled", domain, check=False)
        result["autostart"] = path.is_file() and disabled.returncode == 0 and not re.search(
            re.escape('"' + name + '"') + r"\s*=>\s*true", disabled.stdout
        )
    else:
        status = run("systemctl", "--user", "show", name,
                     "--property=LoadState,ActiveState,MainPID,UnitFileState", check=False)
        values = dict(line.split("=", 1) for line in status.stdout.splitlines() if "=" in line)
        result.update(
            loaded=values.get("LoadState") == "loaded",
            active=values.get("ActiveState") == "active",
            autostart=values.get("UnitFileState") == "enabled",
            pid=int(values.get("MainPID", "0")) or None,
        )
        linger = run("loginctl", "show-user", str(os.getuid()), "--property=Linger", "--value", check=False)
        if linger.returncode == 0 and linger.stdout.strip() in ("yes", "no"):
            result["linger"] = linger.stdout.strip() == "yes"
    return result


def _unit_quote(value: str) -> str:
    # Unit specifiers and ExecStart variable expansion are separate from shell quoting.
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$").replace("\n", "\\n").replace("\r", "\\r") + '"'


def service_definition(home: Path) -> bytes:
    name, _ = service_location(home)
    # A service has no project cwd: folders may be moved while it is installed.
    arguments = [sys.executable, "-m", "ava.app.backend", "serve", "--no-project"]
    environment = {
        "HOME": str(Path.home()), "AVA_HOME": str(home.resolve()),
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "PYTHONUNBUFFERED": "1",
    }
    for key in ("LANG", "LC_ALL", "LC_CTYPE", "XDG_CONFIG_HOME", "AVA_PROVIDER", "AVA_MODEL", "AVA_EFFORT"):
        if key in os.environ:
            environment[key] = os.environ[key]
    if config := os.environ.get("AVA_CONFIG"):
        environment["AVA_CONFIG"] = str(Path(config).expanduser().resolve())
    if sys.platform == "darwin":
        return plistlib.dumps({
            "Label": name, "ProgramArguments": arguments,
            "WorkingDirectory": str(home.resolve()), "EnvironmentVariables": environment,
            "RunAtLoad": True, "KeepAlive": {"SuccessfulExit": False},
            "ThrottleInterval": 3, "ExitTimeOut": 15,
            "StandardOutPath": "/dev/null", "StandardErrorPath": str(home / "backend.log"),
        })
    # Environment= does not expand $, while ExecStart does. Escape each context correctly.
    lines = ["[Unit]", "Description=Ava background agent", "StartLimitIntervalSec=60", "StartLimitBurst=5",
             "", "[Service]", "Type=exec", "WorkingDirectory=/",
             "ExecStart=" + " ".join(_unit_quote(arg) for arg in arguments),
             "Restart=on-failure", "RestartSec=3", "TimeoutStopSec=20", "UMask=0077"]
    lines += ["Environment=" + _unit_quote(key + "=" + value).replace("$$", "$") for key, value in environment.items()]
    lines += ["StandardOutput=null", "StandardError=journal", "", "[Install]", "WantedBy=default.target", ""]
    return "\n".join(lines).encode()


def start_installed(home: Path, *, restart: bool = False) -> bool:
    name, path = service_location(home)
    if not path.is_file():
        return False
    if sys.platform == "darwin":
        domain = f"gui/{os.getuid()}"
        state = service_status(home)
        if not state["autostart"]:
            raise AvaError(ErrorKind.io, "Ava's startup service is disabled. Enable it in background settings.")
        if not state["loaded"]:
            run("/bin/launchctl", "bootstrap", domain, str(path))
        run("/bin/launchctl", "kickstart", *(["-k"] if restart else []), domain + "/" + name)
    else:
        run("systemctl", "--user", "restart" if restart else "start", name)
    return True


def _unload(home: Path) -> None:
    name, _ = service_location(home)
    if sys.platform == "darwin":
        if service_status(home)["loaded"]:
            run("/bin/launchctl", "bootout", f"gui/{os.getuid()}/{name}")
    else:
        if service_status(home)["loaded"]:
            run("systemctl", "--user", "stop", name)
        run("systemctl", "--user", "disable", name)


def _wait_ready(home: Path) -> dict[str, Any]:
    from ava.app.backend import connect

    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        info = connect(home)
        if info is not None:
            return info
        time.sleep(0.05)
    raise AvaError(ErrorKind.network, "Background service did not become ready. Check its service log.")


def install_service(home: Path, *, force: bool = False, defer_if_busy: bool = False) -> dict[str, Any]:
    from ava.app.backend import connect, stop

    with startup_lock(home):
        name, path = service_location(home)
        if path.is_symlink():
            raise AvaError(ErrorKind.io, "Ava service configuration must not be a symbolic link.")
        definition = service_definition(home)
        previous = path.read_bytes() if path.exists() else None
        state = service_status(home)
        existing = connect(home)
        if previous == definition and state["active"] and existing and state["pid"] == existing["pid"]:
            if not state["autostart"]:
                if sys.platform == "darwin":
                    run("/bin/launchctl", "enable", f"gui/{os.getuid()}/{name}")
                else:
                    run("systemctl", "--user", "enable", str(path))
            return service_status(home)
        # Validate the manager before interrupting any existing idle backend.
        if sys.platform == "darwin":
            run("/bin/launchctl", "print", f"gui/{os.getuid()}")
        else:
            run("systemctl", "--user", "show-environment")
        if not stop(home, force=force, defer_if_busy=defer_if_busy):
            return {**state, "update_pending": True}
        if state["loaded"]:
            _unload(home)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        log = os.open(home / "backend.log", os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        os.close(log)
        try:
            write_private_file(path, definition)
            if sys.platform == "darwin":
                if not state["autostart"] and previous is not None:
                    run("/bin/launchctl", "enable", f"gui/{os.getuid()}/{name}")
            else:
                run("systemctl", "--user", "daemon-reload")
                run("systemctl", "--user", "enable", str(path))
            start_installed(home)
            _wait_ready(home)
            return service_status(home)
        except (AvaError, OSError):
            _unload(home)
            if previous is None:
                path.unlink(missing_ok=True)
            else:
                write_private_file(path, previous)
                if sys.platform != "darwin":
                    run("systemctl", "--user", "daemon-reload")
                    run("systemctl", "--user", "enable", str(path))
                start_installed(home)
            raise


def uninstall_service(home: Path, *, force: bool = False) -> dict[str, Any]:
    from ava.app.backend import stop

    with startup_lock(home):
        _, path = service_location(home)
        state = service_status(home)
        if not state["installed"] and not state["loaded"]:
            return state
        stop(home, force=force)
        _unload(home)
        path.unlink(missing_ok=True)
        if sys.platform != "darwin":
            run("systemctl", "--user", "daemon-reload")
        return service_status(home)
