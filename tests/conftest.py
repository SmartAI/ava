"""Shared fixtures: an isolated Ava home, a project directory, and scripted providers."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

import pytest

from ava.base import AvaError, CancelToken, ErrorKind
from ava.base.cancel import NEVER
from ava.llm import (
    Context,
    Provider,
    Selection,
    StopReason,
    StreamEvent,
    StreamEventKind,
    StreamSink,
    Usage,
)
from ava.llm.types import Item, Role, make_text_block

TEST_CONTEXT_WINDOW = 10_000


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("AVA_HOME", str(home))
    monkeypatch.setenv("HOME", str(tmp_path / "user"))
    (tmp_path / "user").mkdir()
    for name in (
        "AVA_PROVIDER",
        "AVA_MODEL",
        "AVA_EFFORT",
        "AVA_CONFIG",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    return home


@pytest.fixture
def project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    return project


def message(text: str) -> Item:
    return Item(role=Role.user, blocks=[make_text_block(text)])


class ScriptedProvider(Provider):
    """A provider whose ``stream`` replays one scripted response per call.

    Each script entry is a list of stream events, an ``AvaError`` to raise, or a callable that
    receives the context and returns either. Scripts run in order; the last one repeats.
    """

    id = "scripted"
    display_name = "Scripted"

    def __init__(self, scripts: list, *, model: str = "scripted-model") -> None:
        super().__init__(Selection(provider="scripted", model=model))
        self.context_window = TEST_CONTEXT_WINDOW
        self.scripts = scripts
        self.calls = 0
        self.contexts: list[Context] = []
        self.gate: asyncio.Event | None = None
        self.started: asyncio.Event = asyncio.Event()

    async def stream(
        self,
        context: Context,
        selected: Selection,
        sink: StreamSink,
        cancel: CancelToken = NEVER,
    ) -> StopReason:
        self.contexts.append(context)
        script = self.scripts[min(self.calls, len(self.scripts) - 1)]
        self.calls += 1
        self.started.set()
        if self.gate is not None:
            await cancel.guard(self.gate.wait())
            self.gate = None
        if callable(script):
            script = script(context)
        if isinstance(script, AvaError):
            raise script
        stop = StopReason.end_turn
        for event in script:
            if isinstance(event, StopReason):
                stop = event
                continue
            sink(event)
        sink(StreamEvent(kind=StreamEventKind.done))
        return stop


def text_response(*deltas: str, usage: Usage | None = None) -> list:
    events: list = [StreamEvent(kind=StreamEventKind.text_delta, text=delta) for delta in deltas]
    if usage is not None:
        events.append(StreamEvent(kind=StreamEventKind.usage, usage=usage))
    return events


def tool_call_response(call_id: str, name: str, arguments: str, text: str = "") -> list:
    events: list = []
    if text:
        events.append(StreamEvent(kind=StreamEventKind.text_delta, text=text))
    events.extend(
        [
            StreamEvent(kind=StreamEventKind.tool_call_start, id=call_id, name=name),
            StreamEvent(kind=StreamEventKind.tool_call_delta, id=call_id, text=arguments),
            StreamEvent(kind=StreamEventKind.tool_call_end, id=call_id),
            StopReason.tool_use,
        ]
    )
    return events


def provider_error(message: str = "provider exploded") -> AvaError:
    return AvaError(ErrorKind.provider, message)


ScriptFactory = Callable[[list], ScriptedProvider]
