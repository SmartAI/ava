"""Git workspaces created on the backend machine, with recoverable setup records."""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import signal
from pathlib import Path

from ava.base import ava_home


async def git(root: Path, *arguments: str, timeout: float = 15) -> str:
    environment = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0")
    process = await asyncio.create_subprocess_exec(
        "git", "--no-pager", "-c", "core.quotePath=false", *arguments,
        cwd=root, env=environment, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True,
    )

    async def drain(stream: asyncio.StreamReader | None) -> str:
        assert stream is not None
        result = bytearray()
        while data := await stream.read(32768):
            result.extend(data[:max(0, 1024 * 1024 - len(result))])
        return result.decode("utf-8", errors="replace")

    try:
        async with asyncio.timeout(timeout):
            output, error, _ = await asyncio.gather(drain(process.stdout), drain(process.stderr), process.wait())
    except BaseException:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.wait()
        raise
    if process.returncode:
        raise ValueError(error.strip() or output.strip() or "Git could not complete this operation.")
    return output.removesuffix("\n")


async def options(project: Path) -> dict:
    root = await git(project, "rev-parse", "--show-toplevel")
    commit = await git(project, "rev-parse", "--verify", "HEAD^{commit}")
    references = await git(project, "for-each-ref", "--count=500", "--format=%(refname)%00%(refname:short)", "refs/heads", "refs/remotes")
    refs = [{"name": "Current HEAD", "ref": "HEAD"}]
    refs.extend({"ref": ref, "name": name} for line in references.splitlines() if "\0" in line for ref, name in [line.split("\0", 1)])
    return {"root": root, "commit": commit, "refs": refs, "directory": str(ava_home() / "worktrees" / project.name)}


def record_path(key: str) -> Path:
    return ava_home() / "worktrees" / (key + ".json")


def save_record(record: dict) -> None:
    path = record_path(record["request_id"])
    temporary = path.with_name(path.name + ".tmp." + secrets.token_hex(8))
    try:
        with temporary.open("x", encoding="utf-8") as output:
            os.chmod(temporary, 0o600)
            json.dump(record, output, ensure_ascii=False)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


async def create(project: Path, branch: str, base: str, key: str) -> dict:
    if not re.fullmatch(r"[a-f0-9]{32}", key):
        raise ValueError("Invalid workspace request identity.")
    if not branch or branch.startswith("-"):
        raise ValueError("Enter a new branch name, without a leading dash.")
    try:
        await git(project, "check-ref-format", "refs/heads/" + branch)
    except ValueError as error:
        raise ValueError("Choose a valid Git branch name, such as ava/my-task. Spaces are not allowed.") from error
    repo = Path(await git(project, "rev-parse", "--show-toplevel"))
    base_directory = ava_home().resolve() / "worktrees"
    directory = base_directory / project.name / key
    path = record_path(key)
    saved = None
    if path.exists():
        saved = json.loads(path.read_text())
        if not isinstance(saved, dict) or not isinstance(saved.get("commit"), str):
            raise ValueError(f"Workspace setup record is damaged. Inspect {path} before retrying.")
        # Keep retries of pre-project-layout checkouts recoverable; never move
        # an existing worktree or accept an arbitrary path from a setup record.
        if saved.get("root") == str(base_directory / key):
            directory = base_directory / key
    if any(folder.is_symlink() for folder in (base_directory, directory.parent, directory)):
        raise ValueError("The workspace location is a symbolic link; it was left unchanged.")
    record = {"request_id": key, "project": str(project), "repo": str(repo),
              "root": str(directory), "cwd": str(directory / project.relative_to(repo)),
              "branch": branch, "base": base}
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if saved is not None:
        if any(saved.get(name) != value for name, value in record.items()):
            raise ValueError("This creation request already belongs to a different workspace. Start a new request.")
        record = saved
    else:
        if directory.exists():
            raise ValueError(f"A directory already exists at {directory}; it was left unchanged.")
        record["commit"] = await git(project, "rev-parse", "--verify", "--end-of-options", base + "^{commit}")
        save_record(record)
    if directory.exists():
        # An interrupted HTTP request or backend restart can leave a completed
        # checkout. Reuse it only after checking its repository, path and branch.
        actual_root = await git(directory, "rev-parse", "--show-toplevel")
        actual_common = await git(directory, "rev-parse", "--path-format=absolute", "--git-common-dir")
        expected_common = await git(project, "rev-parse", "--path-format=absolute", "--git-common-dir")
        actual_branch = await git(directory, "symbolic-ref", "--short", "HEAD")
        if Path(actual_root) != directory or Path(actual_common).resolve() != Path(expected_common).resolve() or actual_branch != branch:
            raise ValueError(f"The interrupted workspace changed. Inspect {directory} before creating a new session.")
    else:
        try:
            directory.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            await git(project, "worktree", "add", "-b", branch, "--", str(directory), record["commit"], timeout=120)
        except (OSError, ValueError, TimeoutError) as error:
            # Never force-remove partial checkouts or hooks' output on a failed
            # Git invocation. The setup record allows an explicit retry.
            raise ValueError(f"{error}\nAny checkout at {directory} was retained. Retry the same request to resume.") from error
    if not Path(record["cwd"]).is_dir():
        raise ValueError(f"This base does not contain the project's subdirectory. Checkout retained at {directory}.")
    return record
