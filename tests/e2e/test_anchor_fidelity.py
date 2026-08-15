"""Does a comment land on the text it was aimed at?

The property the whole tool rests on. An offset that is wrong by one displaces
what *follows* an element, so every probe sits after a hazard rather than on it.
"""

import pytest

from marginal import browser_source, gdocs
from marginal import post as post_mod
from marginal.docs_ui import open_doc

from . import fixture

pytestmark = pytest.mark.e2e


@pytest.fixture(scope="module")
def tab(fixture_doc, token):
    return gdocs.read_doc(fixture_doc, token)["tabs"][0]


def test_the_fixture_contains_every_hazard(tab):
    assert tab["figures"], "no inline image — the offset hazard is not represented"
    assert tab["tables"] and tab["footnotes"]
    for target in fixture.TARGETS:
        assert target in tab["text"], f"fixture lost its probe: {target!r}"


def test_every_probe_anchors_exactly(fixture_doc, token, browser, cfg, tab):
    # Posts a comment per probe, reads the anchor back from Drive, then removes it.
    pairs = [(t, f"e2e probe {i}") for i, t in enumerate(fixture.TARGETS)]
    page = open_doc(fixture_doc, tab["id"], port=browser)
    try:
        run = post_mod.post_many(page, fixture_doc, tab, pairs, token, cfg.strategy)
    finally:
        page.close()
    try:
        wrong = [
            (r.quote, r.anchored or r.error)
            for r in run.results
            if not (r.ok and r.anchored == r.quote)
        ]
        assert not wrong, f"anchors drifted: {wrong}"
    finally:
        # force: these never reached the ledger, and the run created them seconds ago.
        ids = [r.comment_id for r in run.results if r.comment_id]
        if ids:
            post_mod.unpost(fixture_doc, ids, token, force=True)


def test_a_paragraph_final_anchor_does_not_swallow_the_newline(
    fixture_doc, token, browser, cfg, tab
):
    """What Docs returns for a span ending exactly at a paragraph boundary.

    `select_span` asks for exactly `len(quote)` caret positions, and a newline is a
    real character in the stream — so `quote + "\\n"` is a different span from
    `quote`. Both the selection check and the readers currently strip newlines
    before comparing, which would certify the wrong one of those two as exact.

    This is the observation that decides whether that stripping is masking a real
    over-selection or papering over a harmless clipboard artefact. It reads the
    anchor raw, before anything strips it.
    """
    line = next(
        ln for ln in tab["text"].split("\n") if ln.endswith("last sentence of the document.")
    )
    page = open_doc(fixture_doc, tab["id"], port=browser)
    try:
        run = post_mod.post_many(
            page, fixture_doc, tab, [(line, "e2e paragraph-final probe")], token, cfg.strategy
        )
    finally:
        page.close()
    ids = [r.comment_id for r in run.results if r.comment_id]
    try:
        raw = [
            (c.get("quotedFileContent") or {}).get("value", "")
            for c in gdocs.list_comments(fixture_doc, token)
            if c["id"] in ids
        ]
        assert raw, "the probe did not come back from Drive"
        assert raw[0] == line, (
            "Docs returned an anchor that is not the requested span. "
            f"asked for {line[-40:]!r}, got {raw[0][-40:]!r}. If the difference is a "
            "trailing newline, the stripping in select_span and the readers is hiding "
            "a real over-selection and should be removed rather than kept."
        )
    finally:
        if ids:
            post_mod.unpost(fixture_doc, ids, token, force=True)


def test_both_readers_agree_on_the_character_stream(fixture_doc, token, browser):
    # `text` is the coordinate space. If the credential-free path disagrees with the
    # API path, spans computed from one will not select the same range in the other.
    api = gdocs.read_doc(fixture_doc, token)["tabs"][0]["text"]
    page = open_doc(fixture_doc, None, port=browser)
    try:
        docx = browser_source.read_tab(page, fixture_doc, None)["text"]
    finally:
        page.close()
    assert api == docx
