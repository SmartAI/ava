"""The command-line surface.

    ava                                  serve the loopback Web UI on the default port
    ava --serve[=PORT]                   serve the Web UI (0 selects an unused port)
    ava -p [OPTIONS] PROMPT...           one-shot: run one turn, print the answer, exit
    ava session dump FILE                every record as logical JSONL on stdout

stdout carries only the model's answer (``-p``), the served URL (``--serve``), or a subcommand's
output. Diagnostics go to stderr. Exit codes: 0 completed; 1 provider, tool, or session error;
2 usage error; 130 interrupted.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path

from ava import __version__
from ava.agent import Agent, CompactionOptions, Status
from ava.app.attach import expected_media_type, load_attachment
from ava.base import AvaError, ErrorKind, find_project_root
from ava.llm import (
    AuthRequirement,
    Item,
    Role,
    Selection,
    SelectionOverride,
    make_text_block,
    provider_from_environment,
)
from ava.session import (
    AssistantChunk,
    CompactionFailed,
    DriveError,
    Event,
    Log,
    OpenMode,
    SessionCandidate,
    SessionStart,
    TurnEnd,
    TurnEndReason,
    canonical_working_directory,
    default_session_root,
    discover_sessions_in,
)
from ava.session import (
    Selection as SelectionEvent,
)
from ava.session.codec import encode_record

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_INTERRUPTED = 130


def _print_error(error: AvaError) -> None:
    print(f"ava: {error}", file=sys.stderr)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ava", add_help=True, description="A coding-agent harness."
    )
    parser.add_argument("-v", "--version", action="version", version=f"ava {__version__}")
    parser.add_argument(
        "-p", "--print", action="store_true", help="one-shot: run one turn and print the answer"
    )
    parser.add_argument(
        "--serve", nargs="?", const=str(8777), metavar="PORT", help="serve the loopback Web UI"
    )
    parser.add_argument(
        "-c",
        "--continue",
        dest="continue_latest",
        action="store_true",
        help="resume the most recent session",
    )
    parser.add_argument("--resume", nargs="?", const="", metavar="ID", help="resume a past session")
    parser.add_argument(
        "--session", metavar="PATH", help="create or resume this exact session log (-p only)"
    )
    parser.add_argument("--provider", metavar="NAME")
    parser.add_argument("--model", metavar="ID_OR_ALIAS")
    parser.add_argument("--effort", metavar="LEVEL")
    parser.add_argument("--no-compact", action="store_true")
    parser.add_argument("--compact-threshold", type=int, metavar="PCT")
    parser.add_argument(
        "--file", action="append", default=[], metavar="PATH", help="attach a UTF-8 text file"
    )
    parser.add_argument(
        "--image",
        action="append",
        default=[],
        metavar="PATH",
        help="attach a PNG, JPEG, GIF, or WebP image",
    )
    parser.add_argument("prompt", nargs="*", help="prompt words; @PATH attaches a file")
    return parser


@dataclass(slots=True)
class SessionPlan:
    resume: SessionCandidate | None = None
    create_path: Path | None = None


def _choose_explicit_session(requested: str, cwd: Path) -> SessionPlan:
    path = Path(requested)
    if not path.is_absolute():
        path = cwd / path
    path = Path(*path.parts)  # lexically normal
    if path.is_symlink():
        raise AvaError(
            ErrorKind.invalid_argument, f"explicit session path '{path}' is a symbolic link"
        )
    if not path.exists():
        if not path.parent.is_dir():
            raise AvaError(
                ErrorKind.io,
                f"explicit session parent is not an accessible directory: '{path.parent}'",
            )
        return SessionPlan(create_path=path)
    header = Log.read_header(path)
    canonical = canonical_working_directory(cwd)
    if header.cwd != str(canonical):
        raise AvaError(
            ErrorKind.invalid_argument,
            "session belongs to a different working directory",
            header.cwd,
        )
    return SessionPlan(resume=SessionCandidate(path=path, header=header))


def _choose_session(args: argparse.Namespace, cwd: Path) -> SessionPlan:
    if args.session is not None:
        return _choose_explicit_session(args.session, cwd)
    if not args.continue_latest and args.resume is None:
        return SessionPlan()
    candidates = discover_sessions_in(default_session_root(), cwd)
    if args.resume:
        found = next(
            (candidate for candidate in candidates if candidate.header.id == args.resume), None
        )
        if found is None:
            raise AvaError(
                ErrorKind.not_found,
                f"session '{args.resume}' was not found for this working directory",
            )
        return SessionPlan(resume=found)
    if not candidates:
        raise AvaError(ErrorKind.not_found, "no sessions were found for this working directory")
    if args.continue_latest:
        return SessionPlan(resume=candidates[0])
    print("Sessions for this working directory:", file=sys.stderr)
    for index, candidate in enumerate(candidates, start=1):
        print(f"  {index}) {candidate.header.id}", file=sys.stderr)
    print(f"Select a session [1-{len(candidates)}]: ", end="", file=sys.stderr, flush=True)
    line = sys.stdin.readline()
    if not line:
        raise AvaError(ErrorKind.invalid_argument, "session selection was cancelled")
    choice = line.strip()
    if not choice.isdigit() or not 1 <= int(choice) <= len(candidates):
        raise AvaError(
            ErrorKind.invalid_argument, "session selection is outside the displayed range"
        )
    return SessionPlan(resume=candidates[int(choice) - 1])


def _resumed_selection(log: Log) -> Selection:
    header = log.loaded_events[0].payload
    assert isinstance(header, SessionStart)
    selected = Selection(provider=header.provider, model=header.model)
    for event in log.loaded_events:
        if isinstance(event.payload, SelectionEvent):
            selected = Selection(event.payload.provider, event.payload.model, event.payload.effort)
    return selected


def _newest_turn_was_interrupted(log: Log) -> bool:
    reason = None
    for event in log.loaded_events:
        if isinstance(event.payload, TurnEnd):
            reason = event.payload.reason
    return reason == TurnEndReason.interrupted


def _one_shot_input(args: argparse.Namespace, cwd: Path, argv: list[str]) -> Item:
    """Attachments keep command-line order and precede the final question text."""
    separator = argv.index("--") if "--" in argv else len(argv)
    literal_words = set(argv[separator + 1 :])
    attachments: list[tuple[bool, str]] = [(False, path) for path in args.file] + [
        (True, path) for path in args.image
    ]
    words: list[str] = []
    for word in args.prompt:
        if word.startswith("@") and len(word) > 1 and word not in literal_words:
            path = word[1:]
            attachments.append((bool(expected_media_type(Path(path).suffix)), path))
        else:
            words.append(word)
    prompt = " ".join(words)
    if not prompt and not sys.stdin.isatty():
        prompt = sys.stdin.read().strip()
    if not prompt:
        raise AvaError(
            ErrorKind.invalid_argument, "a prompt is required; pass -p followed by a prompt"
        )
    root = find_project_root(cwd)
    item = Item(role=Role.user)
    for image, path in attachments:
        item.blocks.append(load_attachment(cwd, path, image=image, root=root))
    item.blocks.append(make_text_block(prompt))
    return item


def _render_one_shot(event: Event) -> None:
    payload = event.payload
    if isinstance(payload, AssistantChunk):
        sys.stdout.write(payload.delta)
        sys.stdout.flush()
    elif isinstance(payload, SelectionEvent) and payload.warning:
        print(f"ava: {payload.warning}", file=sys.stderr)
    elif isinstance(payload, CompactionFailed | DriveError):
        print(f"ava: {payload.message}", file=sys.stderr)


async def _run_one_shot(agent: Agent, item: Item) -> int:
    replaying = True
    with agent.subscribe(lambda event: None if replaying else _render_one_shot(event)):
        replaying = False
        try:
            await agent.followup(item)
            if agent.status == Status.paused:
                agent.resume()
            await agent.drive()
        except AvaError as error:
            _print_error(error)
            return EXIT_USAGE if error.kind == ErrorKind.invalid_argument else EXIT_ERROR
    print(flush=True)
    return EXIT_OK


def _session_dump(path: Path) -> int:
    log = Log.open(path, OpenMode.read_only)
    for event in log.loaded_events:
        sys.stdout.write(encode_record(event) + "\n")
    return EXIT_OK


async def _serve(
    cwd: Path, port: int, options: CompactionOptions, selection: SelectionOverride
) -> int:
    from ava.app.web.server import bind, create_app, serve

    sock = bind(port)
    app = create_app(cwd, options, selection)
    print(f"http://127.0.0.1:{sock.getsockname()[1]}", flush=True)
    await serve(app, sock)
    return EXIT_OK


def run(argv: list[str]) -> int:
    if argv[:1] == ["session"]:
        if len(argv) != 3 or argv[1] != "dump":
            print("ava: usage: ava session dump FILE", file=sys.stderr)
            return EXIT_USAGE
        try:
            return _session_dump(Path(argv[2]))
        except AvaError as error:
            _print_error(error)
            return EXIT_ERROR
    parser = _parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exit_code:
        return int(exit_code.code or 0) if exit_code.code in (0, None) else EXIT_USAGE
    cwd = Path.cwd()
    options = CompactionOptions(enabled=not args.no_compact)
    if args.compact_threshold is not None:
        if not 1 <= args.compact_threshold <= 100:
            print("ava: --compact-threshold must be an integer from 1 through 100", file=sys.stderr)
            return EXIT_USAGE
        options.threshold_percent = args.compact_threshold
    for name in ("provider", "model", "effort"):
        if getattr(args, name) == "":
            print(f"ava: --{name} requires a non-empty value", file=sys.stderr)
            return EXIT_USAGE
    selection = SelectionOverride(provider=args.provider, model=args.model, effort=args.effort)
    resuming = args.continue_latest or args.resume is not None or args.session is not None

    serve_port: int | None = None
    if args.serve is not None:
        if not args.serve.isdigit() or int(args.serve) > 65535:
            print("ava: --serve port must be an integer from 0 through 65535", file=sys.stderr)
            return EXIT_USAGE
        serve_port = int(args.serve)
    elif not args.print and not args.prompt and not resuming:
        serve_port = 8777
    if serve_port is not None:
        if resuming or args.print or args.prompt:
            print(
                "ava: --serve cannot be combined with -p, --session, -c, or --resume",
                file=sys.stderr,
            )
            return EXIT_USAGE
        try:
            return asyncio.run(_serve(cwd, serve_port, options, selection))
        except AvaError as error:
            _print_error(error)
            return EXIT_ERROR
        except KeyboardInterrupt:
            return EXIT_INTERRUPTED
    if not args.print:
        print(
            "ava: prompt arguments require -p; run without arguments for the Web UI",
            file=sys.stderr,
        )
        return EXIT_USAGE
    if args.resume == "" and not sys.stdout.isatty():
        print(
            "ava: --resume needs a terminal to choose a session; pass --resume=ID or use -c for the most recent session",
            file=sys.stderr,
        )
        return EXIT_USAGE

    try:
        item = _one_shot_input(args, cwd, argv)
    except AvaError as error:
        _print_error(error)
        return EXIT_USAGE
    try:
        plan = _choose_session(args, cwd)
    except AvaError as error:
        _print_error(error)
        usage = args.session is None and error.kind == ErrorKind.invalid_argument
        return EXIT_USAGE if usage else EXIT_ERROR
    try:
        durable_selection: Selection | None = None
        resumed_log: Log | None = None
        if plan.resume is not None:
            resumed_log = Log.open(plan.resume.path, OpenMode.repair, cwd)
            durable_selection = _resumed_selection(resumed_log)
            print(f"ava: resuming {plan.resume.path}", file=sys.stderr)
            if _newest_turn_was_interrupted(resumed_log):
                print(
                    "ava: the previous run was interrupted; repaired the session before resuming",
                    file=sys.stderr,
                )
        provider = provider_from_environment(selection, durable_selection, AuthRequirement.required)
        if resumed_log is not None:
            agent = Agent.reopen(provider, cwd, resumed_log, options)
        elif plan.create_path is not None:
            agent = Agent.create_at(provider, cwd, plan.create_path, options)
        else:
            agent = Agent.create(provider, cwd, options)
    except AvaError as error:
        _print_error(error)
        return EXIT_ERROR
    try:
        return asyncio.run(_run_one_shot(agent, item))
    except KeyboardInterrupt:
        return EXIT_INTERRUPTED
    finally:
        if plan.create_path is None and plan.resume is None and agent.session_path is not None:
            print(f"ava: session {agent.session_path}", file=sys.stderr)
        agent.close()


def main() -> None:
    try:
        sys.exit(run(sys.argv[1:]))
    except KeyboardInterrupt:
        sys.exit(EXIT_INTERRUPTED)


if __name__ == "__main__":
    main()
