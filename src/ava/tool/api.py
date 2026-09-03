"""The tool contract: a definition plus an async callable.

A recoverable failure is model-facing ``Output`` with ``is_error``; an ``AvaError`` ends the turn.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from ava.base import CancelToken
from ava.llm.types import ToolDef

ToolRun = Callable[[str, CancelToken], Awaitable["Output"]]


@dataclass(slots=True)
class Output:
    text: str
    is_error: bool = False


@dataclass(slots=True)
class Tool:
    definition: ToolDef
    run: ToolRun

    @property
    def name(self) -> str:
        return self.definition.name


def error_output(message: str) -> Output:
    return Output(text=message, is_error=True)


def parse_arguments(arguments_json: str) -> dict | str:
    """Parse tool arguments; returns a diagnostic string when the JSON is not an object."""
    try:
        parsed = json.loads(arguments_json) if arguments_json.strip() else {}
    except json.JSONDecodeError as error:
        return str(error)
    if not isinstance(parsed, dict):
        return "arguments must be a JSON object"
    return parsed


def resolve_path(cwd: Path, raw: str) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        path = cwd / path
    return Path(os_normpath(str(path)))


def os_normpath(value: str) -> str:
    import os

    return os.path.normpath(value)


def optional_int(arguments: dict, name: str) -> tuple[int | None, str | None]:
    value = arguments.get(name)
    if value is None:
        return None, None
    if isinstance(value, bool) or not isinstance(value, int):
        if isinstance(value, float) and value.is_integer():
            return int(value), None
        return None, f"'{name}' must be an integer"
    return value, None
