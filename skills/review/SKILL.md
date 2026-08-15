---
name: marginal:review
description: >
  Review a Google Doc and leave real anchored comments in it — the yellow side comments attached
  to specific sentences, not a summary pasted elsewhere. Argument $ARGUMENTS — `<doc-url>
  [-n N] [--focus "what to look for"]`. Use when the user wants a document commented on,
  reviewed in place, or marked up. Run /marginal:setup first if this machine has never
  used it.
argument-hint: <doc-url> [-n N] [--focus "..."]
user-invocable: true
---

You are reviewing a Google Doc and leaving comments in it. The comments will appear
under the user's or the bot account's name in a document real people read, so the
bar is what you would say to a colleague about their draft — not coverage.

## 1. Get the brief

```bash
marginal comment $ARGUMENTS
```

If `marginal` is not on PATH, prefix it with `uvx `. If it fails because
nothing is set up on this machine, run `/marginal:setup` and come back.

In agent mode this prints everything you need in one block: how to review, how many
comments to make, what has already been said on the document, the figures written
out as files you can open, and the document itself. **Follow that brief.** It is the
same material the API-mode path sends a model, kept in one place precisely so the
two modes cannot drift, and it beats anything in this skill if they disagree.

If the config is in API mode instead, the same command runs the whole loop itself
and prints what it posted. There is nothing for you to do but report it.

## 2. Read the document, then write

Read it properly before writing anything. The failure mode here is not a wrong
comment, it is twenty plausible useless ones — an agent with a document and a repo
can produce those as easily as three good ones, and the three are what the user
wanted.

A comment earns its place if it changes what the author does next: a claim the
evidence does not support, a number that disagrees with the figure beside it, a
definition used two ways, a missing control. Not typography, not "consider
expanding this", not a summary of the paragraph it sits on.

## 3. Place each one as you finish it

Do not batch them up. As soon as a comment is written, hand it to a subagent along
with its quote, and carry on to the next while that runs. The subagent's instruction
is exactly this:

    Run `marginal submit-brief <doc-id> --tab <tab-id>` and follow it.

The document id and tab id are in the brief from step 1. That command tells the
subagent how to post and how to fix a quote that will not place. **It does not
rewrite the comment**, and neither should you — the editing pass for length runs
inside the posting command.

## 4. Quotes are the part that goes wrong

A quote must appear in the document's own plain text, exactly once. That text has no
Markdown: no asterisks, no heading marks, no table pipes, no footnote bodies. A
quote lifted from any of those will be rejected.

If a comment cannot be placed after two attempts, **drop it and say so.** A comment
anchored to the wrong sentence still verifies as exact and reads as deliberate,
which makes it worse than a comment that was never made.

## 5. Report

Say how many were posted, how many were rejected and why, and quote one or two of
the comments so the user can judge the standard without opening the document. If
you dropped any, say which.
