# Marginal

Let a model read a Google Doc and leave **native anchored comments** in it — the
real yellow side comments, attached to specific sentences, not a summary pasted
somewhere else. It comments live, one at a time, and answers the replies in the
thread.

Two reasons to want this: it turns a strong model into a research collaborator
that reviews work in the place the work already lives, and the resulting threads
(model comment → your reply → model reply) are data about what a useful review
comment looks like.

## Install

**As a Claude Code plugin**, if you want the setup done for you:

```
/plugin marketplace add oscar-gilg/marginal
/plugin install marginal
/marginal:setup
```

`/marginal:setup` finds or fetches the CLI, brings up a dedicated Chrome
profile, checks it is signed into Google, and writes a config matching what it
found. Then `/marginal:review <doc-url>` reviews a document and
`/marginal:respond <doc-url>` answers the replies.

**As a CLI**, if you would rather drive it yourself:

```bash
uvx marginal setup <doc-url>     # the same checks, and a config to match
```

Pass a document URL and the check proves the browser session by exporting that
document, which is the operation every run performs. Without one it can only tell
you that docs.google.com did not bounce to a sign-in page, and says so.

There is one step neither route can do for you: **signing in to Google**. Chrome
opens with a dedicated profile, you sign in once, and the profile keeps its
session. Google frequently refuses sign-in inside an automation-launched browser,
which is why nothing here pretends to automate it.

## What you actually need

Almost everything is optional. Two things are not: a Chrome, and a Google session
inside it.

| You have | What runs | What it costs you |
| --- | --- | --- |
| Nothing but Chrome | `source = "browser"`, `mode = "agent"`, `critic = false` | Comments post at the length the agent wrote them, and half the commands are unavailable — see below |
| A model API key | `mode = "api"` — this tool writes the comments, and the shortening pass runs | — |
| Google OAuth too | `source = "api"` — a faster read, a revision check before each post, and every command | one command: `marginal auth --account you@example.com` |

**What the first row cannot do.** `comment`, `review`, `context`, `submit-brief`
and `post-batch` all work with no Google credentials: they read through the browser
export and verify through it too. `read`, `list`, `post`, `reply`, `unpost` and
`respond` mint a token whatever the source, and exit with a message naming what to
run if there is none. `reply`, `unpost` and `respond` write to the comment list,
which has no browser route at all; `read`, `list` and `post` could have one and do
not yet.

So a Chrome-only install reviews a document and leaves anchored comments, but
cannot answer the replies, and cannot remove the ones it left. Adding OAuth is one
command, because this build ships an OAuth client — see below for what that means
and how to use your own instead.

`setup` writes whichever of those fits, with the reason for each setting in the
file next to it. `marginal config` prints what a run here would use and where
each value came from.

The middle row is the one worth understanding. In **agent mode** a coding agent
already reading the document writes the comments and Marginal places them, so
nothing here calls a model and no key is needed — it runs on whatever subscription
the agent has. The exception is the shortening pass, which runs inside
`post-batch` on this tool's side, deliberately: it used to live in a *prompt*, and
any caller that reached `post-batch` without reading that prompt posted unedited
comments. With no key there is nothing to run it with, so `setup` turns it off
rather than failing on every comment.

## Usage

```bash
marginal setup   <doc-url>                # check this machine, write a config
marginal chrome                           # the dedicated profile, on its own
marginal read    <doc-url>
marginal list    <doc-url>
marginal comment <doc-url> -n 5           # leave comments; `mode` says who writes them
marginal respond <doc-url>                # answer replies to our comments
marginal post    <doc-url> -q "exact quote" -b "comment text"
marginal reply   <doc-url> -c <comment-id> -b "reply text"
marginal unpost  <doc-url> -c <comment-id>
```

From a checkout, `uv sync` once and prefix each with `uv run`.

`mode` in the config decides who writes the comments, and `comment` runs whichever
it names: `api` calls a model from here, `agent` prints the brief for the coding
agent that is already reading the document. The explicit names still work —
`review` for API mode, `context` + `post-batch` for agent mode — and they beat the
setting, with a note, because comparing the two modes on one document should not
mean editing the config between the two halves of the comparison.

`mode = "api"` and `source = "api"` are unrelated: the first is who writes the
comments, the second is where the document text is read from, and any combination
of the two works.

Both modes read the same settings, and every command that takes part in writing a
comment accepts all of them as flags:

```bash
marginal review     <doc-url> --max-words 40 --critic-model claude-opus-5
marginal post-batch <doc-url> --max-words 40 --critic-model claude-opus-5 --from -
marginal review     <doc-url> --web-search    # API mode, may look things up
marginal context    <doc-url> --web-search    # agent mode, same instruction
```

## How it works

Read through the API, write through the browser, verify through the API.

| Step | Mechanism | Why |
| --- | --- | --- |
| Read the document | Docs API `documents.get` | Exact text and tab structure; no scraping |
| Decide comments | model call | Returns `{quote, body}` pairs |
| Recheck the revision | Docs API `documents.get` | Reread changed tabs before using their offsets |
| Resolve the quote to a span | `docs_ui.resolve_quote` | Exact character offsets in our own text stream |
| Post the comment | Chrome DevTools Protocol | The only way to create an anchored comment without the Developer Preview |
| Verify the anchor | Drive API `comments.list` | Compare `quotedFileContent` to the span we intended |
| Reply in a thread | Drive API `replies.create` | Replies need no anchor, so no browser |

That last row matters more than it looks: only the *first* comment of a thread
needs the browser. The whole conversation afterwards is plain API, so the slow
fragile part is a small fraction of the system.

With `source = "browser"` the two API reads are done through the signed-in
browser's own session instead — a `txt` export for the text, a `docx` export for
the existing comments and their anchor ranges. Slower than one JSON call, and the
reason a user with no Google Cloud project can still review a document. The last
row has no browser equivalent, so replying still needs OAuth.

## Why not just use the API to comment?

Three comment paths exist, and only one anchors:

- **Drive API `comments.create`** — GA, but the Docs editor *ignores* the anchor
  field. The comment exists and is not attached to any text. Useless here.
- **Docs API `InsertCommentRequest`** — anchors properly via
  `range: {startIndex, endIndex}` and posts every comment in a single
  `batchUpdate`. Gated behind the Workspace Developer Preview Program, which
  **requires a Google Workspace account** and rejects personal Gmail. The fastest
  possible version of this tool, for anyone who can get into it.
- **Driving the Docs UI** — what this repo does. Works now, no gatekeeping, and
  it's the only route that could post as an arbitrary account.

## Two modes, and looking things up

Who writes the comments is the one axis that changes what you need. **API mode**
(`review`) has this tool call a model itself and needs a model API key; **agent
mode** (`context` then `post-batch`) hands the job to a coding agent already
reading the document and needs no key, so it runs on a subscription.

The two must behave identically, which is what made web search a setting rather
than an accident. An agent-mode commenter could always search, because nothing
stopped it; the API-mode commenter could not, because it was handed exactly one
tool. `web_search = true` closes that: both modes are given the same paragraph
about when a search is worth making, and the mechanism differs underneath —
Anthropic's server-side `web_search` tool in API mode, the coding agent's own
search in agent mode.

Off by default. A comment is about the document's own reasoning, and a commenter
that can search will sometimes spend one relaying what the literature says. Turn
it on for a document that leans on external facts. One caveat the run prints for
itself rather than leaving you to infer: `web_search_max_uses` caps searches per
turn on the direct Anthropic route only, since OpenRouter's `web` plugin caps
results per search instead.

## Google OAuth, and what it buys

Optional. Without it, `source = "browser"` reads and verifies through the Chrome
session you already signed in, and no Google Cloud project is involved at all.
With it, the document is read in one JSON call and the revision is rechecked
immediately before each post, so a document edited while the reviewer was thinking
is caught rather than commented on stale.

```bash
marginal auth --account you@example.com
marginal chrome       # sign this Chrome profile into the same account
```

That is the whole of it. marginal ships an OAuth client, so there is no Google
Cloud project to create, no APIs to enable and no consent screen to configure.

**What you are agreeing to.** The consent screen says the app is not verified by
Google and *"may stop working soon"*, names the developer as the address that owns
the client rather than you, and describes the access in Google's blunt terms: *see,
edit, create, and delete all of your Google Drive files*. All of that is accurate.
The narrower `drive.file` scope will not do: it reaches only files the app created
or that the user hands it through a picker, and marginal starts from the URL of a
document that already exists. Your token stays on your machine under
`~/.config/marginal/` with private permissions; nothing is sent anywhere but
Google. The client itself is not private and does not need to be — it ships in the
package, world-readable, which is what RFC 8252 means by a public client.

**Three consequences of a shipped client**, none of them hidden:

- Your API calls count against the shipped project's quota, shared with every
  other user of it.
- Unverified apps are capped at 100 authorized users, after which new ones fail.
- If Google ever requires verification of that client, it stops working for
  everyone at once.

**Use your own project instead** — recommended if you rely on this, and the way
around all three. Create a Google Cloud project, enable the Docs and Drive APIs,
configure the consent screen, create a
[**Desktop app** OAuth client](https://developers.google.com/identity/protocols/oauth2/native-app),
and point marginal at its JSON:

```bash
marginal auth --client ~/Downloads/client_secret.json --account you@example.com
```

That file is copied into `~/.config/marginal/` and takes precedence from then on;
there is no setting to remember, because the file's presence is the preference.
`marginal config` prints which client a run would use, so whose project you are on
is never something to deduce.

Two things worth knowing if you do run your own. **The consent screen must be
either published or list you as a test user.** Being neither is refused with
"has not completed the Google verification process", which reads like a
verification problem and is really an access-list one. Both routes work — that an
unverified *published* app still lets people through was confirmed by trying it
with an account that was neither the owner nor a test user. And **an app left in
Testing expires its refresh tokens after seven days** for scopes like these, so
you would re-run `marginal auth` weekly; publishing avoids that.

marginal owns its OAuth tokens and does not discover or modify another tool's
credential store.

Tokens, and any client you install with `--client`, are stored under
`~/.config/marginal/` with private file permissions. A bundled client is not: it
lives in the installed package and is world-readable, because it is public. The first account becomes the default. With several accounts, select
one per command or set the default explicitly:

```bash
marginal --account work@example.com read <doc-url>
marginal auth list
marginal auth default work@example.com
```

Use `auth --no-browser --account NAME` on a remote machine; it prints the
authorization URL and asks for the complete redirected URL. An old
`credentials = "/path/to/workspace-mcp.json"` setting still works as an explicit,
warning-emitting migration path, but no workspace-MCP directory is searched.

This authentication flow uses the generally available Docs and Drive APIs. It does
not enroll an account in the Workspace Developer Preview, where
[`InsertCommentRequest` remains gated](https://developers.google.com/workspace/docs/api/how-tos/suggestions),
and does not make that request available. Personal Gmail accounts can use
Marginal's browser posting path even though they cannot join that Preview
program.

## Findings that shaped the design

From a probe against the live editor:

- **Docs' find does not move the editing selection.** Typing a quote into Cmd+F
  and pressing the comment shortcut anchors the comment *wherever the caret
  already was*. In the probe it landed tens of lines from the match. This is the
  failure mode to fear: a well-formed comment with
  sensible text attached to the wrong sentence, and no error anywhere. Find is
  not used for selection.
- A real selection plus `Cmd+Opt+M` anchors correctly — verified byte-exact
  against `quotedFileContent`, with a genuine `kix.*` anchor id.
- Escape does not close the find bar, and closing it discards any selection.
- Key events need `windowsVirtualKeyCode`. Without it `event.keyCode` is 0 and
  Docs' shortcut handler ignores the key.

Across a large sample of model-authored comment proposals, the quote a model
picks to anchor a comment is almost always unique within its tab; a fraction of a
percent are ambiguous or not present verbatim, and essentially none span a
paragraph break. So quote widening, occurrence-index navigation and
cross-paragraph selection are all edge-case machinery. This repo fails loudly on
the rare miss instead of guessing.

## Selection strategies

Selection is keyboard navigation from a deterministic origin (select-all, then
collapse left). Two strategies, so accuracy and speed can be compared:

- `paragraph` — `Option+Down` per paragraph, then character steps within it.
  Few events; assumes Option+Down lands on paragraph starts.
- `chars` — one character step at a time. Exact by construction *if* one arrow
  press equals one character of our stream, which bullets, tables and inline
  objects may break.

Neither is trusted. Every post is verified against the API.

API-backed runs also retain the document's `revisionId`. Immediately before each
browser post, Marginal checks it again. If the document changed while the
reviewer was thinking, it rereads the named tab and resolves the quote against the
new coordinate stream. A moved unique quote can still be placed; a deleted or newly
ambiguous quote is rejected. The browser action cannot be made atomic with the API
check, so the final selection confirmation and Drive read-back remain necessary.

## Still to de-risk

Ordered by how much each would change the design.

1. **Where the time goes.** If the model takes 10–20s to decide a comment and
   posting takes 1–3s, the executor is not the bottleneck. Measure both halves
   before optimising either.
2. **Selection accuracy per strategy**, across deliberately varied spans: prose,
   bulleted item, heading, table cell, span containing a hyperlink, hyphens,
   smart quotes. Decides which strategy is the default and whether a
   screenshot-based fallback is needed at all.
3. **Structural traversal drift.** The flatten walks table cells inline; the
   caret may not. If they diverge, every span after the divergence lands wrong.
4. **Burst integrity.** Post ~20 comments as fast as the UI accepts them and
   verify all 20 anchors. Docs' save pipeline is async, so this is where dropped
   or unanchored comments would appear.
5. **Durable signed-in Chrome profile.** Google sometimes refuses sign-in in
   automation-launched browsers. Until this works the tool borrows a live
   browser session.
6. **Headless viability** — decides whether this can run without taking over the
   screen, and whether several documents can go in parallel.
7. **Reply round trip** — confirm `replies.create` shows up in the Docs thread,
   and that the model can read human replies back.
8. **Comment quality.** The real risk isn't mechanical: an agent with the
   document and a repo can produce twenty plausible useless comments as easily
   as three good ones.

## Known issues

- **macOS only.** Posting sends `Cmd+Opt+M` to the Docs editor and Chrome is
  looked for in `/Applications`. Nothing else is platform-specific, but nothing
  else has been tried.
- **Identity has two sides.** The named API account and the account signed into the
  dedicated Chrome profile must match. A separate account for the model keeps its
  comments distinct, but Marginal cannot force Chrome to use the same identity.
- **Notifications.** On a shared document, every comment emails the
  collaborators, and deleting the comment does not recall the mail. Default to
  documents nobody else is watching.
- **`provider = "auto"` falls back quietly, and that is fine.** A direct Anthropic
  call that fails — an unfunded key returns `400 Your credit balance is too low` —
  sends every call through OpenRouter instead. Not a degraded route: caching and
  effort both work there for Anthropic-backed models, and a run with caching on
  reads the overwhelming majority of its input from cache either way.

  Every run prints a `tokens:` line. A zero `cache read` across a multi-comment run
  is the only symptom a dead breakpoint has — it raises no error, it just costs
  full price.
