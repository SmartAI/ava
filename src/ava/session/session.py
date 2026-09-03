"""The in-memory session: event ownership, replay-first subscriptions, and projections.

``model_context()`` is the only correct way to build a model request. ``inbox()`` folds every
``inbox/spliced``, ``step/claimed``, and abort-clearing ``turn/end`` into pending input.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ava.base import AvaError, ErrorKind
from ava.llm.types import ContentBlockKind, Context, Item, Origin, Role
from ava.session.event import (
    AssistantChunk,
    AssistantMessage,
    CompactionSeed,
    Event,
    EventPayload,
    InboxMessage,
    InboxSpliced,
    InboxTarget,
    PromptResolved,
    StepClaimed,
    ToolResult,
    ToolsAdvertised,
    TurnEnd,
    TurnEndReason,
    UserMessage,
    now_ms,
)
from ava.session.recovery import INTERRUPTED_TOOL_TEXT

EventSink = Callable[[Event], None]


@dataclass(slots=True)
class Inbox:
    next_turn: list[InboxMessage] = field(default_factory=list)
    next_step: list[InboxMessage] = field(default_factory=list)

    def target(self, value: InboxTarget) -> list[InboxMessage]:
        return self.next_turn if value == InboxTarget.next_turn else self.next_step


class _Subscriber:
    __slots__ = ("delivering", "next", "sink")

    def __init__(self, sink: EventSink) -> None:
        self.sink = sink
        self.next = 0
        self.delivering = False


class Subscription:
    """Dropping or closing the handle ends future delivery."""

    def __init__(self, session: Session, subscriber: _Subscriber) -> None:
        self._session = session
        self._subscriber: _Subscriber | None = subscriber

    def close(self) -> None:
        if self._subscriber is not None:
            self._session._unsubscribe(self._subscriber)
            self._subscriber = None

    def __enter__(self) -> Subscription:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


class Session:
    def __init__(self, events: list[Event] | None = None) -> None:
        self._events: list[Event] = []
        self._publications: list[tuple[Event, bool]] = []
        self._next_sequence = 0
        self._subscribers: list[_Subscriber] = []
        self._publishing = False
        if events:
            completed_attempts = {
                event.payload.attempt_id
                for event in events
                if isinstance(event.payload, AssistantMessage)
            }
            for event in events:
                self._next_sequence = max(self._next_sequence, event.seq + 1)
                # A chunk whose completed message exists is redundant for every live purpose.
                if (
                    isinstance(event.payload, AssistantChunk)
                    and event.payload.attempt_id in completed_attempts
                ):
                    continue
                self._events.append(event)

    # ---- ownership --------------------------------------------------------------------------

    def __len__(self) -> int:
        return len(self._events)

    def at(self, index: int) -> Event:
        return self._events[index]

    @property
    def events(self) -> list[Event]:
        return self._events

    @property
    def next_sequence(self) -> int:
        return self._next_sequence

    # ---- publication ------------------------------------------------------------------------

    def append(self, payload: EventPayload) -> Event:
        """Ephemeral publication without a physical writer."""
        event = Event(seq=self._next_sequence, at=now_ms(), payload=payload)
        self._next_sequence += 1
        self._publish([event], retain_chunks=True)
        return event

    def publish_durable(self, events: list[Event]) -> None:
        """The writer calls this only after every byte of the containing frame reached the kernel."""
        if not events:
            return
        expected = self._next_sequence
        for event in events:
            if event.seq != expected:
                raise AvaError(
                    ErrorKind.internal,
                    "cannot publish session event",
                    f"expected sequence {expected}, found {event.seq}",
                )
            expected += 1
        self._next_sequence = expected
        self._publish(events, retain_chunks=False)

    def _publish(self, events: list[Event], *, retain_chunks: bool) -> None:
        for event in events:
            retain = retain_chunks or not isinstance(event.payload, AssistantChunk)
            self._publications.append((event, retain))
        if self._publishing:
            return
        self._publishing = True
        try:
            while self._publications:
                event, retain = self._publications.pop(0)
                if retain:
                    self._events.append(event)
                for subscriber in list(self._subscribers):
                    self._deliver(subscriber)
                    if not retain and subscriber in self._subscribers:
                        subscriber.sink(event)
        finally:
            self._publishing = False

    def subscribe(self, sink: EventSink) -> Subscription:
        """Replay every retained event synchronously, then deliver new events without a gap."""
        subscriber = _Subscriber(sink)
        self._subscribers.append(subscriber)
        self._deliver(subscriber)
        return Subscription(self, subscriber)

    def _unsubscribe(self, subscriber: _Subscriber) -> None:
        try:
            self._subscribers.remove(subscriber)
        except ValueError:
            pass

    def _deliver(self, subscriber: _Subscriber) -> None:
        if subscriber.delivering:
            return
        subscriber.delivering = True
        try:
            while subscriber.next < len(self._events) and subscriber in self._subscribers:
                index = subscriber.next
                subscriber.next += 1
                subscriber.sink(self._events[index])
        finally:
            subscriber.delivering = False

    # ---- projections ------------------------------------------------------------------------

    def model_context(self) -> Context:
        context = Context()
        # Recovery repairs old error-closed turns by appending interrupted tool results. Providers
        # require each result beside its original assistant call, so project those repair blocks at
        # that semantic position rather than their later append-only storage position.
        relocated_results = {
            block.call_id: block
            for event in self._events
            if isinstance(event.payload, ToolResult)
            for block in event.payload.item.blocks
            if block.kind == ContentBlockKind.tool_result
            and block.origin == Origin.interrupted
            and block.text == INTERRUPTED_TOOL_TEXT
        }
        covered_end: int | None = None
        seed_seq = 0
        for event in reversed(self._events):
            if isinstance(event.payload, CompactionSeed):
                covered_end = event.payload.covered_end
                seed_seq = event.seq
                context.items.append(event.payload.item)
                break

        def visible(seq: int) -> bool:
            # Defend against a malformed range instead of trusting covered_end < seed_seq.
            return covered_end is None or seq > covered_end or seq > seed_seq

        for event in self._events:
            payload = event.payload
            if isinstance(payload, PromptResolved):
                context.system_prompt = payload.system_prompt
            elif isinstance(payload, ToolsAdvertised):
                context.tools = list(payload.tools)
            elif isinstance(payload, StepClaimed):
                if visible(event.seq):
                    context.items.extend(message.item for message in payload.claimed)
            elif isinstance(payload, UserMessage | AssistantMessage | ToolResult):
                if not visible(event.seq):
                    continue
                if isinstance(payload, AssistantMessage):
                    context.items.append(payload.item)
                    repaired = [
                        relocated_results.pop(block.call_id)
                        for block in payload.item.blocks
                        if block.kind == ContentBlockKind.tool_call
                        and block.call_id in relocated_results
                    ]
                    if repaired:
                        context.items.append(Item(role=Role.tool, blocks=repaired))
                elif isinstance(payload, ToolResult):
                    ordinary = [
                        block
                        for block in payload.item.blocks
                        if not (
                            block.kind == ContentBlockKind.tool_result
                            and block.origin == Origin.interrupted
                            and block.text == INTERRUPTED_TOOL_TEXT
                        )
                    ]
                    if len(ordinary) == len(payload.item.blocks):
                        context.items.append(payload.item)
                    elif ordinary:
                        context.items.append(
                            Item(
                                role=payload.item.role,
                                blocks=ordinary,
                                provenance=payload.item.provenance,
                            )
                        )
                else:
                    context.items.append(payload.item)
        return context

    def inbox(self) -> Inbox:
        result = Inbox()
        seen_ids: set[str] = set()
        pending_ids: set[str] = set()

        def invalid(detail: str) -> AvaError:
            return AvaError(ErrorKind.parse, "invalid session inbox", detail)

        for event in self._events:
            payload = event.payload
            if isinstance(payload, InboxSpliced):
                messages = result.target(payload.target)
                if payload.index > len(messages):
                    raise invalid("splice index is outside its target")
                if payload.removed > len(messages) - payload.index:
                    raise invalid("splice removal exceeds its target")
                for message in messages[payload.index : payload.index + payload.removed]:
                    pending_ids.discard(message.id)
                for message in payload.inserted:
                    if not message.id:
                        raise invalid("inbox message id is empty")
                    if message.source_id and message.source_id not in seen_ids:
                        raise invalid(
                            f"inbox message source id has not been seen: {message.source_id}"
                        )
                    if message.id in pending_ids:
                        raise invalid(f"inbox message id is already pending: {message.id}")
                    if message.id in seen_ids:
                        raise invalid(f"inbox message id is reused: {message.id}")
                    seen_ids.add(message.id)
                    pending_ids.add(message.id)
                messages[payload.index : payload.index + payload.removed] = list(payload.inserted)
            elif isinstance(payload, StepClaimed):
                if payload.target is None:
                    if len(payload.claimed) != 1 or payload.claimed[0].id:
                        raise invalid("legacy claim must contain exactly one id-less item")
                    continue
                if not payload.claimed:
                    raise invalid("claimed inbox prefix is empty")
                if payload.target == InboxTarget.next_turn and len(payload.claimed) != 1:
                    raise invalid("next-turn claim must contain exactly one item")
                if any(not message.id for message in payload.claimed):
                    raise invalid("claimed inbox id is empty")
                messages = result.target(payload.target)
                if len(messages) < len(payload.claimed):
                    raise invalid("claimed inbox prefix exceeds its target")
                for index, entry in enumerate(payload.claimed):
                    if messages[index].id != entry.id:
                        raise invalid(f"claimed inbox id is not in the target prefix: {entry.id}")
                for entry in payload.claimed:
                    pending_ids.discard(entry.id)
                del messages[: len(payload.claimed)]
            elif isinstance(payload, TurnEnd) and payload.reason == TurnEndReason.user_abort:
                result.next_turn.clear()
                result.next_step.clear()
                pending_ids.clear()
        return result
