"""Posting suggestions: ordering, the canary, mode restore, verification.

`docs_ui` is stubbed at the `post` module boundary, as the pipeline tests do for
comments: these tests are about what `post_suggestions` decides — what order it
types in, what it refuses to retry, what aborts the batch — not about the editor.
"""

from collections import Counter

import pytest

from marginal import post
from marginal.cdp import META
from marginal.docs_ui import SuggestNotReady

# Every quote sits mid-paragraph: a paragraph-initial quote is refused by design
# (the editor misplaces a typed replacement there), which is its own test.
DOC = "Say Alpha one two.\nSay Beta three four.\nSay Gamma five six.\n"
TAB = {"id": "t.0", "text": DOC, "paragraphs": []}


class FakePage:
    def __init__(self):
        self.keys = []

    def key(self, name, modifiers=0, **kw):
        self.keys.append((name, modifiers))


def _quiet(monkeypatch):
    monkeypatch.setattr(post.time, "sleep", lambda *_: None)


def _state(stream=DOC, sites=()):
    return lambda: (stream, [dict(s) for s in sites])


def site(deleted, inserted, stream=None):
    """A tracked change. Its offset indexes the live stream, so a state that
    also classifies plain-text landings must pass the stream it built."""
    offset = stream.index(inserted) if stream is not None and inserted in stream else 0
    return {"deleted": deleted, "inserted": inserted, "offset": offset}


def _post(monkeypatch, pairs, *, typed=None, state=None, mode_ok=True,
          suggest=None, restore_fails=False, mode_after="suggesting", record=None):
    """Run post_suggestions with everything scripted; returns (run, page, modes).

    `mode_after` is what the post-typing mode probe reads — "suggesting" for an
    editor behaving itself; a callable taking the count of items typed so far
    scripts a mode that changes part-way through the batch.
    """
    _quiet(monkeypatch)
    typed = typed if typed is not None else []
    modes = []

    def fake_set_mode(page, mode):
        if not mode_ok:
            raise RuntimeError("no mode switcher")
        if restore_fails and mode == "editing":
            raise RuntimeError("menu would not open")
        modes.append(mode)
        return "editing" if mode == "suggesting" else "suggesting"

    def fake_suggest(page, span, replacement, paragraphs, strategy, text=None):
        typed.append((span.quote, replacement))
        return 3

    def fake_editor_mode(page, timeout=5.0):
        return mode_after(len(typed)) if callable(mode_after) else mode_after

    monkeypatch.setattr(post, "set_mode", fake_set_mode)
    monkeypatch.setattr(post, "suggest_edit", suggest or fake_suggest)
    monkeypatch.setattr(post, "editor_mode", fake_editor_mode)
    page = FakePage()
    run = post.post_suggestions(
        page, "doc", TAB, pairs, port=0, read_state=state or _default_state(typed),
        record=record,
    )
    return run, page, modes


def _default_state(typed):
    """A document that faithfully records every typed suggestion as tracked."""

    def read():
        stream = DOC
        for q, r in typed:
            stream = stream.replace(q, r)
        sites = [site(q, r, stream) for q, r in typed]
        return stream, sites

    return read


def test_suggestions_post_bottom_up(monkeypatch):
    typed = []
    run, _, _ = _post(
        monkeypatch,
        [("Alpha one", "Alpha 1"), ("Gamma five", "Gamma 5"), ("Beta three", "Beta 3")],
        typed=typed,
    )
    assert [q for q, _ in typed] == ["Gamma five", "Beta three", "Alpha one"]
    assert all(r.ok for r in run.results)
    assert all(r.kind == "suggestion" for r in run.results)


def test_the_mode_is_restored_after_the_batch(monkeypatch):
    _, _, modes = _post(monkeypatch, [("Alpha one", "Alpha 1")])
    assert modes == ["suggesting", "editing"]


def test_a_failed_restore_is_a_note_not_a_silence(monkeypatch):
    run, _, _ = _post(monkeypatch, [("Alpha one", "Alpha 1")], restore_fails=True)
    assert any("switch it back by hand" in n for n in run.notes)


def test_an_unconfirmed_mode_types_nothing_and_says_so(monkeypatch):
    typed = []
    run, _, _ = _post(monkeypatch, [("Alpha one", "Alpha 1")], typed=typed, mode_ok=False)
    assert typed == []
    assert all("suggesting mode could not be confirmed" in r.error for r in run.results)


def test_not_ready_is_retried_and_a_typing_failure_is_not(monkeypatch):
    calls = {"n": 0}

    def flaky(page, span, replacement, paragraphs, strategy, text=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise SuggestNotReady("selection not confirmed")
        raise RuntimeError("websocket died mid-word")

    run, _, _ = _post(monkeypatch, [("Alpha one", "Alpha 1")], suggest=flaky,
                      state=_state())
    [r] = run.results
    assert calls["n"] == 2  # the not-ready was retried, the typing failure was not
    assert not r.ok
    assert "typing had begun" in r.error


def test_a_quote_that_drifted_onto_a_paragraph_start_is_refused_before_typing(monkeypatch):
    # Accept-time checked it too, but the document can change in between: the
    # fresh read hands back a stream where the quote now opens a paragraph.
    _quiet(monkeypatch)
    monkeypatch.setattr(post, "set_mode", lambda page, mode: "editing")
    typed = []
    monkeypatch.setattr(
        post, "suggest_edit",
        lambda *a, **k: typed.append(a) or 3,
    )
    drifted = {"id": "t.0", "text": "Beta three four.\nAlpha one two.\n", "paragraphs": []}
    run = post.post_suggestions(
        FakePage(), "doc", drifted, [("Beta three", "Beta 3")], port=0,
        read_state=_state(stream=drifted["text"]),
    )
    [r] = run.results
    assert typed == []
    assert not r.ok and "refused before typing" in r.error


def test_an_unresolvable_quote_is_reported_not_posted(monkeypatch):
    typed = []
    run, _, _ = _post(monkeypatch, [("nowhere at all", "x")], typed=typed)
    assert typed == []
    [r] = run.results
    assert not r.ok and "SpanError" in r.error


def _edited_state(typed):
    """A document where typing changed the text and nothing tracked it.

    What a real edit leaves behind — and, indistinguishably, what an accepted
    suggestion leaves behind. The post-typing mode probe is what tells them apart.
    """

    def read():
        if not typed:
            return DOC, []
        stream = DOC
        for q, r in typed:
            stream = stream.replace(q, r)
        return stream, []

    return read


def test_an_untracked_replacement_under_an_unconfirmed_mode_stops_the_batch_without_undo(monkeypatch):
    # The canary's positive case: the replacement is in the live stream, no
    # tracked change records it, and the editor did not read suggesting mode
    # after typing. Either the author accepted it or it was a real edit; the
    # export cannot tell, so nothing is undone — an undo of an acceptance would
    # delete the author's text — and everything after must not run.
    typed = []
    run, page, _ = _post(
        monkeypatch,
        [("Alpha one", "Alpha ONE"), ("Gamma five", "Gamma FIVE")],
        typed=typed, state=_edited_state(typed), mode_after="editing",
    )
    assert ("z", META) not in page.keys
    first = next(r for r in run.results if r.quote == "Gamma five")  # bottom-up: typed first
    rest = next(r for r in run.results if r.quote == "Alpha one")
    assert not first.ok and not first.unverified
    assert "REAL EDIT" in first.error and "editing mode" in first.error
    assert "'Gamma five'" in first.error and "'Gamma FIVE'" in first.error
    assert "Nothing was undone" in first.error
    assert not rest.ok and rest.error.startswith("not attempted")
    assert len(typed) == 1


def test_an_unreadable_probe_over_an_accepted_suggestion_is_never_undone(monkeypatch):
    # Codex review, finding 1: a probe exception used to become mode_ok=False,
    # and with the author's acceptance in the export that read as "edited" —
    # one undo, deleting text the author had just accepted.
    typed = []

    def unreadable(n_typed):
        raise RuntimeError("editor mode could not be read")

    run, page, _ = _post(
        monkeypatch, [("Beta three", "Beta 3")], typed=typed, state=_edited_state(typed),
        mode_after=unreadable,
    )
    [r] = run.results
    assert ("z", META) not in page.keys
    assert r.mode_after is None
    assert not r.ok and not r.unverified
    assert "an unreadable mode" in r.error and "Nothing was undone" in r.error


def test_the_same_picture_with_the_mode_confirmed_is_unverified_and_the_batch_goes_on(monkeypatch):
    # Codex review round 2, finding 1. A real edit followed by a probe that reads
    # suggesting is indistinguishable from an acceptance. It used to be reported
    # as a success; it is now the third outcome — not ok, not a hard failure,
    # recorded, batch not stopped (the editor is not known to be broken) — and
    # there is no undo on this path either.
    typed = []
    rows = []
    run, page, _ = _post(
        monkeypatch, [("Alpha one", "Alpha 1"), ("Beta three", "Beta 3")],
        typed=typed, state=_edited_state(typed), mode_after="suggesting",
        record=lambda r: rows.append(r.quote),
    )
    assert [q for q, _ in typed] == ["Beta three", "Alpha one"], "the batch went on"
    assert all(_is_unverified(r) for r in run.results)
    assert run.posted == []
    assert sorted(rows) == ["Alpha one", "Beta three"]
    assert ("z", META) not in page.keys


def test_a_verified_suggestion_reports_its_exact_halves(monkeypatch):
    run, _, _ = _post(monkeypatch, [("Beta three", "Beta 3")])
    [r] = run.results
    assert r.ok and r.anchored == "Beta three" and r.replacement == "Beta 3"


def test_a_merged_or_widened_site_fails_verification_loudly(monkeypatch):
    # The editor recorded a bigger deletion than the quote (the smart-delete
    # failure this feature's typing recipe exists to avoid). Exact or failure.
    typed = []

    def widened_state():
        if not typed:
            return DOC, []
        return DOC.replace(" Beta three", "Beta 3"), [site(" Beta three", "Beta 3")]

    run, _, _ = _post(monkeypatch, [("Beta three", "Beta 3")], typed=typed,
                      state=widened_state)
    [r] = run.results
    assert not r.ok
    assert "does not match the suggestion exactly" in r.error
    assert r.anchored == " Beta three"


def test_a_suggestion_that_never_appeared_fails_verification(monkeypatch):
    run, _, _ = _post(monkeypatch, [("Beta three", "Beta 3")], state=_state(sites=[]))
    [r] = run.results
    assert not r.ok and r.error == "typed suggestion could not be read back"


def test_pre_existing_identical_suggestions_are_not_claimed(monkeypatch):
    # A tracked change with the same halves already on the document must not
    # verify our typing: the count has to *exceed* the baseline.
    baseline = [site("Beta three", "Beta 3")]
    calls = {"n": 0}

    def state():
        calls["n"] += 1
        # Baseline read sees one; every later read still sees only that one —
        # our typing never landed.
        return DOC, [dict(s) for s in baseline]

    def silent(page, span, replacement, paragraphs, strategy, text=None):
        return 3  # claims to have typed; the document never shows it

    run, _, _ = _post(monkeypatch, [("Beta three", "Beta 3")], suggest=silent, state=state)
    [r] = run.results
    assert not r.ok and "could not be read back" in r.error


# --- landed as plain text before read-back (2026-09-16) ----------------------
#
# The author was accepting suggestions in the Doc while the run typed them. A
# suggestion accepted between typing and verification has no tracked change
# left to read back — the document simply reads as edited — and was reported as
# "could not be read back" although it had landed exactly. A real edit leaves
# the same document, and nothing read back afterwards can tell the two apart,
# so this is the third outcome, `unverified`: not ok, not the hard failure,
# recorded, and reported with the text to check. A document showing neither
# the tracked change nor the replacement where the quote was is still the hard
# failure it was.


def _is_unverified(r):
    return r.unverified and not r.ok and r.error is None and "unverified" in r.status


def _accepted_state(typed, tracked=()):
    """`tracked` names the quotes still pending; every other typed one was accepted."""

    def read():
        stream = DOC
        sites = []
        for q, r in typed:
            stream = stream.replace(q, r)
            if q in tracked:
                sites.append({"deleted": q, "inserted": r, "offset": stream.index(r)})
        return stream, sites

    return read


def test_a_suggestion_accepted_before_read_back_is_unverified_not_failed(monkeypatch):
    typed = []
    rows = []
    run, page, _ = _post(
        monkeypatch, [("Beta three", "Beta 3")], typed=typed, state=_accepted_state(typed),
        record=lambda r: rows.append(r.quote),
    )
    [r] = run.results
    assert _is_unverified(r)
    assert r.anchored == "Beta three"
    assert "check the document" in r.status
    assert ("z", META) not in page.keys, "an acceptance must never be undone"
    assert rows == ["Beta three"], "recorded, so the ledger knows it"
    assert run.posted == []


def test_an_unverified_first_suggestion_does_not_abort_the_batch(monkeypatch):
    # The canary used to read "replacement present, nothing tracked" as a real
    # edit and undo it. With the mode probe reading suggesting after typing, the
    # batch is not known to be broken, so the rest still types; the item itself
    # is unverified, not a success.
    typed = []
    run, page, _ = _post(
        monkeypatch,
        [("Alpha one", "Alpha 1"), ("Gamma five", "Gamma 5")],
        typed=typed, state=_accepted_state(typed, tracked={"Alpha one"}),
    )
    assert [q for q, _ in typed] == ["Gamma five", "Alpha one"]
    by_quote = {r.quote: r for r in run.results}
    assert _is_unverified(by_quote["Gamma five"])
    assert by_quote["Alpha one"].ok and not by_quote["Alpha one"].unverified
    assert ("z", META) not in page.keys


def test_a_widening_replacement_can_land_as_plain_text(monkeypatch):
    # The replacement contains the quote, so "the quote is gone" cannot be read
    # literally: what follows the quote is what changed.
    typed = []
    run, _, _ = _post(
        monkeypatch, [("Beta three", "Beta three and a half")],
        typed=typed, state=_accepted_state(typed),
    )
    [r] = run.results
    assert _is_unverified(r)


def test_a_shortening_whose_quote_contains_the_replacement_lands(monkeypatch):
    # Codex review round 2, finding 4: "the the answer." with quote "the answer"
    # and replacement "answer". A count of occurrences refused this; a position
    # does not.
    doc = "Say the the answer.\n"
    tab = {"id": "t.0", "text": doc, "paragraphs": []}
    _quiet(monkeypatch)
    monkeypatch.setattr(post, "set_mode", lambda page, mode: "editing")
    monkeypatch.setattr(post, "editor_mode", lambda page, timeout=5.0: "suggesting")
    typed = []
    monkeypatch.setattr(
        post, "suggest_edit",
        lambda page, span, replacement, *a, **k: typed.append((span.quote, replacement)) or 3,
    )
    run = post.post_suggestions(
        FakePage(), "doc", tab, [("the answer", "answer")], port=0,
        read_state=lambda: ("Say the answer.\n" if typed else doc, []),
    )
    [r] = run.results
    assert _is_unverified(r)


def test_the_replacement_elsewhere_with_the_quote_intact_did_not_land(monkeypatch):
    # The replacement text appearing somewhere else in the document, while the
    # quote still sits where it was, is not our edit landing.
    def state():
        return DOC + "Beta 3 appears here too.\n", []

    run, _, _ = _post(monkeypatch, [("Beta three", "Beta 3")], state=state)
    [r] = run.results
    assert not r.ok and not r.unverified
    assert r.error == "typed suggestion could not be read back"


def test_text_inside_a_pending_insertion_is_not_plain_text(monkeypatch):
    # A fresh tracked change covering the region — with halves that are not ours
    # — means the replacement we can see is that insertion, not plain text.
    typed = []

    def state():
        if not typed:
            return DOC, []
        stream = DOC.replace("Beta three", "Beta 3")
        return stream, [{"deleted": "three", "inserted": "3", "offset": stream.index("3")}]

    run, _, _ = _post(monkeypatch, [("Beta three", "Beta 3")], typed=typed, state=state)
    [r] = run.results
    assert not r.ok and not r.unverified


def test_neither_tracked_nor_landed_is_still_the_hard_failure(monkeypatch):
    run, _, _ = _post(monkeypatch, [("Beta three", "Beta 3")], state=_state(sites=[]))
    [r] = run.results
    assert not r.ok and not r.unverified
    assert r.error == "typed suggestion could not be read back"


def test_an_unverified_suggestion_is_counted_and_listed_apart(capsys):
    from marginal import cli

    run = post.Run(doc_id="d", tab_id="t", strategy="paragraph")
    run.results = [post.Result(quote="q", body="", ok=True, kind="suggestion",
                               replacement="better"),
                   post.Result(quote="p", body="", ok=True, kind="suggestion",
                               replacement="other")]
    post._unverified(run.results[1])
    assert cli._report(run, [], []) == 1, "an unverified item keeps the exit code non-zero"
    out = capsys.readouterr().out
    assert "posted 1/2, 1 unverified" in out
    assert "? " in out and "→ other" in out and "check the document" in out


# --- smart punctuation (2026-09-16) -------------------------------------------
#
# Docs stored a typed straight apostrophe as a curly one, so the tracked change
# read back differed from the replacement by one code point and a correct
# suggestion was reported as a failure. Comparison folds both sides; nothing in
# the stream or the result is rewritten.


def test_a_tracked_change_with_a_smart_apostrophe_matches_its_straight_one(monkeypatch):
    typed = []

    def state():
        sites = [site(q, r.replace("'", "’")) for q, r in typed]
        return DOC, sites

    run, _, _ = _post(
        monkeypatch, [("Beta three", "the document's Beta 3")], typed=typed, state=state,
    )
    [r] = run.results
    assert r.ok and not r.unverified
    assert r.replacement == "the document's Beta 3", "the result keeps what was typed"


def test_smart_double_quotes_fold_too(monkeypatch):
    typed = []

    def state():
        return DOC, [site(q, r.replace('"', "“", 1).replace('"', "”", 1))
                     for q, r in typed]

    run, _, _ = _post(monkeypatch, [("Beta three", 'say "Beta 3"')], typed=typed, state=state)
    [r] = run.results
    assert r.ok


def test_plain_text_is_matched_with_quotes_folded(monkeypatch):
    typed = []

    def state():
        stream = DOC
        for q, r in typed:
            stream = stream.replace(q, r.replace("'", "\u2019"))
        return stream, []

    run, _, _ = _post(
        monkeypatch, [("Beta three", "Beta's 3")], typed=typed, state=state,
    )
    [r] = run.results
    assert _is_unverified(r)


def test_a_pre_existing_site_that_differs_only_by_quote_style_is_still_baseline(monkeypatch):
    # The baseline is folded the same way, or a pre-existing curly-quoted change
    # would look fresh after our straight-quoted typing and verify it.
    baseline = [site("Beta three", "Beta’s 3")]

    def silent(page, span, replacement, paragraphs, strategy, text=None):
        return 3

    run, _, _ = _post(
        monkeypatch, [("Beta three", "Beta's 3")], suggest=silent,
        state=lambda: (DOC, [dict(s) for s in baseline]),
    )
    [r] = run.results
    assert not r.ok


# --- the ledger is written as each suggestion verifies (2026-09-16) ----------


def test_each_suggestion_is_recorded_as_it_verifies_and_failures_are_not(monkeypatch):
    typed = []
    rows = []
    run, _, _ = _post(
        monkeypatch,
        [("Alpha one", "Alpha 1"), ("Beta three", "Beta 3"), ("Gamma five", "Gamma 5")],
        typed=typed,
        # Alpha tracked, Gamma landed as plain text, Beta lost.
        state=lambda: (
            DOC.replace("Alpha one", "Alpha 1").replace("Gamma five", "Gamma 5"),
            [site("Alpha one", "Alpha 1")],
        ) if typed else (DOC, []),
        record=lambda r: rows.append((r.quote, r.unverified)),
    )
    by_quote = {r.quote: r for r in run.results}
    assert by_quote["Alpha one"].ok and _is_unverified(by_quote["Gamma five"])
    assert not by_quote["Beta three"].ok
    assert sorted(rows) == [("Alpha one", False), ("Gamma five", True)]


# --- every item is mode-checked after typing (2026-09-16) --------------------
#
# Only the first suggestion used to be canary-checked. The export is too slow
# (and too throttled) to read after every item, so every item gets the cheap
# check instead: the editor mode is probed immediately before typing and again
# immediately after, and a probe that stops reading "suggesting" triggers the
# export for that item and stops the batch before the next keystroke.


def test_the_mode_is_reprobed_after_every_typed_suggestion(monkeypatch):
    probes = []

    def mode_after(n_typed):
        probes.append(n_typed)
        return "suggesting"

    run, _, _ = _post(
        monkeypatch,
        [("Alpha one", "Alpha 1"), ("Beta three", "Beta 3"), ("Gamma five", "Gamma 5")],
        mode_after=mode_after,
    )
    assert all(r.ok and r.mode_after == "suggesting" for r in run.results)
    assert probes == [1, 2, 3], "one post-typing probe per item, after each typing"


def test_a_mode_that_drops_out_of_suggesting_stops_the_batch_before_the_next_item(monkeypatch):
    # Bottom-up: Gamma first, then Beta, then Alpha. The probe reads "editing"
    # after Beta. Beta itself was tracked (the export says so), so it verifies;
    # Alpha must not be attempted.
    typed = []
    run, page, _ = _post(
        monkeypatch,
        [("Alpha one", "Alpha 1"), ("Beta three", "Beta 3"), ("Gamma five", "Gamma 5")],
        typed=typed,
        mode_after=lambda n: "editing" if n >= 2 else "suggesting",
    )
    assert [q for q, _ in typed] == ["Gamma five", "Beta three"]
    by_quote = {r.quote: r for r in run.results}
    assert by_quote["Gamma five"].ok and by_quote["Beta three"].ok
    assert not by_quote["Alpha one"].ok
    assert by_quote["Alpha one"].error.startswith("not attempted")
    assert ("z", META) not in page.keys, "a tracked change is not undone"
    assert any("stopped typing" in n for n in run.notes)


def test_an_untracked_replacement_on_a_later_item_stops_the_batch(monkeypatch):
    # Not the first item: the second one is plain text with nothing tracking it
    # and the probe after it reads "editing". Reported, not undone, and the batch
    # stops — one real edit is never followed by more.
    typed = []

    def state():
        stream = DOC
        sites = []
        for q, r in typed:
            stream = stream.replace(q, r)
        for q, r in typed:
            if q == "Gamma five":
                sites.append(site(q, r, stream))
        return stream, sites

    run, page, _ = _post(
        monkeypatch,
        [("Alpha one", "Alpha 1"), ("Beta three", "Beta 3"), ("Gamma five", "Gamma 5")],
        typed=typed, state=state,
        mode_after=lambda n: "editing" if n >= 2 else "suggesting",
    )
    assert [q for q, _ in typed] == ["Gamma five", "Beta three"]
    by_quote = {r.quote: r for r in run.results}
    assert by_quote["Gamma five"].ok
    assert not by_quote["Beta three"].ok and "REAL EDIT" in by_quote["Beta three"].error
    assert by_quote["Alpha one"].error.startswith("not attempted")
    assert ("z", META) not in page.keys


def test_a_lagging_canary_cannot_turn_an_unconfirmed_mode_into_an_acceptance(monkeypatch):
    # Codex review, finding 2: the probe reads "editing", both canary reads lag
    # ("unknown"), the batch stops — and the final verification then sees the
    # plain text. Without the probe it used to call that accepted and record it.
    typed = []
    reads = {"n": 0}

    def lagging():
        reads["n"] += 1
        if not typed or reads["n"] <= 3:  # baseline + two canary reads lag
            return DOC, []
        stream = DOC
        for q, r in typed:
            stream = stream.replace(q, r)
        return stream, []

    rows = []
    run, page, _ = _post(
        monkeypatch, [("Alpha one", "Alpha 1"), ("Beta three", "Beta 3")],
        typed=typed, state=lagging, mode_after="editing",
        record=lambda r: rows.append(r.quote),
    )
    by_quote = {r.quote: r for r in run.results}
    assert len(typed) == 1  # the batch stopped after the first item
    first = by_quote["Beta three"]
    assert not first.ok and not first.unverified
    assert "editing mode" in first.error and "REAL EDIT" in first.error
    assert by_quote["Alpha one"].error.startswith("not attempted")
    assert rows == [] and ("z", META) not in page.keys


def test_an_unreadable_switcher_after_typing_is_kept_as_unknown_not_guessed(monkeypatch):
    def unreadable(page, timeout=5.0):
        raise RuntimeError("editor mode could not be read")

    monkeypatch.setattr(post, "editor_mode", unreadable)
    _quiet(monkeypatch)
    monkeypatch.setattr(post, "set_mode", lambda page, mode: "editing")
    typed = []
    monkeypatch.setattr(
        post, "suggest_edit",
        lambda page, span, replacement, *a, **k: typed.append((span.quote, replacement)) or 3,
    )
    run = post.post_suggestions(
        FakePage(), "doc", TAB, [("Beta three", "Beta 3")], port=0,
        read_state=_default_state(typed),
    )
    [r] = run.results
    assert r.mode_after is None
    # Tracked, so it verifies; the plain-text-with-no-tracked-change picture would
    # have been a real edit here, since the mode was not confirmed.
    assert r.ok


# --- the ledger row is written at the canary, not only at the end ----------


def test_a_suggestion_the_canary_confirms_is_recorded_before_the_next_one_types(monkeypatch):
    # Codex review, finding 4: a run killed while later suggestions typed lost
    # the row for the first one, although the canary had already confirmed it.
    rows = []
    typed = []

    class Killed(BaseException):
        pass

    def suggest(page, span, replacement, paragraphs, strategy, text=None):
        if typed:
            raise Killed()
        typed.append((span.quote, replacement))
        return 3

    with pytest.raises(Killed):
        _post(
            monkeypatch, [("Alpha one", "Alpha 1"), ("Beta three", "Beta 3")],
            typed=typed, suggest=suggest, record=lambda r: rows.append(r.quote),
        )
    assert rows == ["Beta three"]


def test_a_suggestion_recorded_at_the_canary_is_not_recorded_again(monkeypatch):
    rows = []
    run, _, _ = _post(
        monkeypatch,
        [("Alpha one", "Alpha 1"), ("Beta three", "Beta 3"), ("Gamma five", "Gamma 5")],
        record=lambda r: rows.append(r.quote),
    )
    assert all(r.ok for r in run.results)
    assert sorted(rows) == ["Alpha one", "Beta three", "Gamma five"]


# --- an echo of the replacement elsewhere is not evidence (Codex finding 3) --


def _classify(baseline, stream, sites, pairs):
    """Run the verification classifier over (quote, replacement) pairs."""
    from marginal.post import Result, _outcomes

    results = [Result(quote=q, body="", ok=True, kind="suggestion", replacement=r)
               for q, r in pairs]
    out = _outcomes(results, stream, sites, baseline)
    return [out[id(r)][0] for r in results]


PAD = "\n" * 7


def test_pending_edits_elsewhere_cannot_make_an_untouched_quote_look_landed():
    # Codex review round 2, finding 2. Two look-alike passages; pending zz→ZZ in
    # the first and ZZ→zz in the second flip every contextual count while
    # "Beta three" sits untouched. A count-based check verified it; a position
    # does not.
    baseline = PAD + "aa Beta 3 zz.\n" + PAD + "aa Beta three ZZ.\n"
    stream = PAD + "aa Beta 3 ZZ.\n" + PAD + "aa Beta three zz.\n"
    sites = [
        {"deleted": "zz", "inserted": "ZZ", "offset": stream.index("ZZ")},
        {"deleted": "ZZ", "inserted": "zz", "offset": stream.index("zz")},
    ]
    assert _classify(baseline, stream, sites, [("Beta three", "Beta 3")]) == ["missing"]


def test_a_genuine_landing_in_the_same_document_is_seen():
    baseline = PAD + "aa Beta 3 zz.\n" + PAD + "aa Beta three ZZ.\n"
    stream = PAD + "aa Beta 3 ZZ.\n" + PAD + "aa Beta 3 zz.\n"
    sites = [
        {"deleted": "zz", "inserted": "ZZ", "offset": stream.index("ZZ")},
        {"deleted": "ZZ", "inserted": "zz", "offset": stream.rindex("zz")},
    ]
    assert _classify(baseline, stream, sites, [("Beta three", "Beta 3")]) == ["plain"]


def test_a_shortening_whose_quote_contains_the_replacement_is_seen():
    # Finding 4, at the classifier: "the the answer." → "the answer.".
    pairs = [("the answer", "answer")]
    assert _classify("the the answer.\n", "the answer.\n", [], pairs) == ["plain"]
    # And the untouched document is not mistaken for it.
    assert _classify("the the answer.\n", "the the answer.\n", [], pairs) == ["missing"]


def test_offsets_are_mapped_across_pending_sites_and_earlier_landings():
    # A pending insertion and a pending deletion before the quote, plus one of
    # our own replacements that landed as plain text above it, all move it.
    baseline = "Say Alpha one two.\nSay gone Beta three four.\n"
    stream = "Say NEW Alpha 1 two.\nSay Beta 3 four.\n"
    sites = [
        {"deleted": "", "inserted": "NEW ", "offset": stream.index("NEW ")},
        {"deleted": "gone ", "inserted": "", "offset": stream.index("Beta")},
    ]
    pairs = [("Beta three", "Beta 3"), ("Alpha one", "Alpha 1")]
    assert _classify(baseline, stream, sites, pairs) == ["plain", "plain"]


def test_a_pending_insertion_touching_the_quote_start_is_read_both_ways():
    # From its offset alone, an insertion ending exactly where the quote begins
    # could sit before or after it; the check tries both and only an exact match
    # passes.
    baseline = "Say Beta three four.\n"
    sites = [{"deleted": "", "inserted": "very ", "offset": 4}]
    pairs = [("Beta three", "Beta 3")]
    assert _classify(baseline, "Say very Beta 3 four.\n", sites, pairs) == ["plain"]
    assert _classify(baseline, "Say very Beta three four.\n", sites, pairs) == ["missing"]


def test_a_quote_that_is_not_unique_in_the_baseline_cannot_be_positioned():
    baseline = "Beta three. Beta three.\n"
    pairs = [("Beta three", "Beta 3")]
    assert _classify(baseline, "Beta 3. Beta three.\n", [], pairs) == ["missing"]
