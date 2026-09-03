"""Durable session queueing: batch on the loop, publish only after the frame is written.

An enqueue posts one flush to the event loop if none is pending; every payload queued before that
flush runs shares its frame. ``acknowledge`` flushes synchronously so an inbox record is durable
before the caller is told it was accepted. ``sync`` is the turn-seam durability boundary.
"""

from __future__ import annotations

import asyncio

from ava.base import AvaError, ErrorKind
from ava.session.event import EventPayload
from ava.session.log import Log
from ava.session.session import Session


class SessionWriter:
    def __init__(self, session: Session, log: Log) -> None:
        self._session = session
        self._log = log
        self._pending: list[EventPayload] = []
        self._failure: AvaError | None = None
        self._flush_scheduled = False

    @property
    def log(self) -> Log:
        return self._log

    def _check(self) -> None:
        if self._failure is not None:
            raise AvaError(self._failure.kind, self._failure.message, self._failure.detail)

    def enqueue(self, payload: EventPayload) -> None:
        self._check()
        self._pending.append(payload)
        if not self._flush_scheduled:
            self._flush_scheduled = True
            try:
                asyncio.get_running_loop().call_soon(self._posted_flush)
            except RuntimeError:
                # No running loop: flush at the next explicit drain.
                self._flush_scheduled = False

    def acknowledge(self, payload: EventPayload) -> None:
        self.enqueue(payload)
        self.drain()

    def drain(self) -> None:
        """Flush the backlog synchronously and publish every appended event."""
        self._check()
        while self._pending:
            try:
                appended = self._log.append_next(self._pending)
                self._session.publish_durable(appended)
            except AvaError as error:
                self._poison(error)
                raise

    def sync(self) -> None:
        self.drain()
        try:
            self._log.sync()
        except AvaError as error:
            self._poison(error)
            raise

    def _posted_flush(self) -> None:
        self._flush_scheduled = False
        try:
            self.drain()
        except AvaError:
            # The failure is retained and reported on the next enqueue, drain, or sync.
            pass
        except Exception as error:  # noqa: BLE001 - never let a flush escape the loop
            self._poison(AvaError(ErrorKind.internal, "session writer flush failed", str(error)))

    def _poison(self, error: AvaError) -> None:
        if self._failure is None:
            self._failure = error
        self._pending.clear()

    def close(self) -> None:
        self._log.close()
