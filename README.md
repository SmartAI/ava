# ava

A coding-agent harness in Python: a durable, replayable session log, a `drive → turn → step` loop
with a two-target inbox, four bounded tools, Anthropic and OpenAI-compatible providers, and a
loopback Web UI. It is a port of [ava-cpp](../ava-cpp)'s architecture; the design documents there
(`docs/agent-loop.md`, `docs/session-log.md`, `docs/model-layer.md`, `docs/functional-spec.md`)
remain the specification this code implements.

## The shape

Two layers with an enforced seam. The headless layer never touches a terminal or a browser; the
application layer never sees a provider wire format.

```
src/ava/
├── base/       AvaError · CancelToken · home and project-root lookup
├── transport/  httpx client with an idle-only streaming timeout · SSE parser
├── llm/        provider seam · Anthropic, OpenAI, and Codex adapters · settings · credentials · mock
├── session/    event vocabulary · JSONL codec · zstd/plain log · recovery · projections
├── proc/       process-group subprocesses with timeout escalation
├── tool/       read · write · edit · bash (head+tail output cap)
├── agent/      drive/turn/step loop · durable inbox · abort repair · compaction · prompt
└── app/        CLI entry point · loopback FastAPI Web UI
```

The seam is the `Agent` handle plus the replay-first session event stream:

```python
from pathlib import Path
from ava.agent import Agent
from ava.llm import Item, Role, make_text_block, provider_from_environment

agent = Agent.create(provider_from_environment(), Path.cwd())
subscription = agent.subscribe(lambda event: print(event.seq, type(event.payload).__name__))
await agent.followup(Item(role=Role.user, blocks=[make_text_block("add a test for the parser")]))
await agent.drive()
```

Every model-visible fact is a durable event. The model's context, the browser's transcript, and
resume after a crash are folds over the same append-only log. Recovery appends `interrupted`
closers and never truncates a complete record; a torn final Zstandard frame is the only physical
unit ever replaced.

## Install and run

Python 3.12 or newer and [uv](https://docs.astral.sh/uv/).

```sh
uv sync
export ANTHROPIC_API_KEY=...        # or OPENAI_API_KEY with --provider=openai
uv run ava                          # serve the Web UI on http://127.0.0.1:8777
uv run ava --serve=0                # pick an unused port; the URL is printed on stdout
uv run ava -p "explain src/ava/agent/turn.py"      # one-shot; the answer goes to stdout
uv run ava -c -p "now add a test"                  # continue the most recent session here
uv run ava -p --session run.jsonl.zst "prompt"     # create or resume an exact session file
uv run ava session dump run.jsonl.zst | jq .kind   # inspect any log as plain JSONL
```

Settings live in `$AVA_HOME/settings.json` (default `~/.ava`); API keys come from the provider's
environment variable or `$AVA_HOME/auth.json`. Resolution order is CLI flag, then the resumed
session, then environment (`AVA_PROVIDER`, `AVA_MODEL`, `AVA_EFFORT`), then the settings file,
then built-in defaults. Any endpoint that speaks the Anthropic or OpenAI family is a config entry:

```json
{
  "provider": "my-gateway",
  "model": "company-model",
  "providers": {
    "my-gateway": {
      "family": "openai",
      "base_url": "https://gateway.internal/v1",
      "api_key_env": "GATEWAY_KEY",
      "models": { "company-model": { "context_window": 128000, "effort_values": ["low", "high"] } }
    }
  }
}
```

`--provider=codex` reuses the Codex CLI's ChatGPT login: Ava reads `~/.codex/auth.json` (or
`$CODEX_HOME/auth.json`) once, read-only, and never touches the refresh token. Run `codex login`
first; the model defaults to the catalog's preferred entry.

`AVA_PROVIDER=mock AVA_MOCK_SCRIPT=script.txt` selects the scripted provider, which is how the
loop and the Web UI run with no network and no key.

## The Web UI

`ava --serve` binds `127.0.0.1`, accepts only a matching `Host`, and rejects cross-origin
`Origin` headers. The browser folds `GET /api/chats/:id/events`, a replay-first Server-Sent Events
stream keyed by event sequence, and keeps no second transcript. The same stream carries unkeyed
`status` messages so the page renders the agent's control state (idle, running, pausing, paused,
aborting) the moment core accepts a request.

| State | Enter | Alt+Enter | Esc | Button |
| --- | --- | --- | --- | --- |
| idle | send | send | | send |
| running | steer the next step | queue a follow-up turn | pause after this step | pause |
| pausing | steer the resumed turn | queue a follow-up turn | abort now | abort |
| paused | resume (text steers first) | queue without resuming | | resume |

Chips above the composer count queued follow-ups and steering, derived from the durable inbox
events. Typing `/` opens the command menu:

| Command | Effect |
| --- | --- |
| `/model [ID]` | Pick a model from the provider catalog; applies at the next step |
| `/effort [LEVEL]` | Set reasoning effort (`none` clears it) |
| `/compact` | Summarize older history now |
| `/context` | Show what the model sees right now, by kind and size: prompt sections (system, environment, AGENTS.md, skills), tool schemas, the compaction summary, your messages, attachments, assistant text, reasoning, tool calls, tool results. Estimated tokens beside the newest provider-measured input. |
| `/skills` | List the skills the model can load |
| `/login [PROVIDER]`, `/logout [PROVIDER]` | Store or remove an API key in `$AVA_HOME/auth.json` |
| `/theme`, `/copy [code]`, `/new`, `/clear`, `/help` | Presentation and chat housekeeping |
| `/pause`, `/abort`, `/resume` | The turn controls, as commands |

## Tests

```sh
uv run pytest
uv run ruff check src tests
```

The suite is the acceptance gate for the borrowed invariants: an acknowledged input is present
exactly once after any kill point; `interrupted` never appears in a log a live loop produced;
a torn final frame recovers idempotently at every byte offset; pause leaves fully paired history
and resume continues without a synthetic user message; abort repairs unanswered calls as
`interrupted` or `skipped`; a provider error is contained at the drive boundary with pending input
retained.

## What is deliberately not here

No TUI and no voice frontend (the Web UI is the interactive surface). No MCP, permission prompts, plugins, subagents, or parallel tool dispatch. Session logs are as
sensitive as the repository they record and are never scrubbed.

## License

MIT.
