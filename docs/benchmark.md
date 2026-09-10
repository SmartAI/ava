# Benchmark

Ava evaluates coding-task completion through independent grading and records resource use for successful and unsuccessful attempts.
The latest completed comparison is a 22-task SWE-bench Pro development pilot from September 8, 2026.
It is not a full benchmark score or evidence of general superiority.

## Latest comparison: SWE-bench Pro pilot

The run covers two tasks from each of 11 repositories, with one attempt per agent per task and 44 graded attempts in total.
Ava solved 11/22 tasks (50.0%); Pi solved 10/22 (45.5%).

| Paired outcome | Tasks |
| --- | ---: |
| Both solved | 10 |
| Ava only | 1 |
| Pi only | 0 |
| Neither solved | 11 |

The single differing outcome was a NodeBB avatar-background task: Ava passed all 195 required tests, while Pi passed 194 and failed the invalid-colour fallback case.
The observed difference is 4.5 percentage points, based on one discordant pair.
It does not establish statistical superiority.

| Repository | Tasks | Ava solved | Pi solved |
| --- | ---: | ---: | ---: |
| ansible | 2 | 2 | 2 |
| element-hq | 2 | 1 | 1 |
| flipt-io | 2 | 0 | 0 |
| future-architect | 2 | 1 | 1 |
| gravitational | 2 | 0 | 0 |
| internetarchive | 2 | 2 | 2 |
| navidrome | 2 | 1 | 1 |
| nodebb | 2 | 2 | 1 |
| protonmail | 2 | 0 | 0 |
| qutebrowser | 2 | 0 | 0 |
| tutao | 2 | 2 | 2 |

### Resource use

These totals include all attempts, including failures and the interrupted attempt.

| Metric | Ava | Pi |
| --- | ---: | ---: |
| Input tokens, including cached input | 4,571,720 | 4,275,286 |
| Cached input tokens | 3,885,952 | 3,759,488 |
| Output tokens, including reasoning | 87,128 | 81,101 |
| Tool calls | 515 | 475 |
| Tool errors | 56 | 60 |
| Model attempts | 354 | 381 |
| Compactions | 0 | 0 |
| Mean agent execution, minutes/task | 3.65 | 3.18 |
| Median agent execution, minutes/task | 2.69 | 2.69 |
| Mean installation, minutes/task | 0.47 | 0.20 |
| Mean complete trial, minutes/task | 6.78 | 6.09 |

Cached input is already included in input tokens and must not be added again.
Agent execution includes tool work; complete trial time also includes environment preparation, installation, and verification.
Trials ran sequentially on a shared host, so timings are descriptive rather than isolated hardware measurements.
Installation includes native Python compilation on two older Alpine images.
Both agents used Codex OAuth; per-request subscription billing was unavailable, and catalog-derived dollar estimates are excluded.

### Method and reproducibility

- Dataset: SWE-bench Pro public revision `7ab5114912baf22bb098818e604c02fe7ad2c11f`.
- Upstream evaluator revision: `ca10a60a5fcae51e6948ffe1485d4153d421e6c5`; Harbor 0.22.0.
- Selection: two tasks per repository, seed 20260907; this is development data.
- Agents: Ava and Pi 0.85.1, both using `codex/gpt-6-astra`, medium reasoning, and native project instructions.
- Compaction was enabled for both agents but did not occur in this run.
- Scheduling: seed 42, shuffled task and agent order, fresh sessions and environments, no task retries.
- Budgets: one CPU, 4 GiB, 3,000 seconds for agent execution, and a separate 1,800-second setup limit.
- Every selected task passed its reference-patch control and failed its unchanged-workspace control before model execution.
- Agent images expose source-only Git history; generated patches are graded in separate fresh upstream containers.
- Task contents, image digests, installers, dependencies, and agent artifacts were frozen for the run.

The scheduling seed controls order, not model randomness.
Native prompts, instruction discovery, tools, and provider implementations may differ between agents.
Harbor's Element task scripts retain `--maxWorkers=1 --forceExit` Jest flags relative to the upstream evaluator.
One image required rootless ownership squashing; Alpine images required runtime adaptations, with passing controls and compatibility checks recorded locally.

| Frozen artifact | SHA-256 |
| --- | --- |
| Ava wheel | `3e2a83bb07b6cd896744590e1ca25a682dff3ced4dfcc299514d95244988a91b` |
| Pi lockfile | `5db07d1384f06527077670bf7faac4d1bcb3e2247ec47beed105412847d210bb` |
| Evaluator | `42fc813aa6448e98a0ba00229faf8858be0ad45907df790f21b1412b0dafbb0e` |
| Adapter | `d1286b558456bcc430b5c739f168c7ad689459786a2ca8fd4f060b20176f7380` |
| Task set | `80ac34e82234be151782b2354e616309fe7ce6849d0ced9c2bfe4ea7687d7300` |

### Interruptions and limits

All 44 attempts received grades; no attempt remained ungraded due to setup or infrastructure failure.
One Pi attempt ended with a WebSocket error after 27 model attempts, and its resulting workspace was graded under the original no-retry policy.
Removing that entire task pair leaves Ava at 11/21 and Pi at 10/21; this does not predict what an uninterrupted attempt would have achieved.

Equal repository weighting differs from the full dataset distribution, tasks within a repository can be correlated, and there is only one attempt per agent per task.
The full 731-task configuration was prepared but has not been executed.
The pilot does not separately evaluate memory, interactive steering, desktop UX, or long-context behavior.

### Follow-up evaluation, September 8-9

A prompt candidate added general instructions to map requirements to interfaces, review the final diff, and investigate failed repository checks.
On the 11 tasks Ava previously failed, a fresh baseline and the candidate each completed 11 graded attempts and solved 0/11.
One baseline attempt was interrupted; excluding that pair still leaves no wins for either version.
The candidate used 348 tool calls and 69.79 agent minutes, versus 289 calls and 46.30 minutes for the baseline.
Baseline token totals were incomplete and remain unknown.
The candidate showed no correctness improvement and was not retained.
These selected failed-task reruns do not replace or augment the original 22-task score.

Follow-up investigation found grader/interface compatibility concerns alongside behavioral failures.
Nine manually assisted replays across two tasks isolated missing fixtures, a required middleware call sequence, and undocumented private helper expectations.
Diagnostic repairs passed those two unchanged graders, but used known-test information and zero model calls.
They are not additional agent successes, do not change the published scores, and do not establish that every remaining failure is a grader defect.

## Earlier development studies

The following studies used `gpt-5.6-sol` with low reasoning and different task sets and configurations.
Their results are historical and must not be combined with the SWE-bench Pro pilot.

### Evolution

Earlier studies investigated stable session affinity, multi-tool responses, quieter progress and batch editing.
On a 24-attempt development cohort, the quiet-progress and batch-edit combination reduced observed cost by 9.2% against the preceding Ava version, with both passing 22/24.
This result belongs to that cohort and must not be combined with percentages from later studies.

That iteration focused on tool correctness: Bash display truncation no longer stops command execution, overflow is retained in a bounded local log, and process completion is handled separately from inherited output pipes.
File-tool argument handling and cancellation checks were also improved.

### Historical three-task comparison

The baseline comparison ran three tasks five times per agent: 15 Ava and 15 Pi 0.85.1 attempts, on the same model and settings.
Both used fresh sessions, native read/bash/edit/write tools, equal budgets, and no retries or compaction.
Their native prompts and tool behavior were retained.
Baseline Ava passed 14/15 and Pi passed 15/15; Ava's total estimated cost was 14.1% higher.

After tool-contract repairs, 15 new Ava attempts reused the historical Pi results to reduce evaluation expense.
Ava passed 14/15.
One TLS interruption left one request's usage unknown; that failure was retained without retry or replacement.
The remaining scheduled attempts continued under a documented amendment to the original stop policy.

| Measure | Earlier Ava | Updated Ava | Historical Pi |
| --- | ---: | ---: | ---: |
| Passed, all 15 attempts | 14/15 | 14/15 | 15/15 |
| Full estimated cost | $1.2704 | Unknown; at least $1.1016 | $1.1135 |
| Cost on 14 matching complete-usage slots | $1.1790 | $1.0577 | $1.0321 |
| Cost difference from Pi on those 14 slots | +14.2% | +2.5% | Reference |
| Model requests on those 14 slots | 113 | 90 | 90 |
| Tool calls on those 14 slots | 152 | 110 | 93 |

The 14-slot analysis removes the interrupted slot from every arm and is a secondary diagnostic, not a replacement for the full cohort.
Updated Ava's cost on this subset fell 10.3% against earlier Ava.
No output-limit command stops or Bash timeouts were recorded in the updated cohort.
Separate native checks confirm the repaired tool behavior; the historical agent comparison cannot attribute an exact share of savings to each change.

### Historical interpretation

The observed cost gap narrowed, but parity and statistical equivalence are not established.
The comparison uses three exposed tasks, a historical Pi baseline, and one incomplete-usage attempt.
Repetitions measure variation on these tasks, not diversity or generalization.
Initial paired run order was also unbalanced.

On the 14 matching slots, updated Ava and Pi made the same number of model requests, while Ava generated 12.1% more output tokens and used 18.3% more tool calls.
Ava's cache share was higher; cache percentage alone does not explain cost.
That study motivated separating transport reliability from output and tool-use efficiency and checking improvements on new tasks with fresh controls.

## Running evaluations

See the [evaluation guide](../eval/README.md) for setup, controls, matched comparisons, and artifact formats.
Detailed protocols, task assets, traces, and diagnostic working records remain local.
Evaluations are manually initiated; CI runs implementation checks rather than model evaluations.
