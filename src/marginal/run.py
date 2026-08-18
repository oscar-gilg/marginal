"""The end-to-end loops: leave comments, answer replies, post an agent's batch.

Two ways to run, one pipeline. They differ only in who produces the
`{quote, comment}` pairs:

* **API** — this tool calls a model itself, over the Messages API (`review`).
  Needs a model API key.
* **agent** — a coding agent already reading the document produces the pairs and
  hands them over (`context` then `post_batch`). Needs no model API key, so it
  works on a Claude subscription.

The word "api" now names two different things and the distinction is worth holding
on to: **API mode** says *who writes the comments*, while `cfg.source = "api"` says
*where the document text is read from* (the alternative there being `browser`, the
signed-in Chrome). They vary independently — API mode reading through the browser
source is a supported combination, and so is agent mode against the Docs API.
`cfg.schedule` is a third, separate axis: how the stages are overlapped.

The commenter's job is identical in both. It reads the document, writes a comment,
and hands it off — through a `submit_comment` tool call in API mode, to a
subagent in agent mode — and is told only whether the quote could be placed. It
never counts characters, checks uniqueness, or drives a CLI. There is one
`COMMENTER_CONTRACT` and one commenter prompt, because two of each had drifted.

Everything downstream is `submission.Submitter`, which both modes construct through
`_submitter` and neither reimplements: the anchor ladder and its model fallback,
deduplication, the budget, the attribution header, the editing pass, posting and the
ledger. The editing pass was the last stage to join that list: it used to run in a
subagent in agent mode, described by a prompt rather than performed by code, and a
caller who reached `post-batch` without that prompt posted unedited comments with no
error and no note. It is now `Submitter.critique` on every path — an LLM still does
the editing, but whether the editing happens is not left to a description of a rule.

What genuinely differs is who holds the loop and what pays for it. API mode's
loop is a Python generator over a tool-use conversation, billed as API tokens;
agent mode's is the coding agent's own, billed as subscription capacity. Rejections
therefore travel differently — a tool result in one, a CLI rejection read by the
submitter subagent in the other — but they carry the same sentence, produced by
`anchors.explain`.
"""

from __future__ import annotations

import fcntl
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from queue import Queue

from pathlib import Path

from . import (
    attribution,
    brief,
    browser_source,
    critic,
    gdocs,
    ledger,
    md,
    model,
    reviewer,
    submission,
)
from . import post as post_mod
from .config import Config
from .docs_ui import open_doc
from .post import Result, Run, post_many


def _pick_tab(doc: dict, tab_id: str | None) -> dict:
    """Resolve a tab id, refusing to guess.

    Falling back to the first tab on an unknown id would review and comment on a
    different tab than the caller named.
    """
    tabs = doc["tabs"]
    if tab_id is None:
        # Refuse to guess on a multi-tab document. The old default was `tabs[0]`,
        # and on a many-tab document it reviewed and commented on a tab nobody
        # had named — silently, since every anchor in the wrong tab still verifies.
        if len(tabs) > 1:
            listing = "\n".join(f"  {t['id']}  {t.get('title') or ''}".rstrip() for t in tabs)
            raise LookupError(
                f"this document has {len(tabs)} tabs and none was named; pass --tab "
                f"or a URL with ?tab=…\n{listing}"
            )
        return tabs[0]
    for t in tabs:
        if t["id"] == tab_id:
            return t
    # The browser source reports no tab ids, so it can neither confirm nor deny the
    # request; accept it there rather than refusing every --tab. Where ids exist, a
    # miss is a miss — silently reviewing a different tab is worse than an error.
    if len(tabs) == 1 and tabs[0]["id"] is None:
        return tabs[0]
    raise LookupError(f"no tab {tab_id!r}; document has {[t['id'] for t in tabs]}")


def read_document(doc_id: str, token: str | None, cfg: Config, tab_id: str | None) -> dict:
    """Read the document from whichever source is configured.

    `api` needs Google OAuth. `browser` needs no credentials at all: it captures the
    document's own docx export through the signed-in Chrome. Both return the same
    shape, so nothing downstream knows which ran.
    """
    if cfg.source == "browser":
        page = open_doc(doc_id, tab_id, port=cfg.port)
        try:
            tab = browser_source.read_tab(page, doc_id, tab_id)
        finally:
            page.close()
        return {"title": None, "tabs": [tab]}
    if token is None:
        raise ValueError("source='api' needs a Google token")
    doc = gdocs.read_doc(doc_id, token)
    _attach_markdown(doc, doc_id, token)
    return doc


def _attach_markdown(doc: dict, doc_id: str, token: str) -> None:
    """Give each tab the Markdown the commenter will read, where it can be trusted.

    Drive exports the whole document, so each tab's share is sliced out and checked
    against that tab's own text. A tab whose slice cannot be confirmed keeps the
    plain text and the structural summary — worse to read, but never wrong about
    which tab it is.
    """
    tabs = doc.get("tabs") or []
    if not tabs:
        return
    try:
        export = gdocs.export_markdown(doc_id, token)
    except Exception:
        # Reading the document must not fail because a convenience export did.
        export = ""
    for i, tab in enumerate(tabs):
        tab["markdown"] = md.for_tab(export, tabs, i)


def _prior_threads(doc_id: str, token: str | None) -> str:
    """The unresolved comments already on the document, as a block to read.

    Both modes get this. Agent mode always did; API mode did not, so the same
    document reviewed twice by the same prompt would repeat itself in one mode and
    not the other. Fetching it here rather than leaving it to the caller is what
    keeps that from drifting again.

    A failure is silent on purpose: not knowing what is already there costs a
    duplicate comment, and refusing to review because the comment list would not
    load costs the whole run.
    """
    if token is None:
        return ""
    try:
        threads = [c for c in gdocs.list_comments(doc_id, token) if not c.get("resolved")]
    except Exception:
        return ""
    if not threads:
        return ""
    lines = [
        "# Comments already on this document",
        "",
        "Do not repeat a point one of these already makes.",
    ]
    for c in threads:
        on = (c.get("quotedFileContent") or {}).get("value", "")
        lines.append(f"\n  on {on[:70]!r}\n    {attribution.strip(c.get('content', ''))[:300]}")
    return "\n".join(lines)


FIGURE_CACHE = Path.home() / ".cache/marginal/figures"
LOCK_DIR = Path.home() / ".cache/marginal/locks"


@contextmanager
def browser_lock(doc_id: str):
    """Hold the document's editor to one poster at a time, across processes.

    API mode has always serialised this: one poster thread pulls from a queue,
    because a Docs tab has one selection state and two callers stepping a caret
    through the same document interleave into a comment on the wrong text.

    Agent mode had no equivalent and needed one more. Its brief tells the commenter
    to hand each comment to a subagent and carry straight on, which is the whole
    point of the design — so several `post-batch` processes can be in the editor at
    once, each having opened its own tab. Asking the commenter to serialise them
    would put back exactly the clerical burden the handoff exists to remove, so the
    lock goes here instead, where neither commenter has to know about it.

    A separate process, not a thread, so this is a file lock. It is per document:
    two runs on different documents have no reason to wait for each other.
    """
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    path = LOCK_DIR / f"{doc_id}.lock"
    with open(path, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _reader(doc_id: str, token: str | None, cfg: Config):
    return (
        post_mod.browser_reader(cfg.port, doc_id)
        if cfg.source == "browser"
        else post_mod.api_reader(doc_id, token)
    )


def _post(
    doc_id: str,
    token: str | None,
    cfg: Config,
    tab: dict,
    pairs: list[tuple[str, str]],
    sub,
) -> Run:
    """Edit an already-decided set of pairs, post them, then verify.

    Agent mode's tail: the pairs arrive from `post-batch` already written. It runs
    the editing pass itself rather than trusting the caller to have run one. Editing
    used to live upstream — in `_edit` on the pipeline path, and in a *prompt* on the
    agent path — so a caller that went through neither posted whatever it was
    handed, silently and with no symptom but a long comment. A stage that can be
    skipped by calling the wrong
    entry point is not a stage; the one place every non-pipeline post passes through
    is here.

    `sub.critique` tightens and stamps, and is a no-op tighten when `critic` is off,
    so the pipeline path (which critiques in `_edit`) never reaches this.
    """
    if not sub.cfg.critic:
        sub.note("critic off: comments posted as written")

    # No wrapper around `sub.critique`: it keeps the comment on an editing failure
    # itself now, so every schedule gets that answer rather than each caller
    # choosing one. See `Submitter.critique`.
    pool = ThreadPoolExecutor(max_workers=sub.cfg.critic_workers)
    try:
        edits = [pool.submit(sub.critique, q, b) for q, b in pairs]
        headed = [(q, e.result()) for (q, _), e in zip(pairs, edits)]
    finally:
        pool.shutdown(wait=True)
    with browser_lock(doc_id):
        page = open_doc(doc_id, tab["id"], port=cfg.port)
        try:
            run = post_many(
                page, doc_id, tab, headed, token, cfg.strategy,
                read_comments=_reader(doc_id, token, cfg),
                # The submitter already built this, under the same rule, for the same
                # document and tab. Building a second one here gave the run two
                # revision baselines and wrote the "when is there a revision guard"
                # rule in two places ten lines apart.
                fresh_tab=sub.fresh_tab,
            ) if pairs else Run(doc_id=doc_id, tab_id=tab["id"], strategy=cfg.strategy)
            # After every comment, same rule as the pipeline: suggestions type last.
            if sub.suggestions:
                s_run = sub.place_suggestions(page, tab, doc_id)
                run.results.extend(s_run.results)
                run.notes.extend(s_run.notes)
        finally:
            page.close()
    for r in run.posted:
        sub.record(doc_id, r)
    return run


def _edit(quote: str, draft: str, sub) -> tuple[str, str]:
    """The editing pass: a model shortens the comment, then it is stamped.

    The unit of work the schedules move around. `sync` calls it inline; `async`
    calls it in a pool worker — nothing in here knows or cares which. Agent mode's
    subagent does the same job with the same prompt, one process further out.
    """
    return quote, sub.critique(quote, draft)


def _finish(doc_id: str, cfg: Config, tab: dict, results: list, before: set, reader, sub) -> Run:
    """Verify every anchor in one pass, then record what actually landed."""
    run = Run(doc_id=doc_id, tab_id=tab["id"], strategy=cfg.strategy)
    run.results = results
    post_mod.verify(run, before, reader)
    for r in run.posted:
        sub.record(doc_id, r)
    return run


def _sync(tab, stream, page, results, pairs, sub) -> None:
    """One comment start to finish, then the next. No threads.

    Slower than `async` by roughly the critique time per comment, and worth it when
    you want the run to be reproducible and easy to follow: every stage happens in
    one place, in order, and a traceback points at the comment that caused it.
    """
    for quote, draft in stream:
        quote, body = _edit(quote, draft, sub)
        pairs.append((quote, body))
        if page is not None:
            results.append(sub.place(page, tab, quote, body))


def _async(tab, stream, page, results, pairs, sub) -> None:
    """Critique and post while the next comment is still being written.

        gen 1 ─┬─ gen 2 ─┬─ gen 3 ─┬─ done      (this thread, serial by design)
               │         │         │
             crit 1    crit 2    crit 3         (worker pool)
               ↓         ↓        ↓
             [──── browser queue, serial ────]  (one thread, owns the tab)

    The generator never blocks on the critic, which is why comment K+1 is written
    against the model's own *draft* of comment K rather than the tightened version:
    waiting for the edit would serialise the whole thing and buy nothing, since the
    critic only shortens and never changes what a comment claims.

    This is also why `submit_comment` answers before the critique has run. The tool
    call resolves the anchor — local, deterministic, instant — and returns that
    verdict, which is the only part the commenter can act on. Blocking it on the
    editing pass would collapse the pipeline back to `sync` and tell the commenter
    nothing it could use.

    Posting is serial because the browser is — one tab, one selection state — so a
    single consumer thread pulls futures in order. Ordering therefore matches `sync`
    exactly; only the waiting differs.
    """
    queue: Queue = Queue()

    def poster() -> None:
        while True:
            item = queue.get()
            if item is None:
                return
            draft_quote, future = item
            try:
                quote, body = future.result()
            except Exception as e:
                # A backstop, not the editing policy: `Submitter.critique` keeps the
                # comment when the critic fails, so what reaches here is something
                # else — a malformed header, a bug. It still has to become a `Result`
                # rather than a dead poster thread, because the generator would go on
                # filling a queue nobody is reading and the run would report the
                # comments it silently lost as never proposed.
                sub.note(f"critic stage failed: {type(e).__name__}: {e}")
                results.append(
                    Result(
                        quote=draft_quote,
                        body="",
                        ok=False,
                        error=f"editing stage failed: {type(e).__name__}: {e}",
                    )
                )
                continue
            pairs.append((quote, body))
            if page is not None:
                results.append(sub.place(page, tab, quote, body))

    pool = ThreadPoolExecutor(max_workers=sub.cfg.critic_workers)
    worker = threading.Thread(target=poster, name="poster", daemon=True)
    worker.start()
    try:
        for quote, draft in stream:
            queue.put((quote, pool.submit(_edit, quote, draft, sub)))
    finally:
        queue.put(None)
        worker.join()
        pool.shutdown(wait=True)


def _submitter(doc_id, token, cfg: Config, tab: dict, label: str, budget, usage, notes):
    """The shared downstream half, configured once for whichever mode is running."""
    return submission.Submitter(
        tab["text"],
        cfg,
        label,
        budget=budget,
        usage=usage,
        notes=notes,
        # The same last rung in both modes, under the same setting. API mode used
        # to have no equivalent at all: a quote no string rule could place ended the
        # comment even with a key sitting right there.
        reconcile=(
            (lambda t, q: critic.reconcile_anchor(t, q, cfg, usage))
            if cfg.reconcile_anchors
            else None
        ),
        fresh_tab=(
            post_mod.api_tab_refresher(doc_id, token, tab)
            if cfg.source == "api" and token is not None
            else None
        ),
    )


def _pipeline(
    doc_id: str,
    token: str | None,
    cfg: Config,
    doc: dict,
    tab: dict,
    budget: int,
    focus: str | None,
    dry_run: bool,
) -> tuple[Run | None, list[tuple[str, str]], list[str]]:
    """Run the three stages, in the order `cfg.schedule` asks for.

    Both schedules share the commenter, the submitter, the critic, the caching and
    the verification pass; `async` overlaps the stages and `sync` does not. Keeping
    the difference to the scheduler is the point — otherwise a comment would depend
    on which schedule produced it.
    """
    results: list[Result] = []
    pairs: list[tuple[str, str]] = []
    notes: list[str] = []
    usage = model.Usage()

    reader = _reader(doc_id, token, cfg)
    before = set() if dry_run else post_mod.snapshot(reader)
    sub = _submitter(doc_id, token, cfg, tab, cfg.model, budget, usage, notes)
    stream = reviewer.propose_stream(
        doc["title"],
        tab["text"],
        budget,
        cfg,
        sub,
        focus,
        usage,
        tab=tab,
        prior=_prior_threads(doc_id, token),
    )
    run_stages = _sync if cfg.schedule == "sync" else _async

    # No lock on a dry run: it never opens the editor.
    with browser_lock(doc_id) if not dry_run else nullcontext():
        page = None if dry_run else open_doc(doc_id, tab["id"], port=cfg.port)
        try:
            run_stages(tab, stream, page, results, pairs, sub)
            # Suggestions type only after the last comment has posted: comments
            # never navigate a document containing our own edits.
            if page is not None and sub.suggestions:
                s_run = sub.place_suggestions(page, tab, doc_id)
                results.extend(s_run.results)
                notes.extend(s_run.notes)
        finally:
            if page is not None:
                page.close()

    if dry_run and sub.suggestions:
        notes.append(
            f"suggestions: {len(sub.suggestions)} accepted (dry run: nothing typed)"
        )
        # Shown in full: a dry run exists to judge what would land, and a
        # replacement is applied verbatim, so the preview must be verbatim too.
        for quote, replacement in sub.suggestions:
            notes.append(f"  would suggest on {quote!r}:\n    → {replacement}")
    notes.append(f"anchors: {sub.summary()}")
    notes.append(f"tokens: {usage.line()}")
    if dry_run or not results:
        return None, pairs, notes
    return _finish(doc_id, cfg, tab, results, before, reader, sub), pairs, notes


def review(
    doc_id: str,
    token: str,
    cfg: Config,
    tab_id: str | None = None,
    n: int | None = None,
    focus: str | None = None,
    dry_run: bool = False,
) -> tuple[Run | None, list[tuple[str, str]], list[str]]:
    """API mode: read the document, ask the model, post what it produces.

    One comment per model turn, critiqued and posted while the next is being
    written. `cfg.schedule` says whether those stages overlap.

    There used to be a second shape here — the whole set decided in one call, kept
    for comparison against this one. It assembled its own brief instead of going
    through `brief.sections`, and drifted until it was no longer comparable: no
    figures, though the Markdown it sent still carried the `[figure N]` markers; no
    record of what had already been said on the document; and a 4096-token reply
    limit rather than the configured one. A baseline that differs from the thing it
    measures in four ways nobody chose is not a baseline.
    """
    doc = read_document(doc_id, token, cfg, tab_id)
    tab = _pick_tab(doc, tab_id)
    budget = cfg.comments if n is None else n
    return _pipeline(doc_id, token, cfg, doc, tab, budget, focus, dry_run)


def post_batch(
    doc_id: str,
    token: str,
    cfg: Config,
    items: list,
    tab_id: str | None = None,
    dry_run: bool = False,
    as_model: str | None = None,
) -> tuple[Run | None, list[tuple[str, str]], list[str]]:
    """Agent mode: post `{quote, comment}` pairs an agent produced elsewhere.

    The commenter has finished by the time these arrive, so there is no conversation
    to re-anchor in — a rejection goes back to the submitter subagent instead, which
    is the same shape as API mode's tool result and is read by a model with the
    same document in front of it.

    `as_model` is what the header will name. In agent mode the tool cannot know which
    model did the thinking, so an agent should declare itself (`--as opus-5`);
    otherwise the header reads "agent", which tells a reader nothing about the source
    and defeats the point of having a header.
    """
    doc = read_document(doc_id, token, cfg, tab_id)
    tab = _pick_tab(doc, tab_id)
    usage = model.Usage()
    notes: list[str] = []
    sub = _submitter(
        doc_id, token, cfg, tab, as_model or "agent", cfg.comments, usage, notes
    )
    pairs, rejected = reviewer.vet(items, sub)
    rejected.extend(notes)
    reported = len(notes)
    rejected.append(f"anchors: {sub.summary()}")
    if dry_run and sub.suggestions:
        rejected.append(
            f"suggestions: {len(sub.suggestions)} accepted (dry run: nothing typed)"
        )
        for quote, replacement in sub.suggestions:
            rejected.append(f"  would suggest on {quote!r}:\n    → {replacement}")
    if dry_run or (not pairs and not sub.suggestions):
        # Reaching this with pairs in hand means `dry_run` — the other way into the
        # branch is `not pairs`. The editing pass runs in `_post`, which a dry run
        # never reaches, so what is printed is the wording before it is tightened.
        # Said out loud because the alternative — a preview that quietly differs
        # from what posts — is the kind of silent gap that stage was moved to close.
        if pairs and cfg.critic:
            rejected.append("dry run: bodies shown before the editing pass")
        return None, pairs, rejected
    run = _post(doc_id, token, cfg, tab, pairs, sub)
    # `notes` is sliced rather than drained because `_post` appends to it as it
    # edits. Without this second extend the "tightened ..." lines were produced and
    # then never printed, which is how a stage stops being visible.
    rejected.extend(notes[reported:])
    # After `_post`, because the editing pass is what spends tokens here. Agent mode
    # calls a model too — the critic runs on this side — and reported nothing, so
    # the one symptom of a dead cache breakpoint, a zero `cache read`, was invisible
    # in exactly the mode where nobody is watching an API bill for it.
    rejected.append(f"tokens: {usage.line()}")
    return run, pairs, rejected


# Appended to `submit_brief` only when suggestions are on. One batch for all of
# them, deliberately: within one `post-batch` call they type bottom-up, so no
# anchor is navigated across an edit already made — a guarantee that cannot hold
# across separate calls.
_SUGGEST_BRIEF = """
# Suggested edits

A suggested edit is an item with a `replacement` instead of a `comment`:

    {"quote": "...", "replacement": "..."}

The quote must match the document exactly — it is never widened or corrected the
way a comment's quote is — and the replacement is typed verbatim, with no editing
pass. Submit every suggested edit together, in ONE `post-batch` call, after the
comments: within one call they are applied bottom of the document first, which is
what keeps each one's anchor exact.
"""


def submit_brief(doc_id: str, token: str, cfg: Config, tab_id: str | None = None) -> str:
    """The operational half, for the subagent that places one comment.

    Anchoring is the whole job here. The editing pass used to be step 1 of this
    prompt, which made a *prompt* the only thing standing between agent mode and an
    unedited comment: any caller who reached `post-batch` without reading this — a
    coding agent taking the shortcut, a script — silently posted raw bodies.
    `_post` runs `critic.tighten` itself now, with the same prompt API mode uses,
    so both modes are edited by the same code rather than by two descriptions of the
    same rule that can drift apart.

    Kept out of the commenter's own brief on purpose. Anchoring rules and the CLI
    are clerical work; every line of it in the main brief is attention spent on
    something other than reading the document.
    """
    # With no tab named this used to interpolate the string "None", handing the
    # subagent a command line that names a tab no document has. Omitting the flag
    # lets `post-batch` apply the same rule as everything else: fine on a one-tab
    # document, and a refusal listing the tabs on any other.
    tab_flag = f" --tab {tab_id}" if tab_id else ""
    return f"""
You are placing one finished comment on a Google Doc. The comment has already been
written by a colleague reviewing the document. Do not second-guess whether it is
worth making — that judgement is not yours, and do not reword or shorten it either:
an editing pass runs on the way in. Your job is to make it land on the right words.

# 1. Submit it

Post the comment with its quote, exactly as it was given to you:

    echo '[{{"quote": "...", "comment": "..."}}]' | \
        marginal post-batch {doc_id}{tab_flag} --from - --as <model>

# 2. Fix anything it rejects

A rejected quote comes back with the reason and, where there is one, the nearest
passage in the document. Correct the quote and post again. The document's own text
is what a quote must match: it contains no Markdown syntax, no table pipes and no
footnote text, so a quote taken from any of those will not place. Anchor to the
author's own sentence closest to the point the comment makes.

Two attempts is usually enough. If a comment genuinely cannot be placed, say so and
stop rather than anchoring it somewhere it does not belong — a comment on the wrong
sentence is worse than no comment.
{_SUGGEST_BRIEF if cfg.suggestions else ""}""".strip()


def context(
    doc_id: str,
    token: str,
    cfg: Config,
    tab_id: str | None = None,
    focus: str | None = None,
) -> str:
    """Everything the commenter needs, and nothing operational.

    Agent mode's read half, and the same `brief.sections` API mode sends — the
    same instructions, the same budget, the same threads already on the document,
    the same figures, the same document. Only the rendering is ours: one channel
    instead of two, and figures written to disk because a terminal cannot carry an
    image and a coding agent can open a file.

    Operational detail stays out on purpose. The commenter should not be counting
    characters, checking a quote is unique, or driving a CLI; `submit_brief` holds
    all of that for the subagent that does it.
    """
    doc = read_document(doc_id, token, cfg, tab_id)
    tab = _pick_tab(doc, tab_id)
    handoff = f"""
# How to submit

Hand each comment to a subagent as soon as you have written it, then carry straight
on to the next one while that runs. Give the subagent the quote, the comment, and
this instruction:

    Run `marginal submit-brief {doc_id} --tab {tab['id']}` and follow it.

The subagent places the comment; it does not rewrite it. The editing pass for length
and redundancy runs inside the posting command, on {cfg.critic_model}, so write the
comment you mean and let it trim.
""".strip()
    if cfg.suggestions:
        handoff += """

Hand a suggested edit to a subagent the same way, giving it the quote and the
replacement instead of a comment. Suggested edits are typed into the document at
the end, so submit them as you go and expect no immediate change in the document.
""".rstrip()

    parts = brief.sections(
        doc["title"], tab["text"], tab, cfg, reviewer.COMMENTER_CONTRACT, handoff,
        budget=cfg.comments, focus=focus, prior=_prior_threads(doc_id, token),
        suggestions=reviewer.SUGGESTION_CONTRACT,
    )
    return brief.as_text(parts, FIGURE_CACHE / f"{doc_id}-{tab['id'] or 'only'}")


def _tab_of(doc: dict, quoted: str) -> dict | None:
    """The tab a comment is anchored in, found by its quoted text.

    A comment carries no tab id, only the text it quotes. On a one-tab document the
    answer is trivial; on this repository's own working documents it is not, and
    handing the responder the wrong tab would give it a confidently irrelevant
    document to answer from. Ambiguity resolves to nothing, as everywhere else here:
    a quote that appears in two tabs identifies neither, and the responder falls back
    to answering from the passage alone rather than from a guess.
    """
    tabs = doc.get("tabs") or []
    if len(tabs) == 1:
        return tabs[0]
    if not quoted:
        return None
    hits = [t for t in tabs if quoted in t.get("text", "")]
    return hits[0] if len(hits) == 1 else None


def respond(
    doc_id: str,
    token: str,
    cfg: Config,
    dry_run: bool = False,
) -> tuple[list[dict], model.Usage]:
    """Answer human replies on threads this tool started.

    Replies carry no anchor, so this path never touches the browser. A thread is
    answered when its most recent reply is not ours, which also means a rerun after
    a further human reply answers again — that is the intended live behaviour.

    The document is read once and every reply is answered against it, which is both
    why a reply can check a claim about the text and why the second thread onwards
    costs almost nothing: the document is one cached block ahead of the thread.
    """
    doc = read_document(doc_id, token, cfg, None)
    usage = model.Usage()
    # One read of the ledger, not two: `our_comment_ids` walks the same rows. The file
    # accumulates every comment and reply ever posted across every document, and it
    # was being read and parsed twice per invocation to answer two questions about
    # one snapshot of it.
    ledger_rows = ledger.rows(doc_id)
    ledger_ids = {r["comment_id"] for r in ledger_rows if r["kind"] == "comment"}
    ledger_bodies = {r["body"] for r in ledger_rows}
    out: list[dict] = []

    rendered: dict[str | None, list[dict]] = {}

    def material_for(tab: dict | None) -> list[dict] | None:
        """The rendered document for `tab`, built once per tab and reused.

        Building it downloads the figures, and `reviewer.respond` used to build its
        own for every thread — so a ten-thread run on an eight-figure paper fetched
        eighty images to send the same eight. Keyed by tab because `_tab_of` answers
        per thread: two threads can be anchored in different tabs, and one document
        block for the whole run would hand the second one the wrong tab.
        """
        if tab is None:
            return None
        key = tab["id"]
        if key not in rendered:
            title = doc.get("title") or "the document"
            rendered[key] = reviewer.material_blocks(title, tab, cfg)
        return rendered[key]

    def mine(body: str) -> bool:
        """Ours if it carries our header; the ledger is the fallback.

        The header is the better signal — it travels with the comment, so it works
        from another machine and survives a lost ledger. The ledger still covers
        comments posted before headers were enabled, or with them turned off.
        """
        return attribution.is_ours(body, cfg.header) or body.strip() in ledger_bodies

    for c in gdocs.list_comments(doc_id, token):
        if c.get("resolved"):
            continue
        if not (mine(c.get("content", "")) or c["id"] in ledger_ids):
            continue
        replies = c.get("replies") or []
        if not replies:
            continue
        if mine(replies[-1].get("content", "")):
            continue  # we spoke last; nothing to answer

        thread = [("Reviewer", attribution.strip(c["content"]))]
        for rep in replies:
            body = rep.get("content", "")
            who = "Reviewer" if mine(body) else "Author"
            thread.append((who, attribution.strip(body)))
        quoted = (c.get("quotedFileContent") or {}).get("value", "")
        tab = _tab_of(doc, quoted)
        text = attribution.apply(
            reviewer.respond(
                quoted,
                thread,
                doc.get("title") or "the document",
                cfg,
                usage=usage,
                material=material_for(tab),
            ),
            cfg.model,
            cfg.header,
        )
        item = {"comment_id": c["id"], "on": quoted, "reply": text}
        if not dry_run:
            gdocs.create_reply(doc_id, c["id"], text, token)
            ledger.record(doc_id, c["id"], quoted, text, cfg.model, kind="reply")
        out.append(item)
    return out, usage
