# Local agent instructions

## Project overview and structure

Ava is a Python coding-agent runtime with durable sessions, a CLI, a FastAPI backend, a React Web UI, and a PySide6/Qt Quick desktop app.

- `src/ava/agent/`: agent lifecycle, prompts, skills, and tool dispatch.
- `src/ava/session/`: durable events, compressed logs, context reconstruction, and recovery.
- `src/ava/llm/`: provider-neutral types, provider adapters, configuration, and credentials.
- `src/ava/tool/`: built-in file/shell tools and MCP integration.
- `src/ava/base/`, `proc/`, `transport/`: common types, cancellation, subprocesses, and HTTP/SSE.
- `src/ava/app/`: CLI entry points and backend orchestration.
- `src/ava/app/web/`: shared HTTP API and server; `frontend/` contains React/JS/CSS sources, and `assets/` contains bundled web assets.
- `src/ava/app/desktop/`: desktop Python code, `qml/` UI components, and the embedded `terminal/`.
- `tests/`: Python tests and shared fixtures in `conftest.py`; frontend JS tests live beside their sources in `src/ava/app/web/frontend/*.test.js`.
- `eval/`: agent evaluation tooling and benchmarks; see `eval/README.md` before running evaluations.
- `docs/`: usage, architecture, and desktop design guidance.
- `pyproject.toml`: Python dependencies and pytest/Ruff/mypy configuration. `package.json`: frontend scripts. `.github/workflows/ci.yml`: CI checks.

Keep the headless runtime independent of Qt and application routes. The desktop connects to a persistent backend; closing the desktop does not stop backend tasks. See `docs/architecture.md` before changing process ownership or session persistence.

## Setup and running

Run commands from the repository root. Use Python 3.12+ and `uv`; Node.js 22 matches CI.

```sh
uv sync --frozen --extra desktop  # Python development dependencies, including Qt
npm ci                          # Needed for frontend/terminal work
uv run --extra desktop ava-desktop --project .
uv run ava --serve              # Standalone Web UI/API
uv run ava -p "explain this project"  # One-shot CLI task; requires a configured provider
```

Only one API server may own an `AVA_HOME`; do not run a standalone Web server against the persistent backend's home. Use isolated homes for manual testing rather than real user sessions or credentials. See `docs/usage.md` for configuration and launch options.

## Change-focused testing

- Default to focused tests, not the full test suite. Before running tests, identify the code changed for the current task and the behaviors it affects; do not include unrelated pre-existing changes in the working tree.
- Select the smallest meaningful set of tests covering the changed behavior and directly affected callers or integrations. Prefer individual test cases or test files over entire test directories. Include regression tests for bug fixes.
- Measure success by passing checks for the affected behavior with fewer unrelated tests and less test runtime than a full-suite run. Briefly state the selected scope and why it covers the change.
- Expand the test scope only when shared code, public interfaces, dependencies, or configuration create broader impact, or focused failures indicate a wider issue. Run the full suite only when justified by that impact or explicitly requested by the user; explain why before doing so.
- For documentation-only changes, validate the edited content without running application tests unless the documentation affects executable examples or test behavior.
- Report the exact checks run and their results. State any relevant coverage gaps; do not imply the full suite passed when only focused tests were run.

## Test and validation commands

These are alternatives selected by the affected code, not a checklist to run in full.

```sh
# Python: select a relevant file, test node, or keyword within a file.
uv run --extra desktop pytest -q tests/test_session.py
uv run --extra desktop pytest -q tests/test_session.py::test_record_round_trip_preserves_every_block_kind
uv run --extra desktop pytest -q tests/test_session.py -k recovery
# Preview selection when uncertain; ensure it actually selects relevant tests.
uv run --extra desktop pytest --collect-only -q tests/test_session.py -k recovery

# Frontend: select the relevant JS test file.
node --test src/ava/app/web/frontend/markdown.test.js

# Python lint/type checks: replace these example paths with affected files/modules.
uv run --extra desktop ruff check src/ava/session/codec.py tests/test_session.py
uv run --extra desktop mypy src/ava/session/codec.py

# QML: select affected components.
uv run --extra desktop pyside6-qmllint --max-warnings 0 src/ava/app/desktop/qml/Main.qml

# Build validation (no generated bundle updates).
npm run check           # Web frontend and embedded terminal
npm run check:terminal  # Terminal-only changes
```

- Map changes to tests by behavior and imports, not filename alone. Typical starting points are `tests/test_agent.py`, `tests/test_session.py`, `tests/test_context.py`, `tests/test_providers.py`, `tests/test_tools.py`, `tests/test_web.py`, and `tests/test_backend.py`.
- Pytest discovers `tests/` and handles asyncio tests automatically. Reuse the isolated `home`/`project` fixtures and `ScriptedProvider` in `tests/conftest.py` instead of depending on live providers or personal credentials.
- Desktop acceptance tests are in `tests/test_desktop.py` and are deliberately excluded from CI. Run only relevant nodes locally with the desktop extra; inspect their fixtures and opt-in requirements first. Service and SSH tests require isolated environments, not the user's live services.
- Reproduce bug fixes through the affected user-facing flow before changing code where possible, then rerun that flow and the focused regression tests. For UI changes, inspect the actual UI as well as automated assertions; see `docs/desktop-design-system.md`.
- Edit frontend/terminal source, not minified output by hand. When delivering bundle changes, use `npm run build` (web and terminal) or `npm run build:terminal` (terminal only), and review generated diffs for unrelated changes.
- Broader checks, only when justified by the testing policy above: `uv run --extra desktop pytest -q --ignore=tests/test_desktop.py` matches CI's Python test scope; `npm test` runs all frontend JS tests. Refer to `.github/workflows/ci.yml` for the complete lint/type/build gates rather than running every gate by default.
