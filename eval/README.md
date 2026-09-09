# Evaluation tools

Use these tools to evaluate task completion, compare harness versions, and inspect
session behavior. The public [benchmark summary](../docs/benchmark.md) describes
recorded results and their limits. Task collections and experiment outputs are
supplied locally; no particular dataset is selected by these instructions.
Task evaluations run locally and explicitly. CI runs code checks and unit tests;
it does not dispatch evaluations or upload evaluation artifacts.

## Setup

Run from the repository root. Task execution requires Docker; planning and the
scripted runtime checks do not. Install benchmark dependencies separately:

```sh
uv sync
uv venv .venv-eval
uv pip install --python .venv-eval/bin/python -r eval/requirements.txt
uv build --wheel --out-dir eval/cache/wheels
```

The [requirements](requirements.txt), [runtime constraints](constraints.txt), and
[Pi lockfile](integrations/pi/package-lock.json) pin evaluation dependencies.
Rebuild and preserve separate wheels when comparing Ava versions. Source edits
do not change an existing wheel. Refresh runtime constraints when dependencies change:

```sh
uv export --frozen --no-dev --no-emit-project --format requirements-txt \
  --output-file eval/constraints.txt
```

## Supply a task suite

Create `eval/cache/local-suite.json` using materialized Harbor task directories:

```json
{
  "schema_version": 1,
  "name": "local-suite",
  "tasks": [
    {
      "id": "example-task",
      "path": "eval/cache/tasks/example-task",
      "split": "development",
      "origin": "local"
    }
  ]
}
```

Each directory must contain `instruction.md`, `task.toml`, and `tests/test.sh`,
plus its environment and grading assets. A manifest alone is not a task.
Paths are relative to the repository root, not the manifest directory; absolute
local paths also work. Task IDs must be unique lowercase names. Use explicit
`development`, `validation`, or `test` splits and preserve source revisions and
licenses for upstream tasks. The runner hashes task contents, including graders.

An acceptance check should grade the deliverable independently of the agent's
final message. Verify reference solutions and unchanged workspaces before drawing
conclusions from model runs. Keep tasks used to guide a change separate from its
validation tasks. Shared-container grading is not an adversarial security boundary.

## Validate the evaluator

Copy the [controls template](experiments/controls.example.json), point `suite` at
your local manifest, and set budgets for its size:

```sh
cp eval/experiments/controls.example.json eval/cache/controls.json
uv run python -m eval.benchmark plan eval/cache/controls.json
uv run python -m eval.benchmark run eval/cache/controls.json \
  --output eval/results/controls
```

`plan` validates configuration and hashes inputs without starting containers or
calling a model. `run` executes the reference and unchanged-workspace controls.
Investigate failing references or unexpected no-op passes; control scores do not
measure model ability. Output directories must be new.

## Compare Ava and Pi

Copy the [agent template](experiments/ava-vs-pi.example.json) to a local file:

```sh
cp eval/experiments/ava-vs-pi.example.json eval/cache/ava-vs-pi.json
```

Replace both `openai/<model-id>` placeholders with the same exact supported model
and align reasoning effort. Placeholders deliberately fail validation. These
adapters support `openai` and `anthropic` API credentials through
`OPENAI_API_KEY` or `ANTHROPIC_API_KEY`, and `codex` through a local Codex OAuth login.
The common Anthropic adapter currently requires `effort: "off"`. Verify model and
effort support rather than assuming a model name implies compatible endpoints.

Set Ava's `wheel` path, suite, repetitions, timeout, and `max_trials` explicitly.
The schedule size is tasks × repetitions × agents. Both adapters expose
read/edit/write/bash; prompts, tool implementations, and provider APIs may differ.
Both agents load native project instructions; Pi extensions, skills, and prompt templates are disabled.
Record native instruction-discovery differences as part of the comparison.

### Codex OAuth without an API key

Set both agents' `model` to `codex/<exact-model-id>` and use the same explicit reasoning effort.
The Pi adapter maps this provider to its native `openai-codex` provider.
Its pinned catalog must support that exact model and effort; unsupported selections fail before inference.
For longer tasks, explicitly enable `compaction` for both agents and use the dataset's task budget.

The runner reads `$CODEX_HOME/auth.json`, or `~/.codex/auth.json`, without changing the login.
It injects an access-only credential snapshot into a private temporary directory inside each task container and removes it afterward.
Refresh tokens and API keys are excluded from the snapshot; credentials never enter frozen inputs or mounted agent logs.
Each trial reads the current local login, so an expired credential requires refreshing Codex before continuing.
There is no API-key fallback and no automatic refresh of the shared login.

These runs use the ChatGPT/Codex service through the local login, not locally hosted model inference.
Subscription limits still apply.
Token counts and elapsed time are measured normally; catalog or configured token prices are estimates, not subscription charges.
Leave `pricing` and `stop_after_estimated_usd` unset when no API-equivalent cost estimate is wanted.

```sh
uv run python -m eval.benchmark plan eval/cache/ava-vs-pi.json
uv run python -m eval.benchmark run eval/cache/ava-vs-pi.json \
  --output eval/results/ava-vs-pi
uv run python -m eval.benchmark compare \
  eval/results/ava-vs-pi eval/results/ava-vs-pi \
  --baseline-agent pi-baseline --candidate-agent ava-baseline --mode harness \
  --output eval/results/ava-vs-pi/comparison.json
```

Runs use seeded shuffled ordering, sequential trials, fresh environments, and no
automatic trial retry. The seed controls ordering, not model randomness. Missing
grades or infrastructure failures stop the run; incomplete coverage cannot become
a complete success rate or paired comparison. Preserve every attempted trial.

| Mode | Intended comparison |
| --- | --- |
| `version` | Same agent kind, model, and effort; separate baseline/candidate wheels or prompts |
| `harness` | Different agents with the same model and effort |
| `product` | Complete products, including model/configuration differences; native CLI versions must be pinned |

`compare` checks task hashes/splits, evaluator, host architecture, repetitions,
timeout, and pricing; same-model modes also check model and effort. Other settings
must be reviewed against the intended intervention. Separate historical runs have
time/backend confounders. Regressions cause a nonzero exit; this is not a universal
promotion policy. Task-level intervals are descriptive, not proof of equivalence.

## Measurements and outputs

Correctness comes first. Compare complete paired outcomes alongside all-attempt
input/cache/output tokens, model attempts, tool calls/errors, execution time, and
missing measurements. Keep failures in resource totals. Characters and tool-output
bytes are not provider tokens. Unknown measurements remain `null`.

Each run preserves `experiment.json` (configuration, schedule, hashes), `inputs/`
(frozen tasks and wheels), `trials.jsonl`, `summary.json`, and raw `jobs/` artifacts.
On Alpine task images, Ava uses the image's Python 3.12 because the pinned
standalone musl Python has an extension suffix incompatible with musl wheels.
Older Alpine images get a checksum-pinned Python 3.12.11 source build under
`/opt`, with temporary build dependencies removed afterward.
Pi uses the checksum-pinned Node 22.19.0 musl build from Node's unofficial-builds
project on Alpine x86_64; other supported images use the pinned glibc build.
The musl Node binary uses private, checksum-pinned C++ runtime libraries under
`/opt/pi-node/lib`, with its library search path set during installation.
Both installers check CLI startup before model execution.
The image digest, installer snapshot, and install logs record these runtime differences.

Timing separates environment setup, installation, and execution. Keep detailed
evidence locally; sessions and tool outputs can contain sensitive material.

Optional `pricing` entries are keyed by exact model string and contain `input`,
`cached_input`, and `output` USD-per-million rates plus `source` and `as_of`.
The generic three-rate estimator cannot represent cache-write charges and leaves
cost unavailable when unsupported or usage is missing. Estimates are not invoices.
`stop_after_estimated_usd` requires prices for live models and is checked between
trials, so it is not a hard spending cap.

## Other tools

Inspect existing sessions without rerunning a model:

```sh
uv run ava session inspect eval/cache/session.jsonl.zst
uv run python -m eval.diagnose eval/cache/session.jsonl.zst \
  --output eval/results/diagnosis.json
```

Repeated calls and large results are investigation signals, not automatic waste.
The optional [prompt-proposal tool](evolve.py) prepares development evidence and
proposes changes; proposals require fresh task evaluation and are not accepted
automatically. See `uv run python -m eval.evolve --help` for its interface.

The [incident materializer](incidents.py) consumes an explicit local manifest:

```sh
uv run python -m eval.incidents --manifest eval/cache/incidents.json \
  --output eval/cache/prepared-tasks
```

For official dataset integrations, use the retained [adapters](integrations/) with
your own pinned selection and downloaded task assets. Controls on a small selection
are not full-benchmark results.

The [SWE-bench Pro preparer](integrations/swebench_pro.py) derives tasks with separate agent and grading containers.
The upstream images retain solution commits, so the agent image replaces Git history with a source-only baseline.
After execution, Harbor transfers the generated patch to a fresh upstream image containing the original grader and gold test history.
The preparer retains the original instructions, reference solution, and grading script, and records the adaptation and source hashes.
Qualify each selected task with reference and unchanged-workspace controls before measuring agents.

```sh
uv run python -m eval.integrations.swebench_pro eval/cache/upstream-suite.json \
  --image-digests eval/cache/image-digests.json \
  --output eval/cache/isolated-suite
```

The image-digest file maps each upstream `jefzda/sweap-images:<tag>` to its `sha256:...` digest.
Missing pins or source changes fail preparation; output directories must be new.
Use the resulting `suite.json` in the controls and agent experiments, and disclose this adaptation with reported results.
This isolates the grader from the agent's container; it does not establish immunity to public-data contamination or malicious submissions.

On SELinux hosts, benchmark result directories must have a container-compatible label before Harbor creates bind-mounted logs.
Harbor also requires Docker Compose options that some `podman-compose` releases do not implement.
Record the actual engine and Compose versions alongside the experiment.

Scripted runtime checks need no API key or Docker:

```sh
uv run python -m eval.run regression --output eval/results/runtime-baseline
uv run python -m eval.run regression --output eval/results/runtime-candidate
uv run python -m eval.run compare eval/results/runtime-baseline eval/results/runtime-candidate
```

They test runtime invariants, not model intelligence. Optional session replay
checks recorded protocol behavior; it cannot predict a changed agent's decisions
and does not replace independent task grading.
