---
name: marginal:publish
description: >
  Publish a Markdown draft to a Google Doc for the user to comment on, one new tab per
  version, and run the revision loop: read the comments they left, revise the Markdown,
  publish the next version as a new tab in the same Doc. Argument $ARGUMENTS — a Markdown
  file, then either a Doc URL (add a version tab to it) or a title (create the Doc), and
  optionally a version label. Use when the user wants a draft, plan, or report put in a
  Google Doc for comments, wants the next version posted, or asks what comments came back
  on it. Needs Google OAuth from /marginal:setup.
argument-hint: <draft.md> <doc-url | "new doc title"> [v<N>]
user-invocable: true
---

You are running a draft through a Google Doc with the user. The Markdown file in the
repository is the source of truth; the Doc is where the user reads and comments. Each
version is a **new tab** in one Doc, so the user keeps one link and one comment history,
and a tab they commented on is never touched again.

```bash
marginal publish draft.md --title "Post draft"                          # v1: create the Doc
marginal publish draft.md --doc <doc-url> --tab-title "v2 — 07-09"      # v2+: add a tab
marginal list <doc-url> --full                                           # read every thread
```

If `marginal` is not on PATH, prefix each with `uvx ` — or, when the draft has
figures, `uvx --from 'marginal[publish]' marginal ...`, because shrinking them needs
Pillow and the base install deliberately has no image library. The command says so
if it is missing; install rather than working around it.

**This needs Google OAuth.** Creating a Doc, adding a tab, and reading the comment
list all go through the API; there is no browser route. If the command exits asking
for a token, tell the user it needs the OAuth step from `/marginal:setup` and stop.

## 1. Which command

- **No Doc yet** → `--title "<name>"`. The first tab is titled `v1 — <date>`; the
  command prints the Doc URL. Give the user that URL; nothing else is needed.
- **A Doc exists** → `--doc <url>` with `--tab-title "v<N> — <DD-MM>"`. Keep that
  naming: the number is how the user and the comments refer to versions. Read `<N>`
  off the newest existing tab rather than guessing; `marginal read <url>` on a URL
  that names no tab prints the tab list.
- `--note "…"` puts an italic line under the title — what changed since the last
  version, in one sentence, is the useful content.

The command re-reads the finished tab and checks its heading, table and image counts
against the parsed Markdown. On a mismatch it deletes the tab (or trashes a Doc it had
just created), exits 3, and leaves the document exactly as it was. Nothing to clean up:
fix the Markdown and run it again. Do not retry in a loop.

## 2. What the Markdown may contain

The renderer issues Docs requests itself rather than converting a file, so it handles
exactly this subset and nothing else: `#`/`##`/`###` headings, paragraphs, `**bold**`,
`*italic*`, `` `code` ``, `[links](https://…)`, `-` bullets nested by two spaces, `1.`
numbered lists, pipe tables, `>` blockquotes, `---` rules (dropped), and
`![alt](path.png)` figures on their own line. Figures are local files, resolved
against the Markdown file's directory and then the repository root; PNG, JPEG or
GIF; they are shrunk to ≤1100 px, framed with a hairline border, and placed at 6.4 in.

Not supported: fenced code blocks, footnotes, raw HTML, reference-style links,
images inside a sentence. Rewrite those before publishing; an unsupported construct
comes out as literal text, which the user will comment on instead of the content.

## 3. Reading the comments

`marginal list <url> --full` prints every thread: the quoted anchor, the comment, each
reply, and whether it is resolved. Threads on older tabs are included — the comment
list is per document, not per tab — so read the anchor text to tell which version a
comment is about.

Address **every open thread** in the revision. Then, when you post the next version,
tell the user in the chat what happened to each comment: applied, applied differently
(how), or declined (why). One line per thread. That summary is the deliverable; the
user should not have to diff two tabs to find out whether they were heard.

Do not reply inside the threads unless the user asks. A reply emails every
collaborator, and the disposition list in the chat is the same information without
the noise. If they do ask, `marginal reply <url> -c <comment-id> -b "…"` posts one,
and `/marginal:respond` handles a whole round.

## 4. Do not

- **Edit a published tab or delete one.** Docs cannot rewrite a tab in place through
  this tool, and a tab with the user's comments on it is the record. A version is
  always a new tab.
- **Publish without the Markdown being current.** The file and the newest tab must
  say the same thing; if you revised in the Doc's direction, revise the file first.
- **Trash a Doc the user has commented on**, even one you created. The only Doc this
  tool trashes on its own is one it created seconds earlier whose render failed.
