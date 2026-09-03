"""HTTP and SSE mechanics. Knows nothing about LLMs."""

from ava.transport.sse import SseEvent, SseParser

__all__ = ["SseEvent", "SseParser"]
