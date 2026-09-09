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

Ava is a compact Python coding agent with a CLI, local Web UI, and embeddable API.
It reads and edits code, runs commands, and resumes work from saved sessions.
Harness changes are measured against independently graded coding tasks.

[Quick start](#quick-start) · [Benchmark](#benchmark) ·
[Usage and configuration](docs/usage.md) · [Architecture](docs/architecture.md)

## Quick start

Requires Python 3.12+, [uv](https://docs.astral.sh/uv/), and macOS or Linux.

```sh
git clone https://github.com/SmartAI/ava.git
cd ava
uv sync
export ANTHROPIC_API_KEY=...
uv run ava --serve
```

Open <http://127.0.0.1:8777> and choose a project, or use the CLI:

```sh
uv run ava -p "Explain this project"
uv run ava -c -p "Add tests for the main failure paths"
```

For OpenAI, set `OPENAI_API_KEY` and add `--provider openai`. For an existing
Codex CLI login, use `--provider codex`. Select a supported model and reasoning
level with `--model` and `--effort`.

Ava is alpha software. Tools run with your local permissions and without per-call
approval; session logs may contain sensitive content.

For the optional Qt Quick desktop app, run `uv run --extra desktop ava-desktop --project .`.
See the [desktop usage guide](docs/usage.md#qt-quick-desktop-app) for setup and current scope.

## Benchmark

The latest iteration repaired Bash output truncation and process completion
handling. On **three development tasks, five runs each**, using `gpt-5.6-sol`
with `low` reasoning, updated Ava was compared with historical Pi 0.85.1 results.

| Measure | Earlier Ava | Updated Ava | Pi |
| --- | ---: | ---: | ---: |
| Task passes, all 15 runs | 14/15 | 14/15 | 15/15 |
| Estimated cost, 14 matching complete-usage runs | $1.1790 | $1.0577 | $1.0321 |
| Cost difference from Pi on those 14 runs | +14.2% | +2.5% | Reference |

That matched subset cost **10.3% less than earlier Ava**. One updated Ava run
ended with a TLS error and unknown request usage, so full-cohort cost is unknown.
The 14-run subset is diagnostic; these exposed tasks and historical comparisons
do not establish general capability or cost parity.

Evaluation uses isolated environments, fixed model settings, independent task
grading, and correctness, cost, token, tool-call and latency measurements.
Failed attempts are retained. Detailed experiment records stay local; CI does
not run evaluations.

[Benchmark results and limitations](docs/benchmark.md) ·
[How to run evaluations](eval/README.md)

## Documentation

- [Usage and configuration](docs/usage.md): CLI, Python API, custom providers and development.
- [Architecture](docs/architecture.md): agent loop, tools, context and session recovery.
- [Evaluation guide](eval/README.md): setup, controls, comparisons and session diagnosis.
- [Port notes](docs/port-notes.md): differences from the original C++ runtime.

## License

[MIT](LICENSE) © 2026 Min Liu.
