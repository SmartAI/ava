# Benchmark

Ava evaluates harness changes through independently graded tasks and explicit
resource accounting. These are small development benchmarks, not full SWE-bench
or Terminal-Bench scores.

## Method

Freeze the runtime, task environment, model settings, budgets, graders and
acceptance criteria before running. Validate reference solutions and unchanged
workspace controls. Start with targeted tool checks and a small agent pilot,
then evaluate promising changes on a larger cohort. Evaluate combinations
separately; component savings cannot be added together.

Correctness comes first. Record cost, total/cached/uncached input, output including
reasoning, model requests, tool calls and active latency. Include unsuccessful
attempts; missing usage remains unknown. Costs use fixed API-equivalent prices,
not subscription billing. Active latency excludes environment setup and grading.

The studies below use `gpt-5.6-sol` with `low` reasoning, isolated task environments,
and public Terminal-Bench tasks plus private repository-derived component tasks.
Tasks have been used during development and are not a fresh holdout. Detailed
protocols, datasets, traces and analysis remain local. Evaluations are manually
initiated and do not run in CI.

## Evolution

Earlier studies investigated stable session affinity, multi-tool responses,
quieter progress and batch editing. On a 24-attempt development cohort, the
quiet-progress and batch-edit combination reduced observed cost by 9.2% against
the preceding Ava version, with both passing 22/24. This result belongs to that
cohort and must not be combined with percentages from later studies.

The latest iteration focused on tool correctness: Bash display truncation no
longer stops command execution, overflow is retained in a bounded local log,
and process completion is handled separately from inherited output pipes.
File-tool argument handling and cancellation checks were also improved.

## Latest comparison

The baseline comparison ran three tasks five times per agent: 15 Ava and 15
Pi 0.85.1 attempts, on the same model and settings. Both used fresh sessions,
native read/bash/edit/write tools, equal budgets, and no retries or compaction.
Their native prompts and tool behavior were retained. Baseline Ava passed 14/15
and Pi passed 15/15; Ava's total estimated cost was 14.1% higher.

After tool-contract repairs, 15 new Ava attempts reused the historical Pi results
to reduce evaluation expense. Ava passed 14/15. One TLS interruption left one
request's usage unknown; that failure was retained without retry or replacement.
The remaining scheduled attempts continued under a documented amendment to the
original stop policy.

| Measure | Earlier Ava | Updated Ava | Historical Pi |
| --- | ---: | ---: | ---: |
| Passed, all 15 attempts | 14/15 | 14/15 | 15/15 |
| Full estimated cost | $1.2704 | Unknown; at least $1.1016 | $1.1135 |
| Cost on 14 matching complete-usage slots | $1.1790 | $1.0577 | $1.0321 |
| Cost difference from Pi on those 14 slots | +14.2% | +2.5% | Reference |
| Model requests on those 14 slots | 113 | 90 | 90 |
| Tool calls on those 14 slots | 152 | 110 | 93 |

The 14-slot analysis removes the interrupted slot from every arm and is a
secondary diagnostic, not a replacement for the full cohort. Updated Ava's cost
on this subset fell 10.3% against earlier Ava. No output-limit command stops or
Bash timeouts were recorded in the updated cohort. Separate native checks confirm
the repaired tool behavior; the historical agent comparison cannot attribute an
exact share of savings to each change.

## Interpretation

The observed cost gap narrowed, but parity and statistical equivalence are not
established. The comparison uses three exposed tasks, a historical Pi baseline,
and one incomplete-usage attempt. Repetitions measure variation on these tasks,
not diversity or generalization. Initial paired run order was also unbalanced.

On the 14 matching slots, updated Ava and Pi made the same number of model
requests, while Ava generated 12.1% more output tokens and used 18.3% more tool
calls. Ava's cache share was higher; cache percentage alone does not explain cost.
The next evaluation should separate transport reliability from output and tool-use
efficiency, then check any proposed improvement on new tasks with fresh controls.
