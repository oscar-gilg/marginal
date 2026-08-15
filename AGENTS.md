# Working agreement for agents

Coding agents share this repository with its author. The invariants below are the
ones worth knowing before changing anything: they are each written because
breaking them produced a failure that reported success.

## Project invariants

- **The character stream is a coordinate space.** `tab["text"]` is what anchors
  resolve against and what the editor's caret traverses. Nothing may insert,
  drop, or normalize a character in it — structure is described alongside the
  document, never marked up inside it. A change that shifts offsets by one
  produces comments anchored to the wrong sentence that still verify as exact,
  which is the worst failure this tool has.
- Prefer a rejected comment to a wrongly anchored one. Ambiguous resolves as
  absent at every rung; do not add a tie-break that guesses.
- Prompts live in `prompts/` and are selected by config. Bump the version rather
  than editing in place, so a run can be tied to the prompt that produced it, and
  keep the header block's record of what changed and why. Each rule lives in one
  place: what to say in the commenter prompt, how few words in the critique
  prompt, and the output contract in `reviewer.py` where the code enforces it.
- API mode (this tool calls the model) and agent mode (a coding agent does) must
  stay behaviourally identical. Note "API mode" names who writes the comments and
  is unrelated to `source = "api"`, which names where the text is read from. Both
  go through `reviewer.vet` and the same critique prompt; if they drift, a comment
  depends on which produced it and cross-mode comparison stops meaning anything.
  A capability granted to one mode belongs in `brief.sections` so both are told
  the same thing, even where the mechanism behind it differs.
- Do not commit credentials, document ids, or exported document content.
  `.chrome-profile/`, `runs/` and `.env` are ignored for that reason.
- If you keep a benchmark or grading harness alongside this, vendor prompts from
  it rather than reading them across at runtime: a response cache that keys on
  prompt bytes needs its prompts frozen, and a file read live by two projects
  cannot be frozen for one of them.

## Verification

- `PYTHONPATH=$PWD/src python3 -m pytest -q` from the tree you are working in.
  About three seconds, and offline: e2e tests are deselected by `pytest.ini`
  unless `MARGINAL_E2E=1` and a Chrome is answering on the debug port.
- **Pin `PYTHONPATH` to the tree you mean to test.** A shell that exports
  `PYTHONPATH` or `VIRTUAL_ENV` from wherever the session started will import that
  copy instead of this one, and the suite passes — against the wrong code. Confirm
  with `python3 -c "import marginal; print(marginal.__file__)"` if a result
  surprises you.
- Add a regression test for any defect that reported success — a wrong anchor
  that verified, a fallback that never fired, a run that exited 0 having dropped
  its work. `tests/test_audit_fixes.py` and `tests/test_review_fixes.py` are
  where those live.
- Model-backed paths are tested with monkeypatched transports, not real calls.
  Patch at the seam the fix lives behind: replacing `_anthropic` wholesale proves
  nothing about a conversion that happens inside `_post`.
- Report exactly what ran and what could not run.

Prioritize correctness and regressions over style, especially anchor offsets,
ambiguity handling, and anything that decides a comment landed successfully.
