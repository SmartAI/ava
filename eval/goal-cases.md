# Goal-mode evaluation cases

Status: implementation evaluation contract. `eval/goal_tasks.py` now materializes
G01–G04 in the existing Harbor format; `tests/test_goal_tasks.py` qualifies reference,
unchanged, and intentionally wrong controls. `tests/test_goal.py` exercises goal
lifecycle behavior. This document itself is not a Harbor manifest or a result.

## Value and comparison

Question: does goal mode improve independently verified completion of the full
requested objective without false completion, uncontrolled continuation, or loss
of user control?

Compare the same tasks in fresh isolated workspaces:

- A: current ordinary task execution, no goal mode and no user follow-up prompts.
- B: goal mode with durable state and runtime continuation.
- C, only if proposed: B plus a separate completion evaluator. Keep this extra
  model only if it improves correctness enough to justify its measured overhead.

Use identical objective text, model ID, effort, permissions, available tools,
context limits, task snapshots, and total token/time budgets. Pin wheels, prompts,
fixture/grader hashes, and evaluator configuration. Count evaluator and subagent
usage against the same total budget. A normal turn may contain many tool calls;
do not artificially restrict the baseline to one model response.

Use seeded interleaved trial ordering and three repetitions per task/arm for an
initial screen. Report paired outcomes and raw counts; this small screen is not
statistical proof. Never omit failed attempts or retry them invisibly.

## Golden deliverable cases

Implement tiny Python standard-library projects so task checks need no network.
Each case needs an immutable starter snapshot, reference solution, private grader,
and explicit user instruction. Tests below are grader requirements, not evidence
inferred from an agent's final answer. Keep private checks outside agent-writable
files. The agent must still receive all behavioral requirements in its objective.

### G01 — Complete the whole migration (development)

**Objective:** migrate a local identifier lookup API from `lookup(id)` to
`lookup(id, *, namespace)`. Update library callers, the CLI, and the usage example;
keep the CLI's output unchanged and make the existing checks pass. Remove the old
one-argument API rather than retaining a compatibility wrapper.

**Starter:** library and CLI have separate callers; the visible test covers only
the library. The README contains a runnable example of the old API.

**Oracle:** invoke library and CLI with fixtures from two namespaces; verify no
cross-namespace lookup; execute the README example; verify the old call fails;
verify baseline tests were not removed or weakened. All requirements must pass.

**Failure targeted:** stopping after the easy subset passes or redefining success.

### G02 — Recover from misleading green tests (development)

**Objective:** fix a CSV total command for empty input, quoted fields containing
commas, and negative decimal values. Preserve its public command-line interface.
Run checks and report evidence for each required behavior.

**Starter:** current visible tests pass but cover only positive integer input;
the implementation splits on commas and sums using integer conversion.

**Oracle:** run the real CLI against independent input files for every stated
requirement; compare exact exit codes and decimal output. Check that the interface
and original tests remain intact. A plausible summary or green visible tests alone
cannot pass.

**Failure targeted:** false completion based on weak or narrow evidence.

### G03 — Already satisfied objective (development)

**Objective:** ensure a supplied JSON validation CLI accepts the documented valid
input and rejects the documented invalid input; change nothing if it already does.

**Starter:** implementation already meets both requirements.

**Oracle:** execute both commands, verify expected exit codes, and compare the
workspace with its original snapshot. Require no modifications and a successful
terminal goal state, not repeated continuation after success.

**Failure targeted:** unnecessary edits and loops when no work remains.

**Control exception:** unchanged-workspace grading MUST pass here. For G01, G02,
and G04 it MUST fail. Reference solutions must pass every case.

### G04 — Multi-artifact delivery (validation; held out from prompt tuning)

**Objective:** add a JSON-lines filter command that selects records by a named
field and value, preserves input order, reports malformed input with a nonzero exit,
and streams rather than reading all input into memory. Deliver code, CLI help,
and a runnable documentation example without changing existing commands.

**Starter:** a small CLI with a different existing subcommand and no filter.

**Oracle:** invoke the installed entry point on matching, nonmatching, malformed,
and large streamed input; check order and exit status; use a bounded-memory
subprocess for the streaming requirement; execute the documentation example;
check help and old-command compatibility. Calibrate the memory bound with the
reference and a deliberately buffering implementation before freezing the fixture.

**Failure targeted:** missing named deliverables and narrow checks used to justify
broad completion. Do not inspect validation outcomes to revise prompts; if used
for tuning, reclassify this case and add a fresh holdout before promotion.

## Scripted lifecycle cases

These require a deterministic model/tool double and a controllable clock, not
paid model trials. Exercise the real public command/session boundary and runtime
when available; do not grade a standalone replacement state machine. Record
persisted events, model starts, tool execution starts, and externally visible
status. Never report scripted correctness as evidence of live model capability.

The policy constants below are proposed evaluation requirements, not claims about
existing Ava behavior: no-progress threshold N=3, transient retry cap R=2, and
budget wrap-up allowance W=one model response with no substantive tool execution.
Freeze or explicitly revise these requirements before implementation trials.

| ID | Setup and stimulus | Required observable outcome |
| --- | --- | --- |
| L01 start / continue / finish | Submit a goal; scripted first turn ends with work remaining; second performs the required edit and verified check; completion is accepted. | Goal persists before automatic work; a second turn starts without a user message; status reaches complete; no third turn starts. Bare status query starts no work. |
| L02 false completion | Worker claims success, but an authoritative check fails; also test a plausible worker summary with missing evidence. | Completion gate rejects both claims; goal stays unfinished; continuation receives the failed or missing requirement. Correct the artifact and check again: completion is then accepted. Inject a false-positive evaluator verdict too: a mandatory deterministic check still wins. |
| L03 repeated no progress | Return three turns of status restatements with no artifact changes, new evidence, or live work; vary wording. | Stop automatic turns at N and show blocked/stalled with a reason; do not mark complete. A user resume starts a fresh audit. In a variant, new authoritative evidence changes the next action and resets the consecutive no-progress count. |
| L04 live background work | Launch a controlled job; finish the foreground turn; advance the fake clock; deliver a delayed job result. Include an observation timeout while the job remains live. | No duplicate job launch or false completion; bounded check-ins inspect the same live handle; a polling timeout is not terminal; completion notification wakes work once, not twice. A missing/terminal handle triggers recovery or an explicit blocker, not indefinite waiting. |
| L05 pause / clear race | Pause, then clear, at a barrier between an idle callback reading the goal and scheduling a turn. Separately request pause during an active turn. | No automatic turn is admitted after acknowledged pause/clear; clear cannot resurrect stale work; status query agrees with persisted state. Active-turn policy must be explicit: already-running tools may finish, but no new goal work starts after pause acknowledgement. Resume works only for a still-existing goal. |
| L06 crash / resume / compact | Persist an active goal and progress; crash after persistence; reopen the same session; force context compaction. Repeat with paused, complete, and cleared goals. | Objective and constraints survive exactly; accepted usage is not reset or double-counted; active work resumes only under the documented resume policy; paused/complete/cleared states never auto-restart. Recovered goal is reinjected despite compacted conversation. |
| L07 limits / errors / permissions | Cross a tiny aggregate token budget using worker, evaluator, and child usage; separately inject transient failures, authentication failure, and a permission-required tool. | Budget stop is not completion; at most W wrap-up, no substantive tool starts after the limit is observed. Transient retries stop after R with no busy loop. Authentication failure stops with an actionable reason. Goal mode never grants approval; denied commands are not executed. Missing usage is surfaced, not treated as zero. |
| L08 replace during work | Replace objective A with B while A has an outstanding model/tool result; then deliver A's late completion/status update. | B remains authoritative; stale A events cannot complete, overwrite, or charge usage to B incorrectly. No duplicate active goal; next admitted goal turn receives B. Preserve pre-existing user edits. |

For L02, use deterministic checks only where the task has them. A separate LLM
verdict is not a universal ground-truth oracle. Tasks without machine-checkable
acceptance still need independent artifact review in live evaluations.

## Metrics and promotion gates

Record per trial:

- Verified completion: every required artifact/behavior passes the independent
  grader, regardless of whether the agent claimed completion.
- False completion: agent/goal reports complete while the independent grader fails.
- Unnecessary continuation: model starts after accepted success or acknowledged
  pause/clear; record already-in-flight work separately.
- Stall: N consecutive no-progress turns; report whether the runtime stopped safely.
- Total input/cache/output tokens, evaluator and child tokens, tool calls, model
  attempts, elapsed execution time, and cost when prices are available. Unknown
  usage/cost is null, not zero. Include failures in all-attempt totals.
- Stop reason, resume outcome, final status, requirement-level grading evidence,
  and complete event trace. Do not substitute completion prose for evidence.

Before any model run: qualify reference/no-op controls, then verify graders reject
partial migrations, test deletion, a buffering filter, and false success messages.
Before shipping: all scripted lifecycle cases must pass, with zero forbidden
post-stop work and zero stale-state resurrection.

For the initial live screen, require zero observed false completions and no
verified-completion regression on any paired task. Goal mode must produce at least
one additional verified completion on a previously failed task, not merely extra
turns; otherwise no value has yet been demonstrated. Report cost/latency deltas
alongside the improvement. If all baseline tasks pass, the suite has a ceiling:
add a qualified harder task before making a usefulness claim. An inconclusive
small sample calls for more trials, not a victory claim.

## Execution readiness

- [x] Define baseline, cases, independent oracles, metrics, and initial gates.
- [x] Materialize G01–G04 as task directories using the existing benchmark format.
- [x] Qualify reference, unchanged-workspace, and intentionally wrong controls.
- [x] Freeze an ordinary baseline wheel before goal-mode source changes.
- [x] Run an initial live baseline/candidate screen; see the [result report](../docs/goal-evaluation.md).
- [ ] Complete the planned multi-repetition promotion comparison. The initial screen
  tied at 4/4 and did not demonstrate additional verified completions.
- [x] Exercise real Agent and HTTP boundaries for continuation, checks, stalls,
  pause/clear/replacement, resume, compaction, accounting, errors, and duplicate drivers.
- [ ] Exhaustive process-kill fault injection and full lifecycle matrix.

### Explicit first-implementation adjustments

- G04 uses an early-output streaming oracle (write a line while keeping stdin open)
  instead of a calibrated memory ceiling. The published objective explicitly requires
  that behavior; a buffering reference mutation is rejected. This tests genuine
  streaming without a host-dependent memory threshold.
- L04 is tested with a controlled awaited tool. Ava has no detached-agent/job registry;
  this implementation does not add one. Idle check-ins, verified background polling,
  and child-agent accounting cannot be claimed tested or supported.
- L06 covers durable reopen and a compaction projection, not every crash point in
  the persistence path. Existing session recovery tests remain applicable.
- L07 reuses existing transport retries (two pre-delivery network retries) instead
  of adding an outer retry loop. No wrap-up model response is added at a budget stop.
  Tool restrictions are preserved by using the same tool seam; Ava does not acquire
  a new permission-management system as part of goal mode.
- Initial live screen: one repetition per arm, shared 180-second timeout, no matched
  aggregate-token ceiling (the frozen ordinary baseline has no such option). Report
  this as a screen, not the three-repetition promotion experiment defined above.

Use the existing [evaluation tools](README.md); do not introduce a second runner
just for goals. Any unsupported lifecycle tracing or total-budget accounting must
be reported as a gap, not silently approximated into a pass.
