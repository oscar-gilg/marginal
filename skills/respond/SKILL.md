---
name: marginal:respond
description: >
  Answer the replies people have left on comments in a Google Doc, in the thread they were
  left on. Argument $ARGUMENTS — `<doc-url> [--dry-run]`. Use when the user says someone has
  replied to the comments, wants the threads answered, or asks what came back. Replies need no
  browser and no anchor, so this is quick.
argument-hint: <doc-url> [--dry-run]
user-invocable: true
---

You are answering replies on a document this tool has already commented on.

```bash
marginal respond $ARGUMENTS
```

If `marginal` is not on PATH, prefix it with `uvx `.

Only the first comment of a thread needs the browser; replies go over the Drive API,
so this path is fast and has none of the anchoring failure modes the review skill
warns about.

**This is the one part that needs Google OAuth.** Replying writes to the comment
list, and there is no browser route for that, so a Chrome-only install cannot do
it — the command will exit asking for a token. If that happens, do not try to work
around it: tell the user that answering replies needs the OAuth step from
`/marginal:setup`, and stop.

Two things worth knowing:

- **`--dry-run` first if the user has not seen the threads.** Every reply emails the
  document's collaborators, and deleting a reply does not recall the mail. Showing
  them what would be said costs one command.
- **A reply is a conversation, not a re-review.** If someone pushed back and they
  are right, say so plainly and briefly. Do not restate the original comment at
  greater length, and do not open new lines of argument in a thread that was about
  one sentence.

Report which threads got answers, and flag any reply that disagreed with the
original comment — that is the signal the user most wants out of this, and it is
easy to lose in a list of successes.
