"""Post comments and verify what actually landed.

The verification step is the point: the browser cannot be trusted to have
anchored where we asked, so every posted comment is compared against the span we
intended by reading `quotedFileContent` back from the Drive API. A mismatch is a
loud failure with the wrong text in hand, not a silent success.
"""

from __future__ import annotations

import time
from collections import Counter
from dataclasses import asdict, dataclass, field

from . import gdocs, ledger
from .cdp import Page
from .docs_ui import post_comment, resolve_quote, select_span


@dataclass
class Result:
    quote: str
    body: str
    ok: bool
    anchored: str | None = None
    comment_id: str | None = None
    error: str | None = None
    select_events: int = 0
    post_seconds: float = 0.0


@dataclass
class Run:
    doc_id: str
    tab_id: str | None
    strategy: str
    results: list[Result] = field(default_factory=list)

    @property
    def posted(self) -> list[Result]:
        return [r for r in self.results if r.ok]

    def as_dict(self) -> dict:
        return {
            "doc_id": self.doc_id,
            "tab_id": self.tab_id,
            "strategy": self.strategy,
            "results": [asdict(r) for r in self.results],
        }


def api_reader(doc_id: str, token: str):
    """Verification via the Drive API: one call returns every anchor."""

    def read() -> list[dict]:
        return [
            {
                "id": c["id"],
                "body": (c.get("content") or "").strip(),
                "anchored": ((c.get("quotedFileContent") or {}).get("value") or "").strip(),
            }
            for c in gdocs.list_comments(doc_id, token)
        ]

    return read


def browser_reader(port: int, doc_id: str):
    """Verification with no Google credentials, via the document's docx export.

    Slower than the API — a download and a zip parse per check — and it carries no
    comment ids, so `unpost` cannot clean up a mismatch found this way. Checked
    against the API: the anchors it reports agree exactly, and none disagree.

    Exports run on their own tab. `export` navigates, and Docs bounces that
    navigation back to the document, reloading the editor — which would discard the
    selection state of the tab being driven. Taking a port rather than a Page keeps
    that separation honest: this never touches the tab being driven, so it is safe
    to call from a different thread than the one posting.
    """
    from . import browser_source

    def read() -> list[dict]:
        # The document has changed since any earlier export in this run.
        browser_source.invalidate(doc_id)
        scratch = Page.open(f"https://docs.google.com/document/d/{doc_id}/edit", port=port)
        try:
            return [dict(c, id=None) for c in browser_source.read_comments(scratch, doc_id)]
        finally:
            scratch.close()

    return read


SELECT_ATTEMPTS = 3
_AUTO_FRESHEN = object()


def api_tab_refresher(doc_id: str, token: str, tab: dict):
    """Return the current tab, rereading only when the Docs revision changed.

    The idea of gating a write on the document's revision is borrowed from
    LucaDeLeo/gdoc; the implementation here is our own, and applies it to a browser
    post rather than an API one. We cannot make the UI post atomic with the revision
    check, but we can refuse to navigate stale coordinates and resolve the quote
    again after every observed edit.
    """
    current_tab = tab
    revision = tab.get("revision_id")
    tab_id = tab.get("id")

    def fresh() -> dict:
        nonlocal current_tab, revision
        if not isinstance(revision, str) or not revision:
            raise RuntimeError("API document read contained no revision baseline")
        latest = gdocs.get_revision_id(doc_id, token)
        if latest == revision:
            return current_tab
        doc = gdocs.read_doc(doc_id, token)
        reread_revision = doc.get("revision_id")
        if not isinstance(reread_revision, str) or not reread_revision:
            raise RuntimeError("API document reread contained no revisionId")
        match = next(
            (
                candidate
                for candidate in doc.get("tabs", [])
                if candidate.get("id") == tab_id
            ),
            None,
        )
        if match is None:
            raise LookupError(f"tab {tab_id!r} disappeared after document revision changed")
        current_tab = match
        revision = reread_revision
        return current_tab

    return fresh


def post_one(
    page: Page, tab: dict, quote: str, body: str, strategy: str,
    attempts: int = SELECT_ATTEMPTS,
    fresh_tab=None,
) -> Result:
    """Select a span and post one comment on it. `ok` is provisional until verified.

    Split out of `post_many` so the pipeline can post each comment as it clears the
    critic, while the model is still writing the next one. `post_many` calls it in a
    loop for an already-written batch; neither path may skip the verification pass
    that follows.

    Selection is retried because it fails transiently. On a 15k-character document
    three comments in one run failed with the selection reading back as a single
    space, and the same quotes then selected correctly three times out of three
    moments later — the editor is mid-render, not the offsets wrong. Retrying is
    safe precisely because an unconfirmed selection is never posted: a retry can
    only turn a dropped comment into a placed one, never a right anchor into a
    wrong one.
    """
    # A Result per call, not a dict keyed by quote: two proposals may share a quote,
    # and a dict would collapse them into one intention.
    r = Result(quote=quote, body=body, ok=False)
    for attempt in range(1, attempts + 1):
        try:
            # Do this again on every selection retry, not merely once per comment.
            # A retry waits before touching the caret again, which is enough time
            # for a collaborator to edit the document a second time.  Applying the
            # first attempt's offsets after that wait would reopen the stale-window
            # this guard exists to close.
            active_tab = fresh_tab() if fresh_tab is not None else tab
            span = resolve_quote(active_tab["text"], quote)
            r.quote = span.quote  # resolved/stripped form is what gets anchored
        except Exception as e:
            r.error = f"{type(e).__name__}: {e}"
            return r
        # Only the selection is retried. Once submission has begun the comment may
        # already exist in the document — Cmd+Enter can reach Docs while the CDP
        # response is lost — and repeating it would post a second copy that
        # verification then claims as the first, reporting success and leaving a
        # duplicate live. A failure from here on is terminal and says so.
        try:
            t0 = time.perf_counter()
            exact, r.select_events, selected = select_span(
                page,
                span,
                active_tab["paragraphs"],
                strategy,
                text=active_tab["text"],
            )
            if not exact:
                # Refuse to comment on a span we could not confirm. A comment on the
                # wrong text is worse than no comment.
                raise RuntimeError(f"selection not confirmed (got {selected!r})")
        except Exception as e:
            r.error = f"{type(e).__name__}: {e}"
            if attempt < attempts:
                time.sleep(0.8 * attempt)
            continue
        try:
            post_comment(page, body)
        except Exception as e:
            r.error = (
                f"{type(e).__name__}: {e} — submission may have reached the document; "
                "not retried. Check the document before posting this comment again."
            )
            return r
        r.post_seconds = round(time.perf_counter() - t0, 3)
        r.ok = True
        r.error = None
        return r
    return r


def snapshot(read_comments) -> Counter:
    """What was already on the document, counted by (id, body).

    Must be taken before the first post. Counted rather than collected into a set
    because the browser source carries no ids: every comment there keys as
    `(None, body)`, so posting a second comment with a body the document already
    had made the new one indistinguishable from the old and it was filtered out as
    pre-existing — reported as never posted while sitting in the document.
    """
    return Counter((c["id"], c["body"]) for c in read_comments())


def post_many(
    page: Page,
    doc_id: str,
    tab: dict,
    comments: list[tuple[str, str]],
    token: str | None = None,
    strategy: str = "paragraph",
    read_comments=None,
    fresh_tab=_AUTO_FRESHEN,
) -> Run:
    """Post (quote, body) pairs, then verify every anchor in one pass.

    `read_comments` supplies the verification source — `api_reader` by default,
    `browser_reader` when running without Google credentials.
    """
    if read_comments is None:
        if token is None:
            raise ValueError("post_many needs either a token or an explicit read_comments")
        read_comments = api_reader(doc_id, token)
    if fresh_tab is _AUTO_FRESHEN:
        fresh_tab = api_tab_refresher(doc_id, token, tab) if token is not None else None
    run = Run(doc_id=doc_id, tab_id=tab["id"], strategy=strategy)
    before = snapshot(read_comments)
    for quote, body in comments:
        run.results.append(post_one(page, tab, quote, body, strategy, fresh_tab=fresh_tab))
    return verify(run, before, read_comments)


def verify(run: Run, before: set, read_comments) -> Run:
    """Match every provisionally-posted comment against what the document now shows.

    One pass for the whole run rather than one per comment: N round trips collapse
    into one, and the docx export path is slow enough that the difference is minutes.
    """
    # Comments persist asynchronously, so one can be posted and not yet be visible.
    # Identity is (id, body) because the browser source carries no ids — body alone
    # would treat a genuine duplicate as pre-existing.
    #
    # The docx export lags noticeably further behind than the Drive API: a comment
    # posted and read back immediately was absent, then present with the right anchor
    # seconds later, which reported a live comment as a failure. So back off longer
    # before concluding anything is missing.
    # A set from an older caller (and `set()` for a dry run) counts each entry once,
    # which is what it meant. Accepting both keeps this independent of who built it.
    was = before if isinstance(before, Counter) else Counter(before)

    def _new_comments() -> list[dict]:
        seen: Counter = Counter()
        fresh = []
        for c in read_comments():
            key = (c["id"], c["body"])
            seen[key] += 1
            if seen[key] > was[key]:
                fresh.append(c)
        return fresh

    # Wait for *our* bodies, not for a count. Counting every new comment let an
    # unrelated one — a collaborator commenting while the run is in flight — satisfy
    # the wait, ending the backoff before our own comment had propagated and
    # reporting a live comment as missing.
    want = Counter(r.body.strip() for r in run.results if r.ok)

    def _outstanding(new: list[dict]) -> int:
        have = Counter(c["body"] for c in new)
        return sum(max(0, n - have.get(body, 0)) for body, n in want.items())

    new = _new_comments()
    for wait in (1.5, 4.0, 8.0):
        if not _outstanding(new):
            break
        time.sleep(wait)
        new = _new_comments()

    # Match on the comment *body*, not the quoted text. Two proposals can share a
    # quote, and a collaborator commenting on the same passage mid-run would
    # otherwise be claimed as ours — reporting success for a comment we did not
    # post and hiding one that landed wrong.
    unclaimed = list(new)
    for r in run.results:
        if not r.ok:
            continue
        # Within the comments carrying our body, prefer the one anchored where this
        # result expected to land. Body alone is not an identity: two comments can
        # share it, and taking them in whatever order the API returned swapped their
        # anchors and reported two mismatches for two comments that landed correctly.
        same_body = [c for c in unclaimed if c["body"] == r.body.strip()]
        mine = next((c for c in same_body if c["anchored"] == r.quote), None)
        if mine is None:
            mine = same_body[0] if same_body else None
        if mine is None:
            r.ok = False
            r.error = "posted comment could not be read back"
            continue
        unclaimed.remove(mine)
        r.comment_id = mine["id"]
        r.anchored = mine["anchored"]
        if r.anchored != r.quote:
            # Wrongly anchored, and it is now live in the document. The id is
            # recorded where available so `unpost` can remove it.
            r.ok = False
            where = f"comment {mine['id']}" if mine["id"] else "the comment"
            r.error = f"anchor mismatch ({where} is live and wrong)"

    return run


def unpost(doc_id: str, comment_ids: list[str], token: str, force: bool = False) -> list[str]:
    """Delete comments this tool created. Returns the ids deleted.

    Deletion is permanent and the model posts under the same Google account as the
    human, so an id absent from the ledger may well be a comment a person wrote by
    hand. Refuse those unless explicitly forced.
    """
    known = ledger.our_comment_ids(doc_id)
    unknown = [c for c in comment_ids if c not in known]
    if unknown and not force:
        raise ValueError(
            f"not in the ledger, refusing to delete: {unknown}. "
            "These may be comments a person wrote. Pass force=True to override."
        )
    for cid in comment_ids:
        gdocs.delete_comment(doc_id, cid, token)
    return comment_ids
