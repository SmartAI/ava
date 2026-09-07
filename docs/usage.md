# Usage and development

[Quick start](../README.md#quick-start) · [Architecture](architecture.md) ·
[Evaluation guide](../eval/README.md)

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


### Try it without an API key

After `uv sync`, run a deterministic installation check:

```sh
printf 'text Ava is ready.\ndone\n' > /tmp/ava-mock.txt
AVA_PROVIDER=mock AVA_MOCK_SCRIPT=/tmp/ava-mock.txt uv run ava -p "hello"
```

Expected output: `Ava is ready.` The mock checks the runtime; it does not solve coding tasks.

