# Prompts

The prompt is the part of this tool most worth changing, so it lives in files
rather than in code. Point a config at whichever you want:

```toml
# marginal.toml
[prompts]
commenter = "prompts/commenter-v2.md"
respond   = "prompts/respond-v1.md"
critique  = "prompts/critique-v1.md"
```

The keys name jobs, not subcommands. `marginal review` runs `commenter` and
then `critique`; nothing is named `review`.

`critique-v1.md` is templated: `{min_words}`, `{max_words}` and `{ceiling}` are
substituted from config before the prompt is sent, so the length band is a setting
rather than a reason to fork the file. A prompt with no placeholders is sent
verbatim. Substitution is a plain string replace, not `str.format`, so a prompt
containing a literal JSON example does not need its braces escaped.

## This repo's own lineage

| File | What it is |
| --- | --- |
| `commenter-v2.md` | Decides what to say. Adapted from a prompt written for a comment-quality benchmark, versioned separately because the targets differ: that one is scored on recall against a human comment set over a fixed corpus, this one is read by the author of a live document. |
| `respond-v1.md` | Replies within a thread. |
| `critique-v1.md` | The editing pass. A second model tightens each finished comment: it cuts a dead last sentence, an opening that restates the anchored passage, and length over the configured band. It may only shorten — it never judges whether a comment was worth making, because it never sees the document. The length and last-sentence rules moved here out of the commenter prompt, so the commenter writes freely and this trims. Both api and agent mode use this same file. |

`commenter-v2.md` opens with a comment block recording what was cut in adaptation
and why. In short: the pre-comment rejection machinery (whole-document rejection
pass, strongest-reply test, "assume the author already thought of it", and the rule
against caveat-or-footnote outcomes), the benchmark scaffolding, and the human
calibration pairs. The rejection pass in particular made a strong model *less*
likely to raise a useful comment asking an author to justify a load-bearing step,
which is why one short priced-in check survives in the self-check rather than five
paragraphs of filtering.

The same block records the second round of cutting: length rules to
`critique-v1.md`, anchor mechanics to the output contract in `reviewer.py`, and
most sentence-level polish dropped. The rule of thumb is that each rule lives
wherever it is enforced, and appears once.

## Versioning

Bump the number rather than editing in place, so a run can be tied to the prompt
that produced it: `commenter-v3.md`, and so on. Keep the header block current — the
record of what changed and why is the useful part.

## Prompts kept elsewhere

If you keep a benchmark or grading harness alongside this, vendor anything you want
from it rather than reading it across at runtime. A response cache that keys on
prompt bytes needs its prompts byte-frozen, and a shared file read live from two
projects cannot be frozen for one of them.
