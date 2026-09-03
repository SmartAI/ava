"""One error type for every seam.

``message`` is user-facing and says what failed and what to do. ``detail`` is diagnostic.
``recoverable`` errors are shown and execution continues; the rest end the operation.
"""

from __future__ import annotations

from enum import StrEnum


class ErrorKind(StrEnum):
    invalid_argument = "invalid_argument"
    not_found = "not_found"
    permission = "permission"
    io = "io"
    parse = "parse"
    network = "network"
    timeout = "timeout"
    cancelled = "cancelled"
    auth = "auth"
    rate_limited = "rate_limited"
    provider = "provider"
    internal = "internal"


class AvaError(Exception):
    def __init__(
        self,
        kind: ErrorKind,
        message: str,
        detail: str = "",
        *,
        recoverable: bool = False,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.detail = detail
        self.recoverable = recoverable

    def __str__(self) -> str:
        if self.detail:
            return f"{self.message}: {self.detail}"
        return self.message

    def __repr__(self) -> str:
        return f"AvaError({self.kind.value!r}, {self.message!r}, {self.detail!r})"
