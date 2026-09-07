<p align="center">
  <img src="src/ava/app/web/assets/ava-logo.svg" alt="AVA" width="300">
</p>

<p align="center">
  <strong>A Python coding agent built for measured iteration.</strong>
</p>

<p align="center">
  <a href="https://github.com/SmartAI/ava/actions/workflows/ci.yml"><img src="https://github.com/SmartAI/ava/actions/workflows/ci.yml/badge.svg?branch=main" alt="Test status"></a>
  <img src="https://img.shields.io/badge/Python-3.12%2B-7c3aed?style=flat-square" alt="Python 3.12 or newer">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-22c55e?style=flat-square" alt="MIT license"></a>
  <img src="https://img.shields.io/badge/status-alpha-f59e0b?style=flat-square" alt="Alpha status">
</p>

Ava is a compact Python coding-agent runtime with a CLI, a local Web UI, and an
embeddable API. Its evaluation workflow connects task outcomes to session evidence,
so changes to tools and context organization can be tested against a baseline.

- **Measure real tasks:** run fresh agent/tool interactions in isolated environments
  and grade the resulting work independently.
- **Compare versions and agents:** use frozen tasks, explicit model settings, repeated
  attempts, and separate correctness, time, token, and cost measurements.
- **Investigate context:** inspect existing session logs for tool failures, repeated
  calls, and large or truncated results, with references back to the events.
- **Propose an improvement:** let Ava review development-session evidence and produce
  a prompt candidate for a new evaluation. Proposals are not automatically accepted.

[Quick start](#quick-start) · [Evaluate and improve](#evaluate-and-improve) ·
[Python API](#python-api) · [Architecture](docs/architecture.md) ·
[Benchmark](docs/benchmark.md)

> [!IMPORTANT]
> Ava is alpha software. Its Python API and on-disk session format may change before 1.0.

## Quick start

Requirements: Python 3.12 or newer, [uv](https://docs.astral.sh/uv/), and macOS or Linux.
The Web UI bundle is included; Node.js is needed only for frontend development.

```sh
git clone https://github.com/SmartAI/ava.git
cd ava
uv sync

export ANTHROPIC_API_KEY=...
uv run ava --serve
```

Open `http://127.0.0.1:8777`, choose a project directory, and start a chat. The UI
supports streamed responses, attachments, queued follow-ups, pause/resume/abort,
and session metrics.

For OpenAI, set `OPENAI_API_KEY` and run `uv run ava --serve --provider openai`.
For an existing Codex CLI login, run `uv run ava --serve --provider codex`. The Codex
adapter reads `~/.codex/auth.json` read-only and never touches the refresh token.
Use `--model <model-id>` and `--effort <level>` to select a model and reasoning
setting supported by your provider. Model availability depends on the account and
endpoint; a model listed in a desktop app is not proof that an evaluation adapter
can call it. Evaluation runs record and verify the requested model explicitly.

Tools edit files and execute commands with your local user permissions. Output and
timeout limits do not provide a sandbox; Ava does not prompt before each tool call.

### Try it without an API key

After `uv sync`, run a deterministic installation check:

```sh
printf 'text Ava is ready.\ndone\n' > /tmp/ava-mock.txt
AVA_PROVIDER=mock AVA_MOCK_SCRIPT=/tmp/ava-mock.txt uv run ava -p "hello"
```

Expected output: `Ava is ready.` The mock checks the runtime; it does not solve coding tasks.

## Evaluate and improve

Ava measures harness changes with a fixed model, independent task grades, and
explicit resource accounting. A baseline is an immutable runtime and configuration.
Start by changing one mechanism, then evaluate combinations independently with
fresh model/tool executions.
Correctness comes first. Costs include unsuccessful attempts, and missing usage
remains unknown.

The [benchmark summary](docs/benchmark.md) describes the baselines, method and
measured evolution. The latest tool-contract iteration narrowed the observed cost
gap against historical Pi 0.85.1 from 14.2% to 2.5% on 14 matching runs with
complete usage. The full follow-up passed 14/15 tasks; one TLS interruption has
unknown request usage. This small development comparison does not establish
cost parity or general capability. Detailed experimental records remain local.

### Run an evaluation

The [`eval/` guide](eval/README.md) covers isolated task execution, reference and
unchanged-workspace controls, version comparisons, public-benchmark adapters, and
session diagnosis. Supply a local task manifest and configure the same supported
model and effort for each comparison. General-purpose templates are included;
research datasets, experiment configurations, raw runs, and detailed analysis stay
local and are excluded from version control.
Evaluations are local, explicit runs; CI does not run them or upload their artifacts.

Measure independently graded completion, total/cached/uncached input, output
including reasoning, model and tool calls, latency, and cost under explicit prices.
Use new tasks to check generalization after developing a candidate. Historical
comparisons, deterministic runtime checks, and fresh task evaluations answer
different questions; none substitutes for the others.

### Inspect and iterate

```sh
uv run ava session inspect /absolute/path/to/session.jsonl.zst
uv run python -m eval.diagnose /absolute/path/to/session.jsonl.zst \
  --output eval/results/session-diagnosis.json
```

Inspect actual tool inputs and results before deciding what to change. Repeated
reads can be necessary after edits, tool-result characters are not provider tokens,
and higher cache use does not by itself mean lower cost. The experimental
[prompt-proposal workflow](eval/README.md#other-tools) can propose one
change from selected local development evidence; it never accepts its own proposal.

## Use Ava from the CLI or Python

### CLI

```sh
uv run ava -p "explain src/ava/agent/turn.py"
uv run ava -c -p "which failure paths should have regression tests?"
uv run ava -p --session review.jsonl.zst "review this project"
uv run ava session inspect review.jsonl.zst
uv run ava session dump review.jsonl.zst
```

`-c` continues the latest session. Explicit session paths make a run easy to locate
and inspect later.

### Python API

Use the same runtime inside your application. With a provider configured, save this
as a Python file and run it with `uv run python`:

```python
import asyncio
from pathlib import Path

from ava.agent import Agent
from ava.llm import Item, Role, make_text_block, provider_from_environment


async def main():
    async with Agent.create(provider_from_environment(), Path.cwd()) as agent:
        with agent.subscribe(lambda event: print(event.seq, type(event.payload).__name__)):
            await agent.followup(
                Item(
                    role=Role.user,
                    blocks=[make_text_block("Explain src/ava/agent/turn.py")],
                )
            )
            await agent.drive()


asyncio.run(main())
```

Applications submit input, control the driver, and subscribe to durable events without
handling provider wire formats. `Agent.create` also accepts custom `tools` and a
`system_prompt`, so evaluations and embedded applications can configure the runtime.

## Engineering foundations

```text
CLI / loopback Web UI / Python application
                    │
                    ▼
                  Agent ─────► read · write · edit · bash
                    │
                    ├────────► Anthropic · OpenAI-compatible · Codex · mock
                    │
                    ▼
          append-only session events
                    │
                    ├────────► model context and compaction
                    ├────────► browser history and metrics
                    └────────► resume and recovery
```

The session event stream is the source of truth for conversation state, queued input,
and recovery. The optional I/O recording is a diagnostic artifact for offline replay.

- **Recoverable sessions:** checksummed Zstandard frames preserve complete events;
  recovery can replace an incomplete final frame.
- **Explicit interruption behavior:** pause stops at a complete step boundary; abort
  pairs partial tool calls with `interrupted` or `skipped` results. Provider failures
  are contained at the drive boundary and pending input remains available.
- **Bounded resources:** HTTP bodies, SSE frames, session records, tool output, and
  decoded data have explicit limits; subprocesses have timeout escalation.
- **Shared event history:** the browser consumes persisted events instead of keeping
  a separate conversation store. The server binds to loopback and validates `Host`
  and `Origin` headers.

Input acknowledgement follows a write to the kernel; `fsync` occurs at separate turn
boundaries. These are process-recovery guarantees, not exactly-once external command
execution or a promise that every acknowledged input survives power loss.

Read the [architecture contracts](docs/architecture.md) for implementation details
and the [port notes](docs/port-notes.md) for differences from the original C++ runtime.

## Configuration

Settings live in `$AVA_HOME/settings.json` (default: `~/.ava`). Credentials come from the
provider's environment variable or `$AVA_HOME/auth.json`. Selection precedence is:

1. CLI flags
2. Resumed session
3. `AVA_PROVIDER`, `AVA_MODEL`, and `AVA_EFFORT`
4. Settings file
5. Built-in defaults

<details>
<summary>Configure a custom model gateway</summary>

Custom endpoints can be registered in the settings file. The `openai` family uses streaming
Chat Completions, and the `anthropic` family uses Messages; endpoints must support the request
fields and tool-calling behaviour required by the selected adapter:

```json
{
  "provider": "my-gateway",
  "model": "company-model",
  "providers": {
    "my-gateway": {
      "family": "openai",
      "base_url": "https://gateway.internal/v1",
      "api_key_env": "GATEWAY_KEY",
      "models": {
        "company-model": {
          "context_window": 128000,
          "effort_values": ["low", "high"]
        }
      }
    }
  }
}
```

</details>

## Develop

```sh
uv run pytest
uv run ruff check src tests eval
uv run mypy src eval/run.py
npm ci
npm run check
npm test
```

When the React source in `src/ava/app/web/frontend/` changes, rebuild the checked-in browser bundle
with `npm run build`.

The acceptance suite focuses on observable invariants: durable input is never lost or duplicated;
tool calls remain paired after abort and recovery; torn tails recover idempotently; pause/resume
preserves valid model history; and provider failures do not discard pending work.

## Scope

Ava focuses on a small, inspectable coding-agent runtime. It does not currently include
MCP, permission prompts, plugins, subagents, parallel tool dispatch, a TUI, or voice.
Session logs and recordings are not scrubbed of sensitive content.

## License

[MIT](LICENSE) © 2026 Min Liu.
