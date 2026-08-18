---
name: marginal:review
description: >
  Review a Google Doc in place. By default: a reasonable number of anchored comments in
  the margin. On request: suggested edits (tracked changes the author accepts or rejects),
  instead or alongside. Argument $ARGUMENTS — a document URL followed by a plain-prose
  prompt that specifies everything: the format ("comments", "suggested edits", "both")
  and the kind of feedback wanted ("be sceptical about the evaluation", "fix the clunky
  wording", "three at most, methodology only").
  Use when the user wants a document reviewed, commented on, marked up, or edited via
  suggestions. Run /marginal:setup first if this machine has never used it.
argument-hint: <doc-url> [what feedback, in what form]
user-invocable: true
---

You are reviewing a Google Doc. Whatever you leave appears under the user's or the
bot account's name in a document real people read, so the bar is what you would say
to a colleague about their draft — not coverage.

## 1. The prompt decides everything

`$ARGUMENTS` is a document URL followed by a prose prompt. There are no flags for
the user to know; the prompt specifies both the **format** of the feedback and the
**kind**. Read it as English and translate:

- **Format** — margin comments unless the user asks for suggested edits.
  - The default, and what any prompt that says nothing about format means, is
    **comments only**: a reasonable number of good ones, not coverage. Do not
    add `--suggestions` unprompted, however improvable the wording looks — a
    suggestion edits their document, and nobody asked.
  - Talk of suggesting, rewording, fixing wording, editing in place, tracked
    changes → add `--suggestions`. If they want *only* suggestions, or *only*
    comments alongside it, say so in `--focus` ("suggested edits only") — the
    brief passes that on.
- **Kind of feedback** → `--focus "..."`, in their own words. "Be sceptical about
  the eval" stays "be sceptical about the evaluation"; a tone, a deadline, a
  request to be harsh all belong here rather than being dropped. Do not summarise
  a long instruction into one word, and do not invent a focus when they gave
  none — an unfocused review is the default and a fabricated focus quietly
  narrows it.
- **How many** → `-n N`. "Three at most", "just a couple", "up to 5" all set it.
  A budget is a cap, not a target: safe to pass when they hint at one, safe to
  omit when they do not. With no count given, the config's default cap applies —
  a handful — and stopping below it at the last comment worth making is the
  intended outcome, not a shortfall.

If they typed real flags anyway (`-n 5`, `--focus "x"`, `--suggestions`), pass
them through untouched.

## 2. Get the brief

```bash
marginal comment <doc-url> [-n N] [--focus "..."] [--suggestions]
```

If `marginal` is not on PATH, prefix it with `uvx `. If it fails because
nothing is set up on this machine, run `/marginal:setup` and come back.

In agent mode this prints everything you need in one block: how to review, how many
to make, what has already been said on the document, the figures written out as
files you can open, and the document itself. **Follow that brief.** It is the same
material the API-mode path sends a model, kept in one place precisely so the two
modes cannot drift, and it beats anything in this skill if they disagree.

If the config is in API mode instead, the same command runs the whole loop itself
and prints what it posted. There is nothing for you to do but report it.

## 3. Read the document, then write

Read it properly before writing anything. The failure mode here is not a wrong
comment, it is twenty plausible useless ones — an agent with a document and a repo
can produce those as easily as three good ones, and the three are what the user
wanted.

A comment earns its place if it changes what the author does next: a claim the
evidence does not support, a number that disagrees with the figure beside it, a
definition used two ways, a missing control. Not typography, not "consider
expanding this", not a summary of the paragraph it sits on.

A suggested edit earns its place where the fix says it better than a comment about
the fix would — and it must read as if the author wrote it. The brief carries the
full rules (exact quoting, plain text, the author's own voice); follow them there.

## 4. Place each comment as you finish it

Do not batch comments up. As soon as one is written, hand it to a subagent along
with its quote, and carry on to the next while that runs. The subagent's
instruction is exactly this:

    Run `marginal submit-brief <doc-id> --tab <tab-id>` and follow it.

The document id and tab id are in the brief from step 2. That command tells the
subagent how to post and how to fix a quote that will not place. **It does not
rewrite the comment**, and neither should you — the editing pass for length runs
inside the posting command.

## 5. Quotes are the part that goes wrong

A quote must appear in the document's own plain text, exactly once. That text has no
Markdown: no asterisks, no heading marks, no table pipes, no footnote bodies. A
quote lifted from any of those will be rejected.

If a comment cannot be placed after two attempts, **drop it and say so.** A comment
anchored to the wrong sentence still verifies as exact and reads as deliberate,
which makes it worse than a comment that was never made.

## 6. Suggested edits, if the brief offers them

When suggestions are enabled the brief says so and explains the shape: a
`{"quote": ..., "replacement": ...}` item instead of a comment. Two rules differ
from comments, and the brief states both — the quote must match the document
exactly (it is never widened or corrected), and **all suggested edits go in one
`post-batch` call at the end, after every comment is placed**. Within one call
they are typed bottom of the document first, which is what keeps each one's
anchor exact; splitting them across calls forfeits that. If the brief says
nothing about suggestions, do not attempt one.

## 7. Report

Say how many comments and suggested edits were posted, how many were rejected and
why, and quote one or two of them so the user can judge the standard without
opening the document. If you dropped any, say which.
