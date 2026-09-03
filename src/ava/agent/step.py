"""One streamed provider attempt: assembly, timing, and accounting."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from ava.agent.state import AgentState
from ava.base import AvaError, CancelToken, ErrorKind
from ava.llm import (
    Context,
    Item,
    Provenance,
    Role,
    Selection,
    StopReason,
    StreamEvent,
    StreamEventKind,
    Usage,
    remember_selection,
    validate_effort,
)
from ava.llm.types import (
    ContentBlockKind,
    make_reasoning_block,
    make_text_block,
    make_tool_call_block,
)
from ava.session import AssistantChunk, AttemptTiming
from ava.session import Selection as SelectionEvent
from ava.session import Usage as UsageEvent


@dataclass(slots=True)
class Timing:
    ttft_ms: int | None = None
    ttft_text_ms: int | None = None
    elapsed_ms: int | None = None


@dataclass(slots=True)
class StepResult:
    assistant: Item
    open_tool_call: int | None = None
    stop_reason: StopReason | None = None
    usage: Usage = field(default_factory=Usage)
    timing: Timing = field(default_factory=Timing)
    error: AvaError | None = None


def merge_usage(total: Usage, report: Usage) -> None:
    """Later reports override per category: input-side counts arrive first, output last."""
    for name in ("input", "cached_read", "cache_write", "cache_write_1h", "output", "reasoning"):
        value = getattr(report, name)
        if value is not None:
            setattr(total, name, value)


def _begins_content(kind: StreamEventKind) -> bool:
    return kind in (
        StreamEventKind.text_delta,
        StreamEventKind.reasoning_item,
        StreamEventKind.tool_call_start,
    )


class _Assembly:
    """The pure turn assembler: borrowed stream events in, an owned Item out."""

    def __init__(self, selected: Selection) -> None:
        self.assistant = Item(
            role=Role.assistant,
            provenance=Provenance(provider=selected.provider, model=selected.model),
        )
        self.open_tool_call: int | None = None
        self.usage = Usage()
        self.error: AvaError | None = None

    def consume(
        self, event: StreamEvent, state: AgentState, attempt_id: str, append_chunks: bool
    ) -> None:
        blocks = self.assistant.blocks
        match event.kind:
            case StreamEventKind.text_delta:
                if blocks and blocks[-1].kind == ContentBlockKind.text:
                    blocks[-1].text += event.text
                else:
                    blocks.append(make_text_block(event.text))
                if append_chunks:
                    state.append(AssistantChunk(attempt_id=attempt_id, delta=event.text))
            case StreamEventKind.reasoning_item:
                blocks.append(make_reasoning_block(event.text))
            case StreamEventKind.tool_call_start:
                if self.open_tool_call is not None:
                    raise AvaError(
                        ErrorKind.parse,
                        "provider started a tool call before finishing the previous call",
                    )
                blocks.append(make_tool_call_block(event.id, event.name))
                self.open_tool_call = len(blocks) - 1
            case StreamEventKind.tool_call_delta:
                if self.open_tool_call is None or blocks[self.open_tool_call].call_id != event.id:
                    raise AvaError(ErrorKind.parse, "provider sent tool input without its call")
                blocks[self.open_tool_call].arguments_json += event.text
            case StreamEventKind.tool_call_end:
                if self.open_tool_call is None or blocks[self.open_tool_call].call_id != event.id:
                    raise AvaError(
                        ErrorKind.parse, "provider finished a tool call that was not open"
                    )
                call = blocks[self.open_tool_call]
                if not call.arguments_json:
                    call.arguments_json = "{}"
                self.open_tool_call = None
            case _:
                pass


def append_accounting(
    state: AgentState,
    attempt_id: str,
    usage: Usage,
    timing: Timing,
    *,
    append_empty_usage: bool = False,
) -> None:
    """A broken stream is billed like a completed one, so whatever accounting arrived is recorded."""
    if usage.any() or append_empty_usage:
        state.append(
            UsageEvent(
                attempt_id=attempt_id,
                input=usage.input,
                cached_read=usage.cached_read,
                cache_write=usage.cache_write,
                cache_write_1h=usage.cache_write_1h,
                output=usage.output,
                reasoning=usage.reasoning,
            )
        )
    if timing.elapsed_ms is not None:
        state.append(
            AttemptTiming(
                attempt_id=attempt_id,
                elapsed_ms=timing.elapsed_ms,
                ttft_ms=timing.ttft_ms,
                ttft_text_ms=timing.ttft_text_ms,
            )
        )


def _record_selection(state: AgentState, selected: Selection) -> None:
    """The complete selection is logged before the request that first consumes it."""
    if state.consumed_selection == selected:
        return
    warning: str | None = None
    if state.provider.remembers_selection:
        try:
            remember_selection(selected)
        except AvaError:
            warning = "could not remember model selection; this choice applies only to this run"
    state.append(
        SelectionEvent(
            provider=selected.provider,
            model=selected.model,
            effort=selected.effort,
            warning=warning,
        )
    )
    state.drain()
    state.consumed_selection = Selection(selected.provider, selected.model, selected.effort)


async def step(
    state: AgentState, context: Context, attempt_id: str, append_chunks: bool, cancel: CancelToken
) -> StepResult:
    provider = state.provider
    selected = Selection(
        provider.selection.provider, provider.selection.model, provider.selection.effort
    )
    validate_effort(provider, selected)
    assembly = _Assembly(selected)
    _record_selection(state, selected)

    timing = Timing()
    started = time.monotonic()

    def sink(event: StreamEvent) -> None:
        since_start = int((time.monotonic() - started) * 1000)
        if timing.ttft_ms is None and _begins_content(event.kind):
            timing.ttft_ms = since_start
        if timing.ttft_text_ms is None and event.kind == StreamEventKind.text_delta:
            timing.ttft_text_ms = since_start
        if event.kind == StreamEventKind.usage:
            # Merged even after an assembly error: the provider billed the attempt either way.
            merge_usage(assembly.usage, event.usage)
            return
        if assembly.error is not None:
            return
        try:
            assembly.consume(event, state, attempt_id, append_chunks)
        except AvaError as error:
            assembly.error = error

    error: AvaError | None = None
    stop_reason: StopReason | None = None
    try:
        stop_reason = await provider.stream(context, selected, sink, cancel)
    except AvaError as failure:
        error = failure
    timing.elapsed_ms = int((time.monotonic() - started) * 1000)
    state.drain()
    result = StepResult(
        assistant=assembly.assistant,
        open_tool_call=assembly.open_tool_call,
        usage=assembly.usage,
        timing=timing,
    )
    if error is not None:
        result.error = error
    elif assembly.error is not None:
        result.error = assembly.error
    elif assembly.open_tool_call is not None:
        result.error = AvaError(ErrorKind.parse, "provider stopped before finishing a tool call")
    else:
        result.stop_reason = stop_reason
        result.open_tool_call = None
    return result
