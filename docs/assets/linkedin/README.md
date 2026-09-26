# LinkedIn image set

Three 1600 × 1400 PNGs, intended to accompany a personal engineering post about managing agent work across machines. English captions are baked into each image. Suggested upload order:

1. `01-board.png` — **Know what needs your attention.** Nine synthetic sessions across three projects/machines, with three sessions in each review-state column.
2. `02-ssh-machines.png` — **Your machines. One workbench.** The native Machines settings dialog, cropped and enlarged. This Mac, GPU workstation and Development server are demonstration entries.
3. `03-session-replay.png` — **Inspect the evidence, not just the answer.** Read-only inspection of a synthetic failed tool result, with error/truncation observations and links to related evidence.

## Provenance and privacy

- Captured from the actual Qt Quick interface in the current working tree on 2026-09-25, not a hand-drawn UI mockup. This is not a claim that every shown feature is in a published release.
- Used isolated test `AVA_HOME`, `HOME`, project directories, and desktop settings. No personal session history or saved machine configuration was used.
- Board summaries and remote connection states were injected as fixture data. The SSH addresses `gpu.demo.example` and `dev.demo.example` are reserved example names. No SSH connection was attempted.
- Replay uses the synthetic `replay_payloads()` fixture in `tests/test_session_replay.py`. It does not call a model or execute the recorded tools. This screenshot demonstrates historical inspection, not deterministic rerunning of an arbitrary session.
- All three final images explicitly disclose demo/synthetic data. Keep these disclosures when publishing.
- Headers, descriptions, background and demo labels are editorial framing outside the captured interface.

## Checks performed

- Two focused native-UI capture scenarios passed: board/machine fixture rendering and read-only replay inspection (latest run: 2 passed in 5.57s).
- Asserted board totals of `[3, 3, 3]`, three machine entries, the selected tool-error event and zero model requests during replay.
- Reopened all final PNGs and verified their 1600 × 1400 dimensions.
- Ran OCR on all final images to check visible captions, demo disclosures, task titles, machine names and absence of private user paths. This is a text/layout sanity check, not a substitute for final human visual review.
- No application source or existing sessions were modified. Temporary capture/compositing scripts and intermediate images were kept outside the repository in the task scratchpad.

## Suggested alt text

- **Board:** AVA session board showing nine fictional tasks across a local Mac, a GPU workstation and a development server. Columns separate In progress, Needs review and Reviewed.
- **SSH machines:** AVA Machines settings showing a local Mac and two simulated SSH connections named GPU workstation and Development server. Projects and chats stay on their execution machine.
- **Session Replay:** AVA's read-only session inspector showing a synthetic login investigation. The selected tool result reports File unavailable; the inspector flags the error and truncated tool output.

Before posting, review the images at phone size and confirm that the release linked in the post contains the shown functionality; otherwise label it as a development preview.
