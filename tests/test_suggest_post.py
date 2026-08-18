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


def site(deleted, inserted):
    return {"deleted": deleted, "inserted": inserted, "offset": 0}


def _post(monkeypatch, pairs, *, typed=None, state=None, mode_ok=True,
          suggest=None, restore_fails=False):
    """Run post_suggestions with everything scripted; returns (run, page, modes)."""
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

    monkeypatch.setattr(post, "set_mode", fake_set_mode)
    monkeypatch.setattr(post, "suggest_edit", suggest or fake_suggest)
    page = FakePage()
    run = post.post_suggestions(
        page, "doc", TAB, pairs, port=0, read_state=state or _default_state(typed),
    )
    return run, page, modes


def _default_state(typed):
    """A document that faithfully records every typed suggestion as tracked."""

    def read():
        sites = [site(q, r) for q, r in typed]
        stream = DOC
        for q, r in typed:
            stream = stream.replace(q, r)
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


def test_a_real_edit_aborts_the_batch_and_sends_one_undo(monkeypatch):
    # The canary's positive case: the replacement is in the live stream and no
    # tracked change records it. Everything after the first typing must not run.
    typed = []

    def edited_state():
        if not typed:
            return DOC, []
        stream = DOC
        for q, r in typed:
            stream = stream.replace(q, r)
        return stream, []  # text changed, nothing tracked

    run, page, _ = _post(
        monkeypatch,
        [("Alpha one", "Alpha ONE"), ("Gamma five", "Gamma FIVE")],
        typed=typed, state=edited_state,
    )
    assert ("z", META) in page.keys
    first = next(r for r in run.results if r.quote == "Gamma five")  # bottom-up: typed first
    rest = next(r for r in run.results if r.quote == "Alpha one")
    assert not first.ok and "REAL EDIT" in first.error
    assert not rest.ok and rest.error.startswith("not attempted")
    assert len(typed) == 1


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
