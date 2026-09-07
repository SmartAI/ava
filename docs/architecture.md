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

The Codex adapter accepts multiple function calls in one response. It buffers arguments by item
ID, then emits complete calls to the core in their original order. Parameter completion order does
not determine tool execution order; the driver still dispatches tools sequentially. A malformed or
incomplete response does not execute its calls or commit unmatched calls to the next request.
Terminal usage remains billable even when the response fails. Buffering moves the tool-only first
content timestamp to a completed call, so that timestamp is not directly comparable to a provider
that emits its tool-start event immediately.

## Editing and progress feedback

The model-facing `edit` contract uses `path` and an `edits` array of `oldText` /
`newText` pairs. Each exact match is located in the original file. Missing,
ambiguous, or overlapping matches reject the entire batch before writing.
Successful replacements preserve untouched bytes and are written once. This
prevalidation does not promise filesystem rollback after an I/O failure.
Legacy `old_string`, `new_string`, and `replace_all` arguments remain accepted
for existing callers; new model requests receive the batch schema.

The default prompt does not require narration before tool calls. The browser
derives activity labels from tool events; final outcomes, errors, and blockers
still have to be reported faithfully.

## Session authority

The append-only session event stream is the authority for model context, browser replay, pending
inbox state, and crash recovery. Physical writes are bounded frames; subscribers see durable events
only after their bytes reach the kernel. Recovery may repair a torn final frame and append missing
`interrupted` lifecycle closers, but never rewrites a complete logical event.

The JSONL codec is intentionally explicit. Its event-by-event mapping is compatibility and
validation code, not generic object serialization, and unknown future event kinds are preserved.

## Session diagnostics and task evaluation

`ava session inspect` derives a read-only report from the event log. It does not
repair files or infer task correctness from a completed turn.

`eval.diagnose` uses the same session authority to locate repeated tool arguments,
error results and truncation clues. It reports evidence coordinates and character
counts rather than inventing token measurements or declaring repeats wasteful.

`eval.benchmark` freezes tasks, candidate wheels, adapter code and configuration,
then runs fresh Harbor environments. Models see new tool observations and may take
different trajectories. Independent deliverable grades are separate from process
completion, resource measurements and infrastructure failures. Comparisons require
complete compatible samples and verify recorded candidate identity.

`eval.evolve` is a bounded prompt-search experiment. Ava receives selected development
sessions and only evidence-reading/proposal tools. A candidate changes the baseline
wheel's prompt and RECORD; it cannot change graders or accept itself. Candidate
selection requires new task trials, with independent validation data supplied by
the experiment owner. This is not model-weight training or an RL implementation.

An opt-in one-shot recording captures provider streams and tool responses for
offline replay. It is a diagnostic artifact, not another live conversation store:
normal operation and recovery still use only the session log. Replay constructs
the same `Agent` with recorded tools and a recorded initial prompt, checks each
request, and refuses missing exchanges or a changed terminal outcome. No live
provider or real tool is instantiated during replay.

`Agent.create` and `Agent.create_at` accept explicit `tools` and `system_prompt`
overrides for embedding and replay. The default coding tools and prompt are
unchanged when these arguments are omitted. Evaluation tooling stays under
`eval/`; its heavy benchmark dependencies are excluded from the Ava wheel.

See [Evaluation and iteration](../eval/README.md) for datasets, upstream graders,
measurement definitions, and reproducibility limits.

## Evaluation gates

Every architectural change must keep these gates green:

- durable input is acknowledged and claimed exactly once;
- every persisted model tool call has one result after completion, abort, or recovery;
- pause/resume and provider failure leave replayable history;
- `ruff`, `mypy`, the Python 3.12 test suite, and the frontend build pass;
- application code has no direct `agent.state` access;
- provider clients close on agent replacement and shutdown.
