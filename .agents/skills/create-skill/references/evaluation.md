# Evaluating a skill

A valid `SKILL.md` is necessary, but it does not prove the skill helps. Separate
structural checks, trigger selection, and task quality. Do not report a checklist
review or a scripted provider run as a live-model evaluation.

## Small before/after comparison

1. Save a few realistic requests and their expected results **before** drafting.
   Start with two ordinary requests, one edge case, and one nearby request that
   should not select the skill. Include sample inputs and observable checks, not
   just "the answer is good." For an edit, include the request that exposed the
   original problem. Keep one additional request unseen until the final check.
   Store evaluation inputs and grading criteria outside the skill package given
   to the agent being tested; a linked reference must not reveal the answers.
2. Use no skill as the creation baseline, or the existing version as the revision
   baseline. Compare in separate disposable workspaces and fresh conversations:
   disabling a skill does not remove instructions already read into a session.
   Keep the model, settings, tools, inputs, and budget the same. If another skill
   with the same name is available, ensure it cannot contaminate the comparison.
3. Test selection with only the normal catalog metadata available. Do not force
   the skill with `$name` in these tests. Positive requests should load it;
   nearby negatives should not. Forced invocation can test the workflow, not
   whether its description is a good trigger.
4. Inspect the actual deliverable and relevant tool calls. Run deterministic
   checks where possible. For subjective output, compare unlabeled results with
   the same rubric; do not grade polish instead of correctness.
5. Record the outcome per case. Deliverable correctness comes first. Report
   trigger misses/false activations and any safety failure separately. Record
   tokens, tool calls, cost, and elapsed time when available; use `not measured`
   rather than inventing numbers. Repeat close or inconsistent comparisons.
6. Revise the smallest cause of a failure, rerun affected cases, then run the
   held-out request. Keep the revision only if it improves correctness or makes
   equally correct work measurably simpler. Do not claim a general improvement
   from one successful example.

Use the existing test tools; a short table is usually enough. Do not create an
agent benchmark framework just to evaluate a small skill. Get permission before
runs that need paid services, sensitive inputs, or external side effects. If
execution is unavailable, deliver a clearly labeled draft with the checks still
needed, not a made-up pass rate.

| Case | Expected selection/result | Baseline evidence | Candidate evidence | Outcome |
| --- | --- | --- | --- | --- |
| ordinary request | … | … | … | pass / fail / not run |
| edge case | … | … | … | pass / fail / not run |
| nearby negative | no skill activation | … | … | pass / fail / not run |
