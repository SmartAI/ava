"""A one-shot cancellation token shared by the loop, providers, and process-backed tools.

The agent creates one token per drive. An accepted user abort cancels it; a provider stream or a
subprocess observing the token stops promptly and reports ``ErrorKind.cancelled``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

from ava.base.errors import AvaError, ErrorKind

T = TypeVar("T")


class CancelToken:
    def __init__(self) -> None:
        self._cancelled = False
        self._callbacks: list[Callable[[], None]] = []

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def cancel(self) -> None:
        if self._cancelled:
            return
        self._cancelled = True
        callbacks, self._callbacks = self._callbacks, []
        for callback in callbacks:
            callback()

    def on_cancel(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Register a callback; returns a function that unregisters it."""
        if self._cancelled:
            callback()
            return lambda: None
        self._callbacks.append(callback)

        def remove() -> None:
            try:
                self._callbacks.remove(callback)
            except ValueError:
                pass

        return remove

    def raise_if_cancelled(self) -> None:
        if self._cancelled:
            raise AvaError(ErrorKind.cancelled, "provider request was cancelled")

    async def guard(self, awaitable: Awaitable[T]) -> T:
        """Run ``awaitable`` as a task that is cancelled when this token fires.

        A cancellation is reported as ``AvaError(cancelled)`` rather than ``CancelledError`` so the
        loop can repair history and continue instead of unwinding.
        """
        self.raise_if_cancelled()
        task = asyncio.ensure_future(awaitable)

        def cancel_task() -> None:
            task.cancel()

        remove = self.on_cancel(cancel_task)
        try:
            return await task
        except asyncio.CancelledError:
            if self._cancelled:
                raise AvaError(ErrorKind.cancelled, "provider request was cancelled") from None
            raise
        finally:
            remove()


NEVER = CancelToken()
"""A token that never fires, for callers with no cancellation source."""
