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

On September 8, 2026, Ava and Pi 0.85.1 completed **22 SWE-bench Pro development tasks across 11 repositories**, with one attempt per agent per task.
Both used `gpt-6-astra` with `medium` reasoning through Codex OAuth.

| Measure | Ava | Pi |
| --- | ---: | ---: |
| Tasks solved | 11/22 (50.0%) | 10/22 (45.5%) |
| Input tokens, including cached input | 4,571,720 | 4,275,286 |
| Output tokens, including reasoning | 87,128 | 81,101 |
| Tool calls | 515 | 475 |
| Mean agent execution, minutes/task | 3.65 | 3.18 |

Both agents solved 10 tasks; Ava alone solved one.
All 44 attempts were graded, including one interrupted Pi attempt.
This small, equally weighted repository sample does not establish general superiority or a full 731-task benchmark score.
Per-request subscription cost was not measured.

Evaluation uses fresh environments, matched model settings and budgets, reference and unchanged-workspace controls, and independent patch grading.
Failed attempts are retained in scores and resource totals.
Detailed experiment records stay local; CI does not run evaluations.

[Benchmark results and limitations](docs/benchmark.md) ·
[How to run evaluations](eval/README.md)

## Documentation

- [Usage and configuration](docs/usage.md): CLI, Python API, custom providers and development.
- [Architecture](docs/architecture.md): agent loop, tools, context and session recovery.
- [Evaluation guide](eval/README.md): setup, controls, comparisons and session diagnosis.
- [Port notes](docs/port-notes.md): differences from the original C++ runtime.

## License

[MIT](LICENSE) © 2026 Min Liu.
