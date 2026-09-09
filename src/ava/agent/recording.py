"""Opt-in recordings of one headless drive, and strict offline replay of its I/O.

The session log remains authoritative history. This separate, versioned recording
captures provider stream boundaries and tool responses that history alone cannot
reconstruct. Replays never instantiate a live provider or a real tool.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_serializer,
    field_validator,
    model_validator,
)

from ava.agent.agent import Agent
from ava.agent.state import CompactionOptions
from ava.base import AvaError, CancelToken, ErrorKind
from ava.base.cancel import NEVER
from ava.llm import (
    Context,
    Item,
    ModelCapabilities,
    Provider,
    Selection,
    StopReason,
    StreamEvent,
    StreamSink,
    make_tool_result_block,
)
from ava.session.codec import (
    block_from_wire,
    block_to_wire,
    item_from_wire,
    item_to_wire,
    payload_to_wire,
)
from ava.session.event import ToolsAdvertised
from ava.tool import Output, Tool

MAX_RECORD_BYTES = 32_000_000
MAX_RECORDING_BYTES = 128_000_000


class _Record(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Header(_Record):
    kind: Literal["header"] = "header"
    version: Literal[1] = 1
    selection: Selection
    context_window: int
    capabilities: ModelCapabilities
    input: dict[str, Any]
    compaction: CompactionOptions

    @field_validator("input")
    @classmethod
    def valid_input(cls, value: dict[str, Any]) -> dict[str, Any]:
        try:
            item_from_wire(value)
        except AvaError as error:
            raise ValueError(error.message) from error
        return value


class _Failure(_Record):
    kind: ErrorKind
    message: str
    detail: str
    recoverable: bool


class _Exchange(_Record):
    kind: Literal["model", "tool"]
    request: dict[str, Any]
    events: list[StreamEvent] = Field(default_factory=list)
    stop: StopReason | None = None
    output: Output | None = None
    error: dict[str, Any] | None = None

    @field_serializer("output")
    def serialize_output(self, value: Output | None) -> dict[str, Any] | None:
        if value is None:
            return None
        wire = block_to_wire(make_tool_result_block("", value.text, value.is_error, value.attachments))
        wire.pop("kind")
        # Keep the original text-only recording shape. Images use the same portable
        # base64 representation as session history, including deferred image data.
        wire.update(text=value.text, is_error=value.is_error)
        return wire

    @field_validator("output", mode="before")
    @classmethod
    def deserialize_output(cls, value: Any) -> Any:
        if not isinstance(value, dict) or "attachments" not in value:
            return value
        try:
            block = block_from_wire({"kind": "tool_result", "attachments": value["attachments"]})
        except AvaError as error:
            raise ValueError(error.message) from error
        return {**value, "attachments": block.attachments}

    @field_validator("error")
    @classmethod
    def valid_error(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is not None:
            _Failure.model_validate(value)
        return value

    @model_validator(mode="after")
    def valid_exchange(self) -> _Exchange:
        if self.kind == "tool":
            if set(self.request) != {"name", "arguments"} or not all(
                isinstance(v, str) for v in self.request.values()
            ):
                raise ValueError("tool request requires a name and argument string")
        else:
            if set(self.request) != {"selection", "system_prompt", "items", "tools"}:
                raise ValueError(
                    "model request requires selection, system_prompt, items, and tools"
                )
            TypeAdapter(Selection).validate_python(self.request["selection"])
            if not isinstance(self.request["system_prompt"], str) or not isinstance(
                self.request["items"], list
            ):
                raise ValueError("invalid model context")
            try:
                from ava.session.codec import decode_known

                for item in self.request["items"]:
                    item_from_wire(item)
                decode_known("tools/advertised", {"tools": self.request["tools"]})
            except (AvaError, AttributeError, TypeError) as error:
                raise ValueError("invalid model context") from error
        return self


class _End(_Record):
    kind: Literal["end"] = "end"
    error: dict[str, Any] | None = None

    @field_validator("error")
    @classmethod
    def valid_error(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is not None:
            _Failure.model_validate(value)
        return value


_ADAPTER: TypeAdapter[_Header | _Exchange | _End] = TypeAdapter(
    Annotated[_Header | _Exchange | _End, Field(discriminator="kind")]
)
_STREAM_ADAPTER = TypeAdapter(StreamEvent)


def _error(error: AvaError) -> dict[str, Any]:
    return {
        "kind": error.kind.value,
        "message": error.message,
        "detail": error.detail,
        "recoverable": error.recoverable,
    }


def _raise_error(error: dict[str, Any]) -> None:
    raise AvaError(
        ErrorKind(error["kind"]),
        error["message"],
        error["detail"],
        recoverable=error["recoverable"],
    )


def _request(context: Context, selected: Selection) -> dict[str, Any]:
    return {
        "selection": asdict(selected),
        "system_prompt": context.system_prompt,
        "items": [item_to_wire(item) for item in context.items],
        "tools": payload_to_wire(ToolsAdvertised(tools=context.tools))["tools"],
    }


def _difference(expected: Any, actual: Any, path: str = "request") -> str | None:
    """Return the first differing field, without leaking its potentially private value."""
    if type(expected) is not type(actual):
        return path
    if isinstance(expected, dict):
        if expected.keys() != actual.keys():
            return path
        for key in expected:
            if found := _difference(expected[key], actual[key], f"{path}.{key}"):
                return found
    elif isinstance(expected, list):
        if len(expected) != len(actual):
            return f"{path}.length"
        for index, (left, right) in enumerate(zip(expected, actual, strict=True)):
            if found := _difference(left, right, f"{path}[{index}]"):
                return found
    elif expected != actual:
        return path
    return None


class Recording:
    """Record a single new drive. Call finish only after the drive has stopped.

    Files are private, exclusive-create, and bounded. A crash leaves a recording
    without an end marker; offline replay rejects it instead of claiming success.
    No credentials are captured, but prompts, tool outputs, and reasoning may be sensitive.
    """

    def __init__(
        self,
        path: Path,
        provider: Provider,
        item: Item,
        options: CompactionOptions,
    ) -> None:
        self._fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        self._size = 0
        self._finished = False
        self._provider = provider
        try:
            self._append(
                _Header(
                    selection=provider.selection,
                    context_window=provider.context_window,
                    capabilities=provider.capabilities(provider.selection.model),
                    input=item_to_wire(item),
                    compaction=options,
                )
            )
        except BaseException:
            self.close()
            raise

    def _append(self, record: _Record) -> None:
        if self._fd < 0 or self._finished:
            raise AvaError(ErrorKind.io, "recording is already closed")
        data = (record.model_dump_json() + "\n").encode()
        if len(data) > MAX_RECORD_BYTES or self._size + len(data) > MAX_RECORDING_BYTES:
            raise AvaError(ErrorKind.io, "recording size limit exceeded")
        try:
            view = memoryview(data)
            while view:
                written = os.write(self._fd, view)
                if written == 0:
                    raise OSError("recording write made no progress")
                view = view[written:]
            self._size += len(data)
        except OSError as error:
            raise AvaError(ErrorKind.io, "cannot write recording", str(error)) from error

    def provider(self) -> Provider:
        return _RecordingProvider(self._provider, self._append)

    def tools(self, tools: list[Tool]) -> list[Tool]:
        def wrap(tool: Tool) -> Tool:
            async def run(arguments: str, cancel: CancelToken) -> Output:
                exchange = _Exchange(
                    kind="tool", request={"name": tool.name, "arguments": arguments}
                )
                try:
                    exchange.output = await tool.run(arguments, cancel)
                except AvaError as error:
                    exchange.error = _error(error)
                    raise
                finally:
                    self._append(exchange)
                return exchange.output

            return Tool(tool.definition, run)

        return [wrap(tool) for tool in tools]

    def finish(self, error: AvaError | None = None) -> None:
        self._append(_End(error=_error(error) if error else None))
        os.fsync(self._fd)
        self._finished = True

    def close(self) -> None:
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1


class _RecordingProvider(Provider):
    def __init__(self, provider: Provider, append: Callable[[_Record], None]) -> None:
        super().__init__(provider.selection)
        self.id = provider.id
        self.display_name = provider.display_name
        self.context_window = provider.context_window
        self.model_aliases = provider.model_aliases
        self.model_overrides = provider.model_overrides
        self.selection_model_may_be_alias = provider.selection_model_may_be_alias
        self.remembers_selection = provider.remembers_selection
        self._provider = provider
        self._append = append

    def capabilities(self, model: str) -> ModelCapabilities:
        return self._provider.capabilities(model)

    async def list_models(self, cancel: CancelToken = NEVER) -> list[str]:
        return await self._provider.list_models(cancel)

    async def stream(
        self,
        context: Context,
        selected: Selection,
        sink: StreamSink,
        cancel: CancelToken = NEVER,
    ) -> StopReason:
        exchange = _Exchange(kind="model", request=_request(context, selected))
        size = 0

        def record(event: StreamEvent) -> None:
            nonlocal size
            # Copy before the provider can reuse/mutate its event or usage object.
            data = _STREAM_ADAPTER.dump_json(event)
            size += len(data)
            if size > MAX_RECORD_BYTES // 2:
                raise AvaError(ErrorKind.io, "recorded provider stream size limit exceeded")
            exchange.events.append(_STREAM_ADAPTER.validate_json(data))
            sink(event)

        try:
            exchange.stop = await self._provider.stream(context, selected, record, cancel)
        except AvaError as error:
            exchange.error = _error(error)
            raise
        finally:
            self._append(exchange)
        return exchange.stop

    async def aclose(self) -> None:
        await self._provider.aclose()


class _ReplayProvider(Provider):
    def __init__(self, header: _Header, take: Callable[[str, dict[str, Any]], _Exchange]) -> None:
        super().__init__(header.selection)
        self.id = header.selection.provider
        self.context_window = header.context_window
        self._capabilities = header.capabilities
        self._take = take

    def capabilities(self, model: str) -> ModelCapabilities:
        return self._capabilities

    async def stream(
        self,
        context: Context,
        selected: Selection,
        sink: StreamSink,
        cancel: CancelToken = NEVER,
    ) -> StopReason:
        exchange = self._take("model", _request(context, selected))
        for event in exchange.events:
            sink(event)
        if exchange.error:
            _raise_error(exchange.error)
        if exchange.stop is None:
            raise AvaError(ErrorKind.parse, "recorded model exchange has no stop reason")
        return exchange.stop


async def replay_recording(path: Path) -> dict[str, Any]:
    """Re-execute the agent loop using recorded I/O; stop on the first divergence.

    This tests the runtime, not model quality. It does not recreate filesystem
    changes, execute recorded commands, or claim to reproduce OS scheduling.
    """
    records: list[_Header | _Exchange | _End] = []
    total = 0
    try:
        with path.open("rb") as source:
            while line := source.readline(MAX_RECORD_BYTES + 1):
                total += len(line)
                if len(line) > MAX_RECORD_BYTES or total > MAX_RECORDING_BYTES:
                    raise AvaError(ErrorKind.parse, "recording size limit exceeded")
                records.append(_ADAPTER.validate_json(line))
    except (OSError, ValidationError) as error:
        raise AvaError(ErrorKind.parse, "cannot read recording", str(error)) from error
    if len(records) < 3 or not isinstance(records[0], _Header) or not isinstance(records[-1], _End):
        raise AvaError(
            ErrorKind.parse, "recording is incomplete: header, exchanges, and end required"
        )
    header, end = records[0], records[-1]
    exchanges = records[1:-1]
    if not all(isinstance(record, _Exchange) for record in exchanges):
        raise AvaError(ErrorKind.parse, "unexpected recording header or end")
    first = exchanges[0]
    if not isinstance(first, _Exchange) or first.kind != "model":
        raise AvaError(ErrorKind.parse, "recording must start with a model request")
    position = 0
    divergence: str | None = None

    def take(kind: str, request: dict[str, Any]) -> _Exchange:
        nonlocal position, divergence
        if position >= len(exchanges):
            divergence = f"exchange {position + 1}: unexpected {kind} call"
        else:
            exchange = exchanges[position]
            assert isinstance(exchange, _Exchange)
            difference = _difference(exchange.request, request)
            if exchange.kind != kind or difference:
                divergence = f"exchange {position + 1}: {difference or 'kind'} differs"
            else:
                position += 1
                return exchange
        raise AvaError(ErrorKind.invalid_argument, "replay diverged", divergence or "")

    from ava.session.codec import decode_known

    advertised = decode_known("tools/advertised", {"tools": first.request.get("tools")})
    assert isinstance(advertised, ToolsAdvertised)

    def replay_tool(name: str) -> Callable:
        async def run(arguments: str, cancel: CancelToken) -> Output:
            exchange = take("tool", {"name": name, "arguments": arguments})
            if exchange.error:
                _raise_error(exchange.error)
            if exchange.output is None:
                raise AvaError(ErrorKind.parse, "recorded tool exchange has no output")
            return exchange.output

        return run

    provider = _ReplayProvider(header, take)
    tools = [Tool(definition, replay_tool(definition.name)) for definition in advertised.tools]
    observed_error: dict[str, Any] | None = None
    with tempfile.TemporaryDirectory(prefix="ava-replay-") as directory:
        cwd = Path(directory)
        async with Agent.create_at(
            provider,
            cwd,
            cwd / "session.jsonl",
            header.compaction,
            tools=tools,
            system_prompt=first.request["system_prompt"],
        ) as agent:
            await agent.followup(item_from_wire(header.input))
            try:
                await agent.drive()
            except AvaError as error:
                observed_error = _error(error)
    if divergence:
        raise AvaError(ErrorKind.invalid_argument, "replay diverged", divergence)
    if position != len(exchanges) or observed_error != end.error:
        raise AvaError(
            ErrorKind.invalid_argument,
            "replay diverged",
            f"consumed {position}/{len(exchanges)} exchanges; terminal outcome must also match",
        )
    return {
        "schema_version": 1,
        "matched": True,
        "exchanges": position,
        "reproduced_error": observed_error,
        "live_model_calls": 0,
        "executed_tools": 0,
    }
