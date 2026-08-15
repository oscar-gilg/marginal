"""The credential-free path, end to end, with no Google credentials.

`source = "browser"` exists so someone with nothing but a signed-in Chrome can
review a document. Every other end-to-end test here takes a `token`, so that tier
had no end-to-end coverage at all — the easiest way in was the least verified.

Reading only. Posting is deliberately out of scope: a posted comment is removed
with `unpost`, which writes to the comment list and has no browser route, so a
browser-only posting test would leave its comments on a real document with no way
to clean up. That asymmetry is a fact about the tool, not an oversight here.

Needs `MARGINAL_E2E=1`, a Chrome on the debug port signed into an account that can
open the document, and `MARGINAL_E2E_DOC` naming it. Skips otherwise, because
building a document would need the API these tests exist to avoid.
"""

import pytest

from marginal import browser_source
from marginal.cdp import Page

pytestmark = pytest.mark.e2e


@pytest.fixture(scope="module")
def page(pinned_doc, browser):
    p = Page.open("about:blank", port=browser)
    # Invalidated *before* the first read as well as after the last. The export cache
    # is process-wide, so an earlier test in the same session could have populated it
    # — and then "the text comes back through the browser session" would pass without
    # this page downloading anything at all.
    browser_source.invalidate(pinned_doc)
    try:
        yield p
    finally:
        p.close()
        browser_source.invalidate(pinned_doc)


def test_the_document_text_comes_back_through_the_browser_session(page, pinned_doc):
    tab = browser_source.read_tab(page, pinned_doc)
    assert tab["text"].strip(), "the export produced no text"
    assert tab["paragraphs"], "no paragraph boundaries were derived"


def test_the_text_is_a_coordinate_space_the_paragraphs_agree_with(page, pinned_doc):
    # The invariant everything downstream rests on: a paragraph's recorded offsets
    # must address that paragraph's own characters in `text`. Off by one here and
    # every anchor after it lands on the wrong sentence while still verifying.
    #
    # `text_from_docx` always terminates the text with a newline, and a span
    # includes its paragraph's terminator, so the last span ends exactly at
    # `len(text)`. Comparing against a bare `text.split("\n")` is what hid a phantom
    # trailing paragraph: both sides invented the same extra empty item and agreed.
    tab = browser_source.read_tab(page, pinned_doc)
    text, paras = tab["text"], tab["paragraphs"]
    lines = text.split("\n")[:-1] if text.endswith("\n") else text.split("\n")
    assert len(paras) == len(lines), (len(paras), len(lines))
    for para, line in zip(paras, lines, strict=True):
        assert text[para["start"]:para["end"]].rstrip("\n") == line, para
    # Contiguous, gapless, and covering the whole stream. A hole between two
    # paragraphs is a character the caret traverses that no span accounts for; a
    # span past the end is a paragraph the caret never reaches.
    assert paras[0]["start"] == 0
    assert all(b["start"] == a["end"] for a, b in zip(paras, paras[1:]))
    assert paras[-1]["end"] == len(text), (paras[-1], len(text))


def test_reading_twice_gives_the_same_stream(page, pinned_doc):
    # A quote is resolved against one read and posted against a later one, so two
    # reads of an unchanged document must agree exactly.
    #
    # The second read is forced past the cache, or this compares one set of cached
    # bytes with itself and would pass under almost any caching bug.
    first = browser_source.read_tab(page, pinned_doc)["text"]
    browser_source.invalidate(pinned_doc)
    second = browser_source.read_tab(page, pinned_doc)["text"]
    assert first == second


def test_existing_comments_and_their_anchors_are_recoverable(page, pinned_doc):
    # This is what verification uses when there is no Drive API: the docx carries
    # comment bodies and the ranges they are anchored to. A document with no
    # comments proves nothing, so that case is skipped rather than passed.
    comments = browser_source.read_comments(page, pinned_doc)
    if not comments:
        pytest.skip("the pinned document has no comments to verify against")
    assert all("body" in c and "anchored" in c for c in comments)
    anchored = [c for c in comments if (c["anchored"] or "").strip()]
    if not anchored:
        # A document can legitimately have only unanchored comments. That is a
        # weaker fixture rather than a failure of the code under test.
        pytest.skip("the pinned document has no anchored comment to verify against")
    # The anchored text is a slice of the same stream a quote resolves against. If
    # the two walks disagree about which characters count, verification passes on a
    # span that was never selected — the wrong-anchor-that-verifies failure.
    text = browser_source.read_tab(page, pinned_doc)["text"]
    missing = [c["anchored"] for c in anchored if c["anchored"] not in text]
    assert not missing, f"anchored text absent from the character stream: {missing[:3]}"
