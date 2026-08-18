"""Suggested edits against a real document and a real editor.

What the live spike established (2026-08-18, headless Chrome 151, English UI),
and which of it these tests re-prove on every run:

* The mode switcher `#docs-toolbar-mode-switcher` carries the current mode in
  its class (`edit-mode` / `suggest-mode`); its label follows the UI language.
  It reads empty-and-disabled briefly at load, so the probe waits.
* The Cmd+Opt+Shift+Z chord does NOT switch modes; DOM clicks on the switcher's
  menu do, both directions.
* `Input.insertText` over a selection in Suggesting mode inserts WITHOUT
  deleting the selection — the replacement and the text it replaced both end up
  live. Backspace and forward-Delete over a word-shaped selection both
  suggest-delete one extra character (the preceding space). A real typed
  keypress replaces the selection byte-exactly; `Page.type_text` is that, and a
  keyDown/keyUp pair that both carry the text double-inserts and wedges the
  editor's input pipeline (it took the whole browser down once).
* The caret traverses BOTH the struck-through deleted text and the inserted
  text, so the live stream (which drops deletions) undercounts caret positions
  across a suggestion — which is why suggestions post bottom-up and comments
  post first.

These run the production path (`post.post_suggestions`), not raw CDP: what the
spike proved by hand, the code must now prove on demand.
"""

from __future__ import annotations

import pytest

from marginal import browser_source, gdocs
from marginal import post as post_mod
from marginal.docs_ui import editor_mode, open_doc

from tests.e2e import fixture

pytestmark = pytest.mark.e2e

EARLY = "sits before any of the awkward elements"
LATE = "comes after the table and is the real test"


def test_replacements_land_as_exact_tracked_changes(cfg, token, browser, fixture_doc):
    doc = gdocs.read_doc(fixture_doc, token)
    tab = doc["tabs"][0]
    text = tab["text"]
    pairs = [
        # Given in top-down order; the poster must reorder bottom-up itself.
        (EARLY, "sits before every one of the awkward elements"),
        (LATE, "comes after the table and is, in truth, the real test"),
    ]

    page = open_doc(fixture_doc, tab["id"], port=browser)
    try:
        run = post_mod.post_suggestions(
            page, fixture_doc, tab, pairs, port=browser,
            strategy=cfg.strategy, token=token,
        )
        assert [r.error for r in run.results if not r.ok] == []
        assert all(r.anchored == r.quote for r in run.results)
        assert run.notes == []

        # The editor must be back in editing mode: a shared account left in
        # suggesting mode turns the human's next edit into a suggestion.
        assert editor_mode(page) == "editing"

        # The exported stream must read as the transform the suggestions
        # describe — each quote replaced, everything else byte-identical,
        # offsets included. Exported before close (the export drives this page),
        # and after the mode probe (the export navigation reloads the editor).
        browser_source.invalidate(fixture_doc)
        blob = browser_source.export(page, fixture_doc, "docx", tab_id=tab["id"])
    finally:
        page.close()

    stream = browser_source.text_from_docx(blob)
    expected = text
    for quote, replacement in pairs:
        expected = expected.replace(quote, replacement)
    assert stream == expected

    # And the API reader must agree with the docx reader across our own
    # suggestions — the two-sources invariant, now with tracked changes present.
    assert gdocs.read_doc(fixture_doc, token)["tabs"][0]["text"] == stream


def test_an_anchor_before_a_posted_suggestion_is_unshifted(cfg, token, browser, fixture_doc):
    # Runs after the test above on the same session-scoped document: the fixture
    # now carries suggestions. A probe phrase the suggestions did not touch —
    # TARGETS[0] and TARGETS[3] were both replaced above — must still
    # verbatim-resolve, exactly once, in the post-suggestion stream.
    tab = gdocs.read_doc(fixture_doc, token)["tabs"][0]
    probe = fixture.TARGETS[1]
    assert tab["text"].count(probe) == 1
