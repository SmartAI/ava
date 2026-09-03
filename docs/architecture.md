# Architecture

Ava has a headless core and two thin application surfaces: the CLI and the loopback Web UI.
Dependencies point inward; provider wire formats and frontend concerns do not enter the agent or
session packages.

```text
app ──► agent ──► session ──► llm ──► transport
          │           │          │
          └──► tool ──┴──► proc  └──► base
```

## Public seam

`Agent` is the application boundary. Applications submit input, control the driver, subscribe to
durable events, and read provider-neutral status or context reports through that object. Access to
`Agent.state` is reserved for agent internals and invariant-focused tests.

An agent owns its provider, durable writer, and scratchpad. Call `await agent.aclose()` when done,
or use `async with`. The Web registry closes every chat agent during the FastAPI lifespan shutdown.

## Durable input seam

Acknowledging input, claiming input for a step, and deciding a turn boundary are serialized by one
inbox gate. `followup()` and `steer()` return only after their inbox splice is durable. A step claims
one next-turn message on its first step and all available next-step messages. The gate remains held
from the driver's snapshot through that claim so an acknowledged message cannot be lost between
the two operations.

## Driver lifecycle

The driver moves through `idle`, `running`, `pausing`, `paused`, and `aborting`. A turn contains one
or more provider steps; a step either ends the turn or emits tool calls whose paired results become
the next step's context. Pause is honored at a complete step boundary. Abort repairs partial model
and tool output before it closes the turn.

Only one driver may own an agent. The Web `DriveHandoff` compensates for a message acknowledged as
the owning driver finishes, without starting a second driver.

## Session authority

The append-only session event stream is the authority for model context, browser replay, pending
inbox state, and crash recovery. Physical writes are bounded frames; subscribers see durable events
only after their bytes reach the kernel. Recovery may repair a torn final frame and append missing
`interrupted` lifecycle closers, but never rewrites a complete logical event.

The JSONL codec is intentionally explicit. Its event-by-event mapping is compatibility and
validation code, not generic object serialization, and unknown future event kinds are preserved.

## Evaluation gates

Every architectural change must keep these gates green:

- durable input is acknowledged and claimed exactly once;
- every model tool call has one result after completion, abort, or recovery;
- pause/resume and provider failure leave replayable history;
- `ruff`, `mypy`, the Python 3.12 test suite, and the frontend build pass;
- application code has no direct `agent.state` access;
- provider clients close on agent replacement and shutdown.
