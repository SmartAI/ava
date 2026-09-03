"""Drive scheduling and replay-first Server-Sent Events for Web chats."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from ava.base import AvaError
from ava.session import Event

from .events import event_json
from .registry import Chat


def begin_drive(chat: Chat) -> None:
    """Start at most one driver; compensate for acknowledgements narrowly missed at its end."""
    chat.drive.running = True
    chat.drive.started = False
    agent = chat.agent

    async def run_drives() -> None:
        chat.notify_status()
        try:
            while True:
                chat.drive.started = True
                try:
                    await agent.drive()
                    succeeded = True
                except AvaError:
                    succeeded = False
                if not chat.drive.finish(succeeded):
                    return
                chat.drive.running = True
        finally:
            chat.drive.running = False
            chat.notify_status()

    chat.task = asyncio.create_task(run_drives())


def status_payload(chat: Chat) -> dict[str, object]:
    payload = chat.agent.status_snapshot()
    payload["status"] = chat.status
    return payload


async def event_stream(chat: Chat, last: int | None) -> AsyncIterator[bytes]:
    """Multiplex durable session events with unkeyed transient status snapshots."""
    queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
    agent = chat.agent

    def emit(event: Event) -> None:
        if last is None or event.seq > last:
            queue.put_nowait(("event", event))

    def on_status(*_: object) -> None:
        queue.put_nowait(("status", status_payload(chat)))

    queue.put_nowait(("status", status_payload(chat)))
    subscription = agent.subscribe(emit)
    unwatch = agent.watch_status(on_status)
    chat.status_watchers.append(on_status)
    queue.put_nowait(("status", status_payload(chat)))
    try:
        while True:
            channel, payload = await queue.get()
            if channel == "status":
                yield f"event: status\ndata: {json.dumps(payload)}\n\n".encode()
                continue
            try:
                encoded = event_json(payload)
            except AvaError:
                return
            yield f"id: {payload.seq}\ndata: {encoded}\n\n".encode()
    finally:
        unwatch()
        chat.status_watchers.remove(on_status)
        subscription.close()
