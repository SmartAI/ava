# Port notes: ava-cpp → ava (Python)

What was borrowed is the architecture and its decisions, not the code. This page records how each
C++ design element maps onto Python, and where this port deliberately differs.

## Design-to-Python mapping

| ava-cpp | ava (Python) | Notes |
| --- | --- | --- |
| `std::expected<T, Error>` everywhere | `AvaError` exceptions | The loop still contains failures at the same boundaries; tools still return `Output(is_error=True)` for recoverable failures rather than raising |
| Asio `io_context` + `awaitable` | `asyncio` | One loop; providers, tools, and the driver are coroutines |
| Asio cancellation slots | `CancelToken` | One token per drive; an accepted abort cancels the provider task and signals the tool's process group |
| Inbox mutation gate (channel-as-mutex) | `asyncio.Lock` held across the claim seam | Acknowledgements, claims, and boundary decisions serialize on it exactly as before |
| libcurl on a worker thread | `httpx.AsyncClient` | Connect timeout 10 s, idle-only read timeout while streaming, no total timeout; TLS verification is not configurable |
| Glaze typed wire structs | `json` + `dataclasses` | The record format is byte-compatible: `seq`, `at`, `kind`, absent-means-default, lower-snake enums |
| `std::deque` stable addresses | Python object identity | `Context.items` holds references to the session's `Item` objects; nothing is copied per request |
| Zstandard C API | `zstandard` | Independent checksummed frames with content size; torn-tail recovery uses the decoder's `eof`/`unused_data` |
| `flock` writer lock | `fcntl.flock` | A second writer fails with a clear message |
| Boost.Process process groups | `asyncio.create_subprocess_exec(start_new_session=True)` | SIGTERM, 250 ms grace, SIGKILL |
| Glaze HTTP server | FastAPI + uvicorn | Same routes, same JSON error shape, same loopback fence |
| Catch2 + lit/FileCheck | pytest | Providers are tested against a local `http.server`; the Web UI against a real uvicorn socket |

## Deliberate differences

- **No TUI, no voice.** The Web UI is the interactive frontend, so bare `ava` serves it.
- **Providers:** `anthropic`, `openai`, `deepseek`, `llamacpp`, `codex` (Responses over the Codex
  CLI's `~/.codex/auth.json` credential), custom Anthropic- or OpenAI-family endpoints, and `mock`.
- **`ava session`** implements only `dump`. The other inspection commands were not implemented in
  ava-cpp either.
- **Prompt resources** ship inside the package (`ava/agent/prompts/system.md`,
  `ava/session/prompts/checkpoint.md`); `AVA_AGENT_SYSTEM_PROMPT_PATH` still overrides the system
  prompt template.
- **Web composer race fixed.** ava-cpp records that the composer can show a stale running state
  when a submission acknowledgement arrives after the stream delivered the turn's terminal events.
  The page now ignores an acknowledgement's status once a `turn/end` or `drive/error` has been
  applied since the request was sent.

## What the tests gate

`tests/test_session.py` pins the format and recovery invariants: unknown kinds survive verbatim,
the inbox claims one next-turn and all next-step messages, abort clears both targets in one
record, repair appends closers in order and rejects an overlapping lifecycle, a torn final frame
recovers idempotently at every byte offset, and a corrupt complete frame is rejected.

`tests/test_agent.py` pins the loop: the exact event sequence of a turn, sequential tool dispatch,
steering at the next step versus follow-ups in their own turn, pause at a step seam with a
message-free continuation, abort repair with `interrupted` and `skipped` origins, provider-error
containment with pending input retained, reopen restoring `paused`, and a compaction seed that
replaces the prefix while keeping the tail.

`tests/test_web.py` pins the loopback fence, the JSON contracts, mid-turn Enter-versus-Alt+Enter
routing, `Last-Event-ID` replay, and that a dropped event stream releases its subscription.
