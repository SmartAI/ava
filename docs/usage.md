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

### Qt Quick desktop app

The desktop app uses QML and starts its own local Ava backend.
Install the optional Qt dependencies and launch it from a terminal:

```sh
uv sync --extra desktop
uv run --extra desktop ava-desktop --project .
```

`--provider`, `--model`, and `--effort` use the same selection rules as the CLI.
For example, an existing Codex CLI login can be used with `--provider codex`.
Configure credentials and providers through the existing settings file or Web UI before creating a conversation.

Choose a project folder, create or reopen a conversation, and send a message.
Enter sends; Shift+Enter inserts a newline.
While Ava runs, further messages queue for a subsequent turn.
Pause waits for a step boundary; Stop cancels the run; Resume continues a paused conversation.
Drafts are kept separately for each conversation while the app remains open.

The app shares durable history under `$AVA_HOME` and remembers the last selected project and conversation.
Closing the window stops the backend and cancels active work before exiting.
Only one desktop instance may use an Ava home at a time.
Use a separate `AVA_HOME` when running an independent Ava server or CLI against the same project; do not open the same session with concurrent writers.

This first desktop version displays selectable plain text and expandable tool output.
Attachments, Markdown formatting, code diff, browser panels, and desktop installers are not yet included.
macOS and Linux are the initial targets; platform-specific packaging and native input-method testing remain necessary.

Run the desktop acceptance scenarios with a deterministic local model server:

```sh
uv run --extra desktop pytest -q tests/test_desktop.py
uv run --extra desktop pyside6-qmllint --max-warnings 0 src/ava/app/desktop/qml/Main.qml
```

Without a display server, these tests render the QML window offscreen and deliver Qt mouse, keyboard, and input-method events.
They exercise the actual backend process and HTTP/SSE interface without calling a live model.

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
uv sync --extra desktop
uv run --extra desktop pytest
uv run --extra desktop ruff check src tests eval
uv run --extra desktop mypy src eval/run.py
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

