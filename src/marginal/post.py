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
from .browser_source import fold_quotes
from .cdp import Page
from .docs_ui import (
    SuggestNotReady,
    editor_mode,
    post_comment,
    resolve_quote,
    select_span,
    set_mode,
    suggest_edit,
)


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
    # "comment" or "suggestion". A suggestion's `body` is its rationale (may be
    # empty); the edit itself is `quote` -> `replacement`.
    kind: str = "comment"
    replacement: str | None = None
    # Suggestions only. `unverified` is the third outcome, neither ok nor a hard
    # failure: no tracked change was read back, and the replacement now sits as
    # plain text where the quote was. That is what the author accepting the
    # suggestion before verification could see it leaves behind — and also what
    # a real edit leaves behind, and nothing read back afterwards can tell the
    # two apart. `ok` stays False, `error` stays None and `status` says what to
    # check. `mode_after` is what the editor's mode switcher read immediately
    # after the replacement was typed — the per-item evidence that the
    # keystrokes were tracked, cheap enough to take every time where an export
    # is not.
    unverified: bool = False
    status: str | None = None
    mode_after: str | None = None


@dataclass
class Run:
    doc_id: str
    tab_id: str | None
    strategy: str
    results: list[Result] = field(default_factory=list)
    # Run-level facts that belong to no single result — a mode that could not be
    # restored, a stream that drifted from the expected transform. Never silent.
    notes: list[str] = field(default_factory=list)

    @property
    def posted(self) -> list[Result]:
        return [r for r in self.results if r.ok]

    def as_dict(self) -> dict:
        return {
            "doc_id": self.doc_id,
            "tab_id": self.tab_id,
            "strategy": self.strategy,
            "results": [asdict(r) for r in self.results],
            "notes": list(self.notes),
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
    record=None,
) -> Run:
    """Post (quote, body) pairs, then verify every anchor in one pass.

    `read_comments` supplies the verification source — `api_reader` by default,
    `browser_reader` when running without Google credentials. `record`, if given,
    is called with each result as soon as verification has read its id back; see
    `verify`.
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
    return verify(run, before, read_comments, record=record)


def verify(run: Run, before: set, read_comments, record=None) -> Run:
    """Match every provisionally-posted comment against what the document now shows.

    One pass for the whole run rather than one per comment: N round trips collapse
    into one, and the docx export path is slow enough that the difference is minutes.

    `record(result)` is called the moment a comment's id has been read back — for a
    wrongly anchored comment as well as a correct one, since both are live and
    ours, and `unpost` refuses an id the ledger never saw. It is called here, per
    comment, rather than by the caller over the finished run: the ledger used to
    be written after the run's *suggestions* had also been typed and verified,
    and a run killed during that slow tail left every comment it had posted
    absent from the ledger, where `unpost` then refused to delete it.
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
    # Suggestions in a mixed run were already verified against the document's
    # tracked changes; matching them here against the *comments* would fail every
    # one for lacking a comment body.
    want = Counter(r.body.strip() for r in run.results if r.ok and r.kind == "comment")

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
        if not r.ok or r.kind != "comment":
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
        if record is not None and (r.ok or r.comment_id):
            record(r)

    return run


# --- suggested edits ---------------------------------------------------------
#
# A suggestion is posted like a comment — navigate, select, act — but the act is
# typing in Suggesting mode, and verification reads the document's tracked
# changes rather than its comments. The docx export is the one source that can
# see a tracked change (the Docs API cannot create one and `list_comments`
# cannot see one), so suggestion verification always goes through the export,
# whichever source the run reads its text from.


def suggestion_state_reader(port: int, doc_id: str, tab_id: str | None = None):
    """Return () -> (live_stream, sites) from one docx export.

    One export serves both questions verification asks — what does the stream say
    now, and which tracked changes exist — so they can never disagree with each
    other. Runs on a scratch tab for the same reason `browser_reader` does: the
    export navigates, and Docs bounces that navigation into a reload that would
    discard the driven tab's state.
    """
    from . import browser_source

    def read() -> tuple[str, list[dict]]:
        browser_source.invalidate(doc_id)
        scratch = Page.open(f"https://docs.google.com/document/d/{doc_id}/edit", port=port)
        try:
            blob = browser_source.export(scratch, doc_id, "docx", tab_id=tab_id)
        finally:
            scratch.close()
        return (
            browser_source.text_from_docx(blob),
            browser_source.suggestions_from_docx(blob),
        )

    return read


def post_suggestion_one(
    page: Page, tab: dict, quote: str, replacement: str, strategy: str,
    attempts: int = SELECT_ATTEMPTS,
    fresh_tab=None,
) -> Result:
    """Type one suggested replacement over `quote`. `ok` is provisional until verified.

    The same retry boundary as `post_one`, moved one act later: selection and the
    mode probe are retried (`SuggestNotReady` promises nothing was typed), and the
    first typed character is terminal — a typing failure may have half-applied the
    edit, and repeating it could apply it twice.

    Re-resolution here is verbatim-only by construction (`resolve_quote`): however
    the quote was anchored at accept time, the text actually edited must appear
    exactly once in the current stream, or the suggestion is refused.

    `suggest_edit` probes the editor mode immediately before the first keystroke;
    this probes it again immediately after the last one and keeps the answer on
    the result (`mode_after`). A DOM read, a few milliseconds, so it is affordable
    on every item where a docx export (seconds, and throttled when repeated) is
    not. It is evidence, not a verdict: the caller decides what a mode that is no
    longer "suggesting" means, and an unreadable switcher is kept as None rather
    than guessed.
    """
    r = Result(quote=quote, body="", ok=False, kind="suggestion", replacement=replacement)
    for attempt in range(1, attempts + 1):
        try:
            active_tab = fresh_tab() if fresh_tab is not None else tab
            span = resolve_quote(active_tab["text"], quote)
            r.quote = span.quote
        except Exception as e:
            r.error = f"{type(e).__name__}: {e}"
            return r
        if span.start == 0 or active_tab["text"][span.start - 1] in "\n\v":
            # Checked at accept time too, but the document may have changed since:
            # an edit upstream can move a mid-line quote onto a paragraph or
            # soft-line-break boundary, where a typed replacement lands displaced.
            # Refused before anything is typed.
            r.error = (
                "the quote now begins at the very start of a line, where a "
                "typed replacement lands displaced; refused before typing"
            )
            return r
        try:
            t0 = time.perf_counter()
            r.select_events = suggest_edit(
                page, span, replacement, active_tab["paragraphs"], strategy,
                text=active_tab["text"],
            )
        except SuggestNotReady as e:
            r.error = f"{type(e).__name__}: {e}"
            if attempt < attempts:
                time.sleep(0.8 * attempt)
            continue
        except Exception as e:
            r.error = (
                f"{type(e).__name__}: {e} — typing had begun; the edit may be partly "
                "applied. Check the document before suggesting this again."
            )
            return r
        r.post_seconds = round(time.perf_counter() - t0, 3)
        try:
            r.mode_after = editor_mode(page)
        except Exception:
            r.mode_after = None
        r.ok = True
        r.error = None
        return r
    return r


def _site_key(site: dict) -> tuple[str, str]:
    """A tracked change's identity for matching: its halves, smart quotes folded."""
    return fold_quotes(site["deleted"]), fold_quotes(site["inserted"])


def _want_key(r: Result) -> tuple[str, str]:
    """The halves a result asked for, folded the same way as `_site_key`.

    Docs stores a typed straight apostrophe as a curly one (`document's` becomes
    `document’s`), so the inserted half read back from the export can differ from
    the replacement typed by exactly that substitution. Both sides are folded before
    comparison; neither the stream nor the result is rewritten.
    """
    return fold_quotes(r.quote), fold_quotes(r.replacement or "")


def _fresh_sites(sites: list[dict], before: Counter) -> list[dict]:
    """The tracked changes beyond the baseline, in document order.

    Only these may verify anything: a pre-existing tracked change with the same
    halves is somebody else's edit, not proof ours landed.
    """
    remaining = Counter(before)
    out = []
    for s in sites:
        key = _site_key(s)
        if remaining[key] > 0:
            remaining[key] -= 1
        else:
            out.append(s)
    return out


def _baseline_offset(baseline: str, r: Result) -> int | None:
    """Where `r`'s quote sits in the baseline stream, or None if not exactly once."""
    quote = _want_key(r)[0]
    folded = fold_quotes(baseline)
    return folded.index(quote) if folded.count(quote) == 1 else None


def _map_offset(at: int, fresh_sites: list[dict], shifts: list[tuple[int, int]]) -> list[int]:
    """Candidate positions in the current stream for baseline offset `at`.

    Two kinds of change move text after them. Our own replacements that landed
    as plain text (`shifts`, as (baseline offset, net length change)) are in
    baseline coordinates and applied first. Pending tracked changes
    (`fresh_sites`) are in current coordinates — inserted text counts, deleted
    text does not — and are applied in document order while they end at or
    before the moving position. A site that *touches* the position cannot be
    told, from its offset alone, to be before or after the quote, so both
    readings are returned and the caller tries each; the check that follows is
    exact enough that a wrong reading fails rather than passes.
    """
    mapped = at
    for off, delta in shifts:
        if off < at:
            mapped += delta
    candidates = [mapped]
    for s in sorted(fresh_sites, key=lambda s: s["offset"]):
        delta = len(s["inserted"]) - len(s["deleted"])
        s_end = s["offset"] + len(s["inserted"])
        moved = []
        for m in candidates:
            if s_end < m:
                moved.append(m + delta)
            elif s["offset"] <= m <= s_end:
                moved.extend((m, m + delta))
            else:
                moved.append(m)
        candidates = sorted(set(moved))
    return candidates


def _landed(
    r: Result, at: int, stream: str, baseline: str, fresh_sites: list[dict],
    shifts: list[tuple[int, int]],
) -> bool:
    """Whether `r`'s replacement now sits as plain text exactly where its quote was.

    Position, not counts: the quote's unique baseline offset is mapped into the
    current stream (`_map_offset`), and the stream there must start with the
    replacement and no longer start with the quote — with smart quotes folded
    on both sides. Counting occurrences of "surroundings plus replacement" was
    fooled by look-alike passages whenever pending edits elsewhere shifted the
    counts, and refused a genuine shortening whose quote contained its
    replacement; a position is indifferent to both.

    "No longer the quote" is read against the quote's own tail where the
    replacement begins with the quote (a widening), since the quote is then
    present at the offset either way and only what follows it differs. No
    pending tracked change may overlap the replacement: text inside an
    insertion is not plain text.
    """
    quote, repl = _want_key(r)
    stream = fold_quotes(stream)
    baseline_tail = None
    if repl.startswith(quote):
        after = at + len(quote)
        baseline_tail = quote + fold_quotes(baseline)[after:after + 16]
    for mapped in _map_offset(at, fresh_sites, shifts):
        if mapped < 0 or not stream.startswith(repl, mapped):
            continue
        if baseline_tail is not None:
            if stream.startswith(baseline_tail, mapped):
                continue
        elif stream.startswith(quote, mapped):
            continue
        end = mapped + len(repl)
        clear = True
        for s in fresh_sites:
            s_start = s["offset"]
            s_end = s_start + len(s["inserted"])
            inside = mapped < s_start < end  # a pure deletion sitting inside it
            if (s_start < end and s_end > mapped) or inside:
                clear = False
                break
        if clear:
            return True
    return False


def _outcomes(want: list[Result], stream: str, fresh_sites: list[dict], baseline: str) -> dict:
    """Classify every provisionally-typed result against one export, top-down.

    Returns {id(result): (kind, detail)} with kind one of 'tracked' (an exact
    site, consumed so two identical suggestions need two sites), 'mismatch' (a
    site containing the halves; detail is the site), 'plain' (the replacement
    landed as plain text where the quote was) or 'missing'. Document order
    matters: a replacement that landed as plain text shifts every offset after
    it, and only results already classified can be counted, so results are
    taken top-down whatever order they were typed in.
    """
    fresh = Counter(_site_key(s) for s in fresh_sites)
    positions = {id(r): _baseline_offset(baseline, r) for r in want}
    ordered = sorted(want, key=lambda r: (positions[id(r)] is None, positions[id(r)] or 0))
    shifts: list[tuple[int, int]] = []
    out: dict = {}
    for r in ordered:
        key = _want_key(r)
        if fresh[key] > 0:
            fresh[key] -= 1
            out[id(r)] = ("tracked", None)
            continue
        near = _tracked_near(r, fresh_sites)
        if near is not None:
            out[id(r)] = ("mismatch", near)
            continue
        at = positions[id(r)]
        if at is not None and _landed(r, at, stream, baseline, fresh_sites, shifts):
            quote, repl = key
            shifts.append((at, len(repl) - len(quote)))
            out[id(r)] = ("plain", None)
            continue
        out[id(r)] = ("missing", None)
    return out


def _unverified(r: Result) -> None:
    """The third outcome: landed as plain text, which nothing can vouch for."""
    r.ok = False
    r.error = None
    r.unverified = True
    r.anchored = r.quote
    r.status = (
        "unverified: replacement present as plain text, nothing tracked; the "
        "author may have accepted it, or it was a real edit; check the document"
    )


def _tracked_near(r: Result, fresh_sites: list[dict]) -> dict | None:
    """A fresh tracked change whose halves *contain* the ones asked for.

    Proof the keystrokes were tracked, and also proof the editor recorded a
    different edit than the one asked for — merged with a neighbour, or widened
    by a character. `verify_suggestions` reports it as a mismatch with the deleted
    text in hand; the canary only needs to know it was not a real edit.
    """
    quote, repl = _want_key(r)
    return next(
        (
            s for s in fresh_sites
            if quote in fold_quotes(s["deleted"]) and repl in fold_quotes(s["inserted"])
        ),
        None,
    )


def _canary(
    r: Result, read_state, before: Counter, baseline_stream: str, landed: list[Result]
) -> str:
    """Classify what a typed suggestion actually did, from one fresh export.

    Four answers. 'suggested': a tracked change with our exact halves exists.
    'tracked': a tracked change *contains* our halves — the keystrokes were
    tracked, as a different edit that verification will report. 'plain': no
    tracked change, and the replacement is plain text where the quote was —
    what the author accepting it leaves behind, and what a real edit leaves
    behind, indistinguishably. 'unknown': the export shows neither yet; it
    lags, so absence is not evidence of anything.

    The caller reads 'plain' with the post-typing mode probe: an observation of
    the editor afterwards, not a record of what the keystrokes did. Nothing here
    is grounds for an undo — undoing an acceptance deletes the author's text.
    `landed` carries the results already known to be plain text, whose length
    changes move everything after them.
    """
    for wait in (1.5, 4.0):
        stream, sites = read_state()
        fresh_sites = _fresh_sites(sites, before)
        kind, _ = _outcomes(landed + [r], stream, fresh_sites, baseline_stream)[id(r)]
        if kind == "tracked":
            return "suggested"
        if kind == "mismatch":
            return "tracked"
        if kind == "plain":
            return "plain"
        time.sleep(wait)
    return "unknown"


def _untracked_error(r: Result) -> str:
    """The report for a replacement that is plain text with nothing tracking it,
    typed while the editor could not be confirmed in suggesting mode."""
    read = r.mode_after or "an unreadable"
    return (
        f"now plain text with no tracked change, and the editor read {read} mode "
        f"after typing: either the author accepted it or it was a REAL EDIT. "
        f"Check the document — {r.quote[:60]!r} reads {(r.replacement or '')[:60]!r}. "
        "Nothing was undone."
    )


def post_suggestions(
    page: Page,
    doc_id: str,
    tab: dict,
    pairs: list[tuple[str, str]],
    port: int,
    strategy: str = "paragraph",
    read_state=None,
    fresh_tab=_AUTO_FRESHEN,
    token: str | None = None,
    record=None,
) -> Run:
    """Post (quote, replacement) pairs as suggested edits, bottom of the document first.

    Descending start-offset order is the coordinate-space discipline: a suggestion
    shifts every offset after itself (inserted text is live, deleted text leaves),
    so posting bottom-up means no quote is ever navigated to across one of our own
    edits — each remaining quote sits strictly before everything already changed.

    After the first suggestion is typed, a canary read classifies what actually
    happened before any further typing: a replacement that is plain text with
    nothing tracking it, typed while the editor could not be confirmed in
    suggesting mode, stops the whole batch and says so loudly. It is not undone:
    the same document is what an acceptance by the author leaves behind, and an
    undo of that deletes their text. The operator is told exactly what to check.

    Every later suggestion gets the cheap half of that check: the editor mode is
    probed immediately before typing (in `suggest_edit`) and again immediately
    after (in `post_suggestion_one`). A probe that no longer reads "suggesting"
    triggers the canary export for *that* item, so one real edit cannot be
    followed by more. The export is not run after every item on purpose: it is a
    download and a zip parse — seconds each, plus the lag waits — and repeated
    exports in quick succession are throttled by Google into producing nothing,
    which would turn the check itself into the failure.

    `record`, if given, is called with each suggestion as soon as it is confirmed
    — at the canary read where that is the first confirmation, otherwise in the
    verification pass — and once only.
    """
    if read_state is None:
        read_state = suggestion_state_reader(port, doc_id, tab_id=tab.get("id"))
    if fresh_tab is _AUTO_FRESHEN:
        fresh_tab = api_tab_refresher(doc_id, token, tab) if token is not None else None

    run = Run(doc_id=doc_id, tab_id=tab.get("id"), strategy=strategy)

    # Resolve everything up front against one stream, for the ordering. Posting
    # re-resolves against a fresh read anyway; a pair that fails here is reported
    # rather than posted in whatever order it happened to arrive.
    ordered: list[tuple[int, str, str]] = []
    for quote, replacement in pairs:
        try:
            span = resolve_quote(tab["text"], quote)
        except Exception as e:
            run.results.append(Result(
                quote=quote, body="", ok=False, kind="suggestion",
                replacement=replacement, error=f"{type(e).__name__}: {e}",
            ))
            continue
        ordered.append((span.start, span.quote, replacement))
    ordered.sort(key=lambda t: t[0], reverse=True)
    if not ordered:
        return run

    baseline_stream, baseline_sites = read_state()
    before = Counter(_site_key(s) for s in baseline_sites)

    # One row per result, however many passes confirm it: the canary records a
    # suggestion it sees tracked, and verification must not record it again.
    recorded: set[int] = set()

    def record_once(r: Result) -> None:
        if record is not None and id(r) not in recorded:
            recorded.add(id(r))
            record(r)

    try:
        previous_mode = set_mode(page, "suggesting")
    except Exception as e:
        for _, quote, replacement in ordered:
            run.results.append(Result(
                quote=quote, body="", ok=False, kind="suggestion", replacement=replacement,
                error=f"suggesting mode could not be confirmed: {e}",
            ))
        return run

    aborted = None
    landed: list[Result] = []
    try:
        first_typed = True
        for _, quote, replacement in ordered:
            if aborted:
                run.results.append(Result(
                    quote=quote, body="", ok=False, kind="suggestion",
                    replacement=replacement, error=f"not attempted: {aborted}",
                ))
                continue
            r = post_suggestion_one(
                page, tab, quote, replacement, strategy, fresh_tab=fresh_tab
            )
            run.results.append(r)
            if not r.ok:
                continue
            mode_ok = r.mode_after == "suggesting"
            if not first_typed and mode_ok:
                continue
            # The export is read for the first item always, and for any later item
            # whose post-typing mode probe stopped reading "suggesting" — the one
            # cheap signal that the keystrokes may not have been tracked.
            first_typed = False
            verdict = _canary(r, read_state, before, baseline_stream, landed)
            if verdict == "suggested":
                # Confirmed now; recorded now. Waiting for the verification pass
                # left a run killed while later items typed with no row for it.
                r.anchored = r.quote
                record_once(r)
            elif verdict == "plain" and not mode_ok:
                # Plain text, nothing tracked, and the editor could not be
                # confirmed in suggesting mode after typing. An acceptance and a
                # real edit look the same from here, so this is reported and the
                # batch stopped — and nothing is undone, because undoing an
                # acceptance deletes the author's own text.
                r.ok = False
                r.error = _untracked_error(r) + " Run stopped before any further typing."
                aborted = "an earlier suggestion is plain text with nothing tracking it"
            elif verdict == "plain":
                # The same document with the mode confirmed: the batch is not
                # known to be broken, so it goes on, but nothing can vouch for
                # this item. Recorded now, so the ledger knows it whatever
                # happens next.
                _unverified(r)
                landed.append(r)
                record_once(r)
            if aborted is None and not mode_ok:
                # 'unknown', 'suggested' or 'tracked' with a mode probe that no
                # longer reads suggesting. A tracked change proves this item, but
                # the editor is not in a state to type the next one; refuse rather
                # than find out.
                aborted = (
                    f"the editor read {r.mode_after or 'an unreadable'} mode after "
                    f"typing on {quote[:40]!r}"
                )
                run.notes.append(
                    f"stopped typing: {aborted}; the suggestions already typed are "
                    "verified below, the rest were not attempted"
                )
            # 'unknown' with the mode confirmed is not evidence: the export lags.
            # The verification pass below is the judge; only positive evidence aborts.
    finally:
        try:
            set_mode(page, previous_mode)
        except Exception as e:
            run.notes.append(
                f"the editor could not be switched back to {previous_mode} mode ({e}); "
                "switch it back by hand or the next human edit becomes a suggestion"
            )

    return verify_suggestions(run, before, read_state, baseline_stream, record=record_once)


def verify_suggestions(
    run: Run, before: Counter, read_state, baseline_stream: str = "", record=None
) -> Run:
    """Match every provisionally-typed suggestion against the document's tracked changes.

    Exact halves or failure: a site whose deleted text merely *contains* the quote
    means the editor recorded a different edit than the one asked for (or merged it
    with an adjacent suggestion), and a suggestion that edits more or less than its
    quote is a wrong anchor wearing a success face. `anchored` carries the deleted
    text actually recorded, so a mismatch is reported with the wrong text in hand.

    Halves are compared with smart quotes folded (`_want_key`), because Docs stores
    a typed straight apostrophe as a curly one and a byte-for-byte match reported
    correct suggestions as failures.

    A suggestion with no tracked change whose replacement sits as plain text where
    the quote was (`_landed`, position-based against `baseline_stream`) is the
    third outcome, `unverified`: the author may have accepted it between our
    typing and this read, or the typing may have been a real edit, and nothing
    read back can say which. It is not ok and it is not the hard failure; it is
    reported with the text to check and recorded in the ledger. Where the
    post-typing mode probe did not read "suggesting" the same picture *is* the
    hard failure `_untracked_error` describes. Neither a tracked change nor plain
    text where the quote was is still "typed suggestion could not be read back".

    `record(result)` is called per suggestion as it verifies, for the reason given
    in `verify`.
    """
    want = [r for r in run.results if r.ok]
    stream, sites = "", []
    fresh_sites: list[dict] = []
    outcomes: dict = {}

    for wait in (1.5, 4.0, 8.0):
        stream, sites = read_state()
        fresh_sites = _fresh_sites(sites, before)
        outcomes = _outcomes(want, stream, fresh_sites, baseline_stream)
        if all(kind != "missing" for kind, _ in outcomes.values()):
            break
        time.sleep(wait)

    for r in want:
        kind, detail = outcomes[id(r)]
        if kind == "tracked":
            r.anchored = r.quote
            if record is not None:
                record(r)
        elif kind == "mismatch":
            r.ok = False
            r.anchored = detail["deleted"]
            r.error = (
                "the tracked change does not match the suggestion exactly "
                f"(deleted {detail['deleted'][:60]!r}); it is live and should be checked"
            )
        elif kind == "plain":
            if r.mode_after != "suggesting":
                r.ok = False
                r.error = _untracked_error(r)
                continue
            _unverified(r)
            if record is not None:
                record(r)
        else:
            r.ok = False
            r.error = "typed suggestion could not be read back"
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
