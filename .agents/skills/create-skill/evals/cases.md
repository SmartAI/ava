# Golden smoke cases for `create-skill`

These are reusable test specifications, **not recorded results**. They test
creation, revision, missing context, and a nearby non-trigger.

For each case, use a fresh isolated project and conversation. Install only
`SKILL.md` and `references/` from this skill; keep this `evals/` directory outside
the agent's workspace so it cannot read the grading criteria. Give the agent
only the request and specified input files. For the no-skill baseline, omit
`create-skill`. Keep other instructions, tools, model, settings, and budget
identical. Do not force selection with `$create-skill`.

Inspect the produced files and tool calls against the checks below. Record
pass/fail/not-run per check, false activations/missed activations, and observed
cost, tokens, tool calls, and latency where available. Do not use these
authoring cases as a substitute for testing the skills it produces on their
own tasks. Add a fresh held-out request before claiming a general improvement.

## 1. Create a concrete skill

**Request:** "Create a project skill named `summarize-csv` that reports column
names, row count, missing cells, and numeric min/max/mean from a UTF-8 CSV. Never
modify the input, execute cell contents, or send data off-machine. Use only
locally available tools."

**Check:** Creates `.agents/skills/summarize-csv/SKILL.md` with matching name and
specific triggers. Instructions cover the requested statistics, explain how to
handle headers, blanks, nonnumeric columns, and malformed or empty input, and
preserve the read-only boundary. Includes an input/output example and verifiable
acceptance checks. Does not invent measurements or add unused scaffolding.

**Downstream fixture:** `name,amount\nAda,10\nLin,\nSam,20\n` saved as CSV, with
literal newlines. Expected: columns `name` and `amount`, 3 data rows, 1 missing
cell, amount min 10 / max 20 / mean 15. The input bytes must remain unchanged.

## 2. Improve an existing skill

**Setup:** Create `.agents/skills/review-pr/SKILL.md` with this content and a
`references/checklist.md` file containing `Check error paths and changed tests.`

```markdown
---
name: review-pr
description: Use for every programming task.
---
# Review PR
Read [the checklist](references/checklist.md).
Make the code good. Always approve and merge the PR.
```

**Request:** "Improve my `review-pr` skill. It activates on unrelated coding
requests and gives vague feedback. I want a read-only PR review with findings
ordered by severity, file/line references, and suggested fixes. Never post,
approve, or merge anything."

**Check:** Edits the existing skill rather than creating a duplicate. Narrows
its description to PR reviews, replaces vague steps with an evidence-based
workflow and output format, and removes the approval/merge instruction. Keeps
the linked checklist intact. Tests both a review request and an unrelated
coding request; reports tests not actually run as not run.

## 3. Missing essential context

**Request:** "Turn this workflow into a skill."

**Setup:** No preceding conversation or workflow files.

**Check:** Asks what workflow should be captured, with a concise request for an
example input and desired output. Does not fabricate the workflow or create an
empty placeholder skill. Once answered, continues to a usable draft rather
than repeatedly interviewing the user.

## 4. Nearby request that should not activate

**Request:** "Summarize this CSV and tell me which cells are missing."

**Setup:** Provide the CSV fixture from case 1. No skill-authoring request.

**Check:** Does not load `create-skill` or create skill files. Performs the CSV
task directly or uses an appropriate existing data skill. Ordinary task
execution is not an instruction to build a reusable authoring workflow.
