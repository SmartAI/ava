# Long-horizon goal evaluation

## Question and baseline (freeze before model screening)

Do durable goals and an independent completion audit increase **fully verified
completion of long, interdependent tasks**, or merely spend more on work ordinary
Ava already completes? The four small golden cases remain regression checks, not
primary evidence for this question.

The pinned candidate pool is in [goal-long-cases.json](goal-long-cases.json).
Selection used published instructions and environment suitability, not agent wins.
None of these tasks is declared to require multiple agent turns before screening.
A single ordinary agent turn may make unlimited tool/model calls subject to the
shared wall-clock limit; do not split its work artificially.

| Candidate | Split | Real dependencies to exercise |
|---|---|---|
| `schemelike-metacircular-eval` | Development | Language semantics → environments/closures/recursion → I/O forwarding → interpreting itself |
| `llm-inference-batching-scheduler` | Development | Feasible plans → exact coverage and global shapes → joint cost/padding/latency constraints across two datasets |
| `make-mips-interpreter` | Validation | ELF/memory → instruction execution → syscalls/file I/O → boot Doom and render the actual first frame |

These are upstream Terminal-Bench tasks, not enlarged versions of the toy projects.
For context only, upstream expert-time estimates are 300, 45, and 480 minutes;
those estimates are **not measured Ava run times**. No GPU is required. The pinned
prebuilt images are amd64; execution on this Apple Silicon Docker host uses emulation.
CPU-sensitive verifier failures must be investigated during reference qualification.

## Experimental sequence

1. **Qualify controls first:** reference must pass and unchanged workspace must fail
   for each task. Inspect test execution and archive collection, not just reward
   file presence. A failing reference or infrastructure error blocks inference on
   that task. Keep original grader results; do not weaken assertions to get passes.
2. **Ordinary-only screen first:** one attempt per candidate, original objective,
   no automatic follow-up. Record what is incomplete when the worker actually ends.
   A timeout while still working is not premature stopping.
3. **Probe the mechanism:** compare ordinary execution (A), ordinary execution plus
   two fixed generic follow-ups (B), and `/goal` (C). All use the **same frozen Ava
   wheel**, model, effort, tools, compaction settings, and total time allowance.
   B has `continuations: 2`; C has `goal: true`. These options are mutually exclusive.
   The evaluator never supplies private grader failures to any arm.
4. **Confirm rather than cherry-pick:** retain the entire selected pool in reporting,
   including tasks A solves immediately and tasks all arms fail. Use development
   trajectories for changes, never tune the worker/auditor on MIPS validation results.
   If validation evidence is used for tuning, reclassify it and choose a new holdout.
   A later promotion comparison needs three paired repetitions, not a single screen.

Initial settings: `codex/gpt-6-astra`, `medium`, compaction **enabled**, 1,800 seconds
per agent trial, one screening repetition, schedule seed 42. The whole B sequence
shares one Harbor timeout; a follow-up does not replenish time. Audits and compaction
count in tokens/latency. No matched aggregate-token cap is currently implemented;
report resource-conditioned results, not a matched-token-budget claim. Provider
costs remain unknown when the service does not report them.

B uses one CLI process and one Agent instance, preserving its resolved prompt,
provider, scratchpad, and session until all turns finish. B always sends up to two
generic follow-ups after a successful native turn, even
if the artifact already passes; it is a deliberately simple continuation control,
not an oracle-assisted controller. It does not retry provider/CLI failures. Before
each follow-up, archive `/app` to `checkpoint-N.tar.gz` in the agent logs so the
pre-nudge deliverable can later be graded independently. No hidden test is executed
or returned to the agent between turns. Checkpoint time is included in B's runtime.
The existing goal mode's 20-turn/50-step safety bounds remain part of C and must be
reported if reached; do not cap A to make C look better.

## Metrics and interpretation

Primary: whole-objective artifact pass/fail from independent upstream tests at the
stopped workspace. Separately report on-time, clean runtime completion: the runner's
`correct` field marks timeouts/errors as failures even if the grader's `reward` is 1.
Do not describe an increase in clean runtime completion as an increase in verified
artifacts. Both axes, including their disagreements, must be visible.

Secondary: failed acceptance groups, fixed-follow-up count, natural worker turns, false goal
completion, total input/cache/output tokens, model attempts, tool calls/errors,
compactions, agent and verifier time, and dollars only if available. Keep per-test
CTRF output and original stdout; an assertion count is not a semantic progress score.
The workstream labels above are analysis categories, not invented partial rewards.

Classify each outcome from the trajectory **and artifact evidence**:

- **Already complete:** A passes at its first natural stop. This is a ceiling case,
  not evidence for autonomous continuation, and is not silently removed.
- **Recoverable early stop:** A stops normally with incomplete work; a pre-nudge
  checkpoint and a later independently passing checkpoint show generic continuation
  can recover. A final message saying "done" is insufficient to establish this.
- **Goal-package gain:** C completes where the equally time-budgeted B control does
  not. This measures the combined goal prompt, auditor, stopping rules, and runtime;
  it does not isolate the auditor itself. Auditor-only attribution would require
  another ablation with the same goal prompt and other settings. Repetition and
  holdout confirmation are still required.
- **Capability/budget problem:** continued work still fails, or the agent never stops
  before timeout. More turns alone have not been shown to solve it.
- **Infrastructure/grader failure:** no model-quality score until the evaluator works.

Promotion requires additional verified completions on matched valid pairs, no
observed false goal completion/control violation, and overhead justified by those
completions. A harder task list, longer run, or larger turn count is not itself a win.

## Preparation and provenance

Keep sources, environments, wheels, and runs in the invocation's scratch directory.
The preparer requires a clean checkout at the catalog's exact upstream revision:

```sh
git clone --depth 1 --filter=blob:none --no-checkout \
  https://github.com/laude-institute/terminal-bench-2.git "$SCRATCH/terminal-bench-2"
git -C "$SCRATCH/terminal-bench-2" fetch --depth 1 origin \
  2fd12b88aafdd04a52c298e3940bcb189f9766d6
git -C "$SCRATCH/terminal-bench-2" sparse-checkout set \
  schemelike-metacircular-eval llm-inference-batching-scheduler make-mips-interpreter
git -C "$SCRATCH/terminal-bench-2" checkout 2fd12b88aafdd04a52c298e3940bcb189f9766d6
uv run python -m eval.goal_long --source "$SCRATCH/terminal-bench-2" \
  --output "$SCRATCH/goal-long-tasks"
```

Use the generated `suite.json` with `eval.benchmark`. The repository retains only
the candidate catalog and preparation code, not upstream task/solution corpora.
Upstream license/canary markers are preserved in local task copies. The preparer:

- pins the **complete prebuilt images by digest**, including for explicit rebuilds;
- records upstream revision, task/grader hashes, image digests, and adaptations;
- preserves original acceptance tests, deadlines, and reference solutions;
- explicitly requires Scheme's supplied `interp.py` to remain byte-identical, so
  changing the reference cannot make direct and interpreted outputs agree falsely;
- archives the whole `/app` workspace before grading for post-run inspection/regrading.

The Scheme immutability condition is disclosed identically to every arm. Scheduler
input integrity is already enforced upstream. Graders execute in the original
shared-container arrangement; this is not an adversarial sandbox. Archives can be
large and contain executable agent output: inspect them as untrusted artifacts and
regrade only in disposable containers, never by executing them on the host.

## Status

The initial multi-process continuation prototype failed its E2E prompt-preservation
check and was replaced by the single-Agent driver. Local lifecycle tests and a
frozen-wheel Harbor smoke run verify three native turns, unchanged custom prompt,
and pre-nudge checkpoints. These are runtime checks, not model-quality evidence.

The rebuilt ordinary-only pilot reproduced a Harbor cancellation gap: after
`Agent execution timed out after 1800.0 seconds`, the worker and its detached tool
processes remained alive while verification began. This pilot is excluded. A
Docker-PID-namespace guard now terminates new benchmark processes on failed runs
before harvesting/grading, while preserving pre-existing processes and successful
trials' background services. Detached-process and wrong-namespace tests pass; a
2-second Harbor timeout smoke verifies no worker survives into grading. Cleanup
failure is an infrastructure error, not a valid model score.

The previous scratch directory became unavailable, so its incomplete long-task
screen is excluded from the new comparison. The rebuilt study runs control
qualification, an ordinary-only screen, and three arms × three tasks × three
repetitions (27 comparison trials). The wheel is identical across arms. This can
consume up to 13.5 comparison agent-hours plus baseline/setup/verification time.

Local incremental results in `eval/goal-long-results.json` preserve completed-trial
measurements and grader outcomes outside scratch. This raw report is excluded from
Git because it contains machine-specific paths. Missing phases remain explicitly
missing; final interpretation must wait for the comparison and checkpoint grading.
