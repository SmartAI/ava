"""Asynchronous local Git review and explicit index/commit operations."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Property, QObject, QProcess, QProcessEnvironment, QTimer, Signal, Slot


class GitReview(QObject):
    changed = Signal()
    filesChanged = Signal()
    closed = Signal()
    committed = Signal(str)
    OUTPUT_LIMIT = 1024 * 1024

    def __init__(self, project: str, parent: QObject, *, remote: dict | None = None, local_cwd: str = "") -> None:
        super().__init__(parent)
        self.project = project
        self.project_id = ""
        self.remote = remote
        self._local_cwd = local_cwd or project
        self._root = project
        self._files: list[dict] = []
        self._state: dict = dict(
            root=project,
            branch="",
            selected="",
            diff="",
            error="",
            refreshError="",
            notice="",
            busy=False,
            loading=True,
            ready=False,
            unborn=False,
        )
        self._jobs: dict[str, QProcess] = {}
        self._processes: set[QProcess] = set()
        self._closing = False
        self._closed_emitted = False
        QTimer.singleShot(0, self._discover)

    @Property(list, notify=filesChanged)
    def files(self) -> list:
        return self._files

    @Property("QVariantMap", notify=changed)  # type: ignore[arg-type]
    def state(self) -> dict:
        return self._state

    def _set(self, **values) -> None:
        if any(self._state.get(key) != value for key, value in values.items()):
            self._state.update(values)
            self.changed.emit()

    def _run(
        self,
        job: str,
        arguments: list[str],
        callback: Callable[[bytes, str], None],
        *,
        mutation: bool = False,
        diff_exit: bool = False,
    ) -> None:
        if self._closing:
            return
        old = self._jobs.get(job)
        if old:
            old.kill()
        process = QProcess(self)
        self._jobs[job] = process
        self._processes.add(process)
        arguments = [
                "--no-pager",
                "--literal-pathspecs",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.quotePath=false",
                *arguments,
            ]
        if self.remote:
            from .ssh import workspace_command

            command = workspace_command(self.remote, self._root, "git", arguments)
            process.setWorkingDirectory(self._local_cwd)
            process.setProgram(command[0])
            process.setArguments(command[1:])
        else:
            process.setWorkingDirectory(self._root)
            process.setProgram("git")
            process.setArguments(arguments)
        environment = QProcessEnvironment.systemEnvironment()
        environment.insert("GIT_TERMINAL_PROMPT", "0")
        environment.insert("GIT_OPTIONAL_LOCKS", "0")
        process.setProcessEnvironment(environment)
        output = bytearray()
        errors = bytearray()
        problem = ""
        finished = False
        deadline = QTimer(process)
        deadline.setSingleShot(True)

        def read():
            nonlocal problem
            data = process.readAllStandardOutput().data()
            remaining = max(0, self.OUTPUT_LIMIT - len(output))
            output.extend(data[:remaining])
            errors.extend(process.readAllStandardError().data())
            del errors[:-4096]
            if len(data) > remaining and not mutation:
                problem = (
                    "Output exceeds 1 MiB. Narrow the change or open the file to inspect more."
                )
                process.kill()

        def timeout():
            nonlocal problem
            problem = "Git did not respond within 10 seconds. Try refreshing."
            process.kill()

        def finish(code: int):
            nonlocal finished
            if finished:
                return
            finished = True
            deadline.stop()
            read()
            current = self._jobs.get(job) is process
            if current:
                del self._jobs[job]
            self._processes.discard(process)
            process.deleteLater()
            error = problem or (
                errors.decode("utf-8", "replace").strip() or "Git operation failed."
                if code not in ((0, 1) if diff_exit else (0,))
                else ""
            )
            if self.remote and code == 255:
                error = "SSH connection lost. Reconnect and refresh to check the result before retrying.\n" + error
            if current:
                if mutation:
                    self._set(busy=False)
                if not self._closing:
                    callback(bytes(output), error)
            self._finish_close()

        def failed(error):
            if error == QProcess.ProcessError.FailedToStart:
                nonlocal problem
                problem = "Cannot start Git: " + process.errorString()
                finish(-1)

        process.readyReadStandardOutput.connect(read)
        process.readyReadStandardError.connect(read)
        process.finished.connect(lambda code, _: finish(code))
        process.errorOccurred.connect(failed)
        deadline.timeout.connect(timeout)
        process.start()
        if not mutation:
            deadline.start(10_000)

    def _discover(self) -> None:
        self._run("status", ["rev-parse", "--show-toplevel"], self._discovered)

    def _discovered(self, output: bytes, error: str) -> None:
        if error:
            self._set(
                loading=False,
                error="This folder is not a Git repository."
                if "not a git repository" in error.lower()
                else error,
            )
            return
        self._root = output.decode("utf-8", "replace").rstrip("\n")
        self._set(root=self._root, ready=True, error="")
        self.refresh()

    @Slot()
    def refresh(self) -> None:
        if self._state["busy"] or "status" in self._jobs or self._closing:
            return
        if not self._state["ready"]:
            self._discover()
            return
        self._run(
            "status",
            ["status", "--porcelain=v1", "--branch", "-z", "--untracked-files=all"],
            self._status,
        )

    def _status(self, output: bytes, error: str) -> None:
        self._set(loading=False, refreshError=error)
        if error:
            return
        fields = iter(output.split(b"\0"))
        rows: list[dict] = []
        branch = ""
        for field in fields:
            if field.startswith(b"## "):
                branch = field[3:].decode("utf-8", "replace")
                continue
            if len(field) < 4:
                continue
            status = field[:2].decode("ascii", "replace")
            path = field[3:].decode("utf-8", "replace")
            previous = (
                next(fields, b"").decode("utf-8", "replace")
                if "R" in status or "C" in status
                else ""
            )
            for index, scope in ((0, "staged"), (1, "worktree")):
                code = status[index]
                if code == " " or scope == "staged" and status == "??":
                    continue
                rows.append(
                    dict(
                        id=scope + ":" + path,
                        path=path,
                        previous=previous,
                        scope=scope,
                        status=code,
                        untracked=status == "??",
                    )
                )
        rows.sort(key=lambda row: (row["scope"] != "worktree", row["path"].casefold()))
        if rows != self._files:
            self._files = rows
            self.filesChanged.emit()
        self._set(
            branch=branch, unborn=branch.startswith(("No commits yet on ", "Initial commit on "))
        )
        selected = self._state["selected"]
        if selected and any(row["id"] == selected for row in rows):
            self.select(selected)
        elif rows:
            self.select(rows[0]["id"])
        else:
            pending = self._jobs.pop("diff", None)
            if pending:
                pending.kill()
            self._set(selected="", diff="", notice="")

    @Slot(str)
    def select(self, identity: str) -> None:
        row = next((row for row in self._files if row["id"] == identity), None)
        if not row or self._closing:
            return
        if identity != self._state["selected"]:
            self._set(selected=identity, diff="", notice="")
        arguments = ["diff", "--no-ext-diff", "--no-textconv", "--no-color"]
        if row["untracked"]:
            arguments += ["--no-index", "--", "/dev/null", row["path"]]
        else:
            if row["scope"] == "staged":
                arguments += ["--cached"]
            arguments += ["--", row["path"]]
            if row["previous"]:
                arguments.append(row["previous"])

        def loaded(output, error):
            if self._state["selected"] == identity:
                self._set(diff=output.decode("utf-8", "replace"), notice=error)

        self._run("diff", arguments, loaded, diff_exit=bool(row["untracked"]))

    @Slot()
    def toggleStage(self) -> None:
        if self._state["busy"]:
            return
        row = next((row for row in self._files if row["id"] == self._state["selected"]), None)
        if not row:
            return
        paths = [row["path"]] + ([row["previous"]] if row["previous"] else [])
        if row["scope"] == "worktree":
            arguments = ["add", "-A", "--", *paths]
        elif self._state["unborn"]:
            arguments = ["rm", "--cached", "--ignore-unmatch", "--", *paths]
        else:
            arguments = ["restore", "--staged", "--", *paths]
        self._set(busy=True, error="")

        def done(_, error):
            self._set(error=error)
            if not error:
                self.refresh()

        self._run("mutation", arguments, done, mutation=True)

    @Slot(str)
    def commit(self, message: str) -> None:
        if (
            self._state["busy"]
            or not message.strip()
            or "\0" in message
            or not any(row["scope"] == "staged" for row in self._files)
        ):
            return
        self._set(busy=True, error="")

        def done(output, error):
            self._set(error=error)
            if not error:
                self.committed.emit(output.decode("utf-8", "replace").split("\n")[0])
                self.refresh()

        self._run("mutation", ["commit", "-m", message.strip()], done, mutation=True)

    @Slot()
    def close(self) -> None:
        self._closing = True
        # A tab can close while a commit hook runs; let an explicit write finish.
        for name, process in list(self._jobs.items()):
            if name != "mutation":
                process.kill()
        self._finish_close()

    def _finish_close(self) -> None:
        if self._closing and not self._processes and not self._closed_emitted:
            self._closed_emitted = True
            self.closed.emit()
