# Goal-mode evaluation — 2026-09-14

**Decision: keep `/goal` opt-in and experimental.** The implementation adds tested
lifecycle controls, but the live screen did **not** demonstrate more verified
completions than ordinary Ava. Both arms solved all four small tasks; goal mode
used more tokens and time. The promotion gates in the [case contract](../eval/goal-cases.md)
are not met.

## Valid live screen

Native Codex provider, `gpt-6-astra`, medium effort, compaction disabled, one
repetition per task/arm, shared 180-second timeout, fresh Harbor task environments.
The ordinary baseline wheel was frozen before implementation. The current runtime
rebuild matches the evaluated goal wheel byte-for-byte.

| Task | Ordinary Ava | Goal mode | Ordinary tokens | Goal tokens | Ordinary seconds | Goal seconds |
|---|---|---|---:|---:|---:|---:|
| G01: migration and callers | Pass | Pass | 9,380 | 16,369 | 41.1 | 52.6 |
| G02: exact CSV totals | Pass | Pass | 12,583 | 19,589 | 57.5 | 67.5 |
| G03: already satisfied | Pass | Pass | 5,940 | 17,272 | 25.8 | 59.1 |
| G04: streaming CLI, help, docs | Pass | Pass | 11,976 | 23,021 | 67.1 | 91.0 |
| **Total** | **4/4** | **4/4** | **39,879** | **76,251** | **191.5** | **270.2** |

- Goal mode used **1.91× tokens** and **1.41× agent execution time** in these selected
  trials. These are descriptive ratios, not statistically established effects.
- Tool calls: **27 → 30**. Model attempts, including audits: **20 → 31**.
- All four goal statuses were `complete`, with **zero false completions against the
  qualified graders** in these four trials. This is not a universal correctness guarantee.
- Goal turns were 1, 1, 2, and 1 respectively. The extra turn on the already-correct
  task shows the evaluator can require redundant verification.
- Tokens are inclusive input plus inclusive output; cached input and reasoning
  are already included. **Dollar cost is unavailable**, not zero.
- Time is Harbor's agent-execution wall time, excluding environment/installation
  time. Goal limits did not add a matched aggregate-token ceiling to the old baseline.

[Machine-readable results and hashes](../eval/goal-results.json) retain the selected
trial measurements and provenance.

### Which trials count

This table combines G01–G03 from `goal-final-screen` with the fresh G04 pair from
`goal-qualified-artifacts`. Every selected task hash was checked against the current
qualified task set. G04's earlier grades were invalidated by a documentation-example
oracle defect; those raw rows were preserved, not silently changed into passes.
Both arms were rerun on the corrected G04 oracle with the same wheels and settings.

This is a **two-batch, one-repetition screen**, not the originally proposed
three-repetition promotion experiment. Live model baseline trials began after code
implementation, although the baseline wheel was frozen beforehand. The task image
was `python:3.12-slim`, not a digest-pinned image. These limitations preclude a release
or general-superiority claim. No live Claude Code-versus-Codex comparison was run.

## Defects the evaluation exposed

1. **Continuation placement caused repeated checks.** An early goal candidate put
   a fresh user instruction after every tool result. On G02 it fixed the program
   and passed its tests, then repeatedly reran them: 55 tool calls, 219,543 reported
   tokens, and a 180-second timeout. Goal continuation is now one durable instruction
   per turn; compaction restores it before the retained tail, not after tool results.
   The corrected G02 trial passed in 67.5 seconds with 19,589 tokens. A deterministic
   regression checks the request boundary, and a separate test verifies the 50-step
   per-turn safety bound. This is a fixed development regression, not evidence that
   goal mode beats the ordinary baseline.
2. **Benchmark installation omitted a dependency.** The first baseline setup failed
   with `ModuleNotFoundError: No module named 'yaml'`. Refreshing the frozen runtime
   dependency export fixed installation for both wheels.
3. **The reward channel included program stdout.** The first migration grade emitted
   `Ada\n1\n` where Harbor required a numeric reward. Reward writing is now separate
   from grader stdout, with a shell-level reference-control test.
4. **The G04 docs oracle overfit the reference.** It assumed the first shell fence
   was the filter example, and that it printed one particular JSON object. Valid
   README files preserved an earlier version example and printed multiple matching
   JSON records. The failures were `JSONDecodeError: Extra data` and then
   `AssertionError` at `assert 'filter' in example[1]`. The oracle now finds runnable filter examples without requiring a
   particular fence label, field name, or record count. Controls cover alternate
   valid docs, version-only docs, and a genuinely buffering implementation. Neither
   the worker nor auditor prompt was tuned to these G04 grading failures.
5. **Accounting and recovery needed hardening.** Reasoning tokens are counted once;
   audit usage no longer overwrites worker context measurements; interrupted goals
   fail closed when spend may be unreported; rejected duplicate drivers cannot clear
   the active driver's goal identity. Required command results have durable evidence
   and are included in tool-call inspection.

The initial broken pilot was stopped after its loop regression was identified.
Setup attempts, invalid-oracle trials, and pilot work are **not** included in the
selected-trial totals above. One interrupted pilot lacks complete usage harvesting;
these numbers are not the total cost of developing the feature.

## Runtime and UI checks

- Final non-native Python suite: **297 passed, 3 skipped**; focused goal/grader
  suite: **22 passed**.
- Focused desktop tests: **5 passed**, including the PDF attachment test after the
  concurrent click-helper fix. The earlier PDF-only reproducer ended with
  `Command timed out after 60 seconds and its process group was stopped.` That
  unrelated fix was preserved. A full native desktop suite was not completed.
- JavaScript tests: **10 passed**. Ruff, mypy over all 99 source files, frontend
  checks/build, and `git diff --check` passed.
- Real-browser checks exercised start, status, pause, clear, persisted replay,
  footer state, and title updates. Repeated accounting notices were removed.
  The final browser session reported no console errors or warnings; screenshots
  were retained for review. This was not a cross-viewport pixel-diff campaign.
- The ordinary synthetic runtime baseline and final candidate both passed
  `edit-and-test` and `provider-outage`: **2/2 each, no regression**. Those tests are
  not model-quality measurements.

Lifecycle coverage includes automatic continuation, false audit verdicts rejected
by required command checks, three-turn stalls, hard turn/step/token limits, missing
usage, pause/clear/replacement while in flight, awaited-tool batches, duplicate
admission, interrupted-log recovery, reopen, and compaction projection. It is not
exhaustive process-kill fault injection or background-subagent coverage.

## Remaining promotion work

- Use a larger, independently held-out set with genuine premature-stopping failures;
  the four-task golden screen is already at the ordinary baseline's ceiling.
- Run the planned paired repetitions with pinned images and explicit resource bounds.
- Establish that the evaluator's additional verified completions justify its cost;
  do not introduce another evaluator/provider configuration layer without that evidence.
- Complete native UI and crash-point coverage before treating this as a production
  autonomous-work guarantee. Detached-job supervision and child-agent accounting
  remain unsupported, as described in [goal usage](goals.md).

Raw evidence lives under this terminal session's scratch directory
`ava-scratch-2c1e9hsl`: `goal-final-screen`, `goal-qualified-artifacts`,
`goal-qualified-plan.json`, the retained earlier run directories, runtime baseline
and comparison directories, and `goal-paused*.png`. No credentials or raw model
reasoning were copied into this report.
