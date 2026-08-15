"""Mapping a quotation from the Markdown back onto the document's own text."""

import pytest

from marginal import anchors
from marginal.docs_ui import SpanError

TEXT = (
    "Adaptive Difficulty in Introductory Programming Courses\n"
    "Students using the adaptive system scored 12 points higher on the final exam "
    "than the control section, a gap we consider large enough to justify rollout.\n"
    "The mechanism we think explains this is desirable difficulty: students stay "
    "near the edge of their competence.\n"
    "We measured engagement by counting problems attempted per week.\n"
)


def span_text(quote, **kw):
    span, how = anchors.resolve(TEXT, quote, **kw)
    return span.quote, how


def test_a_plain_quote_needs_no_repair():
    q = "scored 12 points higher on the final exam"
    assert span_text(q) == (q, "verbatim")


@pytest.mark.parametrize(
    "quote,expected",
    [
        ("explains this is **desirable difficulty**", "explains this is desirable difficulty"),
        ("# Adaptive Difficulty in Introductory", "Adaptive Difficulty in Introductory"),
        ("- We measured engagement by counting", "We measured engagement by counting"),
        ("justify rollout.[^1]", "justify rollout."),
        ("problems attempted per week\\.", "problems attempted per week."),
        ("_desirable difficulty_: students stay", "desirable difficulty: students stay"),
    ],
)
def test_markdown_syntax_is_stripped(quote, expected):
    got, how = span_text(quote)
    assert got == expected and how == "stripped"


def test_a_quote_rewrapped_by_markdown_still_matches():
    # Markdown wraps on its own terms; a newline in the export is a space in the doc.
    got, how = span_text("We measured engagement by counting\n   problems attempted")
    assert got == "We measured engagement by counting problems attempted"
    assert how == "whitespace"


def test_a_mistyped_quote_is_recovered_without_a_model_call():
    # One wrong character mid-quote splits the longest matching run in half. Spanning
    # first-to-last matching block instead keeps this off the model.
    got, how = span_text("scored 12 points higher on the fina1 exam than the control")
    assert "scored 12 points higher on the final exam" in got
    assert how == "fuzzy"


def test_an_ambiguous_quote_is_refused_rather_than_guessed():
    text = "the same words here. and the same words here.\n"
    with pytest.raises(SpanError):
        anchors.resolve(text, "the same words here")


def test_text_absent_from_the_document_is_unplaceable():
    # Footnote bodies and table cells have no counterpart in the character stream.
    with pytest.raises(SpanError):
        anchors.resolve(TEXT, "per-student baselines are in Table 2")


def test_the_model_is_only_asked_after_every_deterministic_rung():
    calls = []

    def recon(text, quote):
        calls.append(quote)
        return "desirable difficulty"

    span, how = anchors.resolve(TEXT, "a passage that is nowhere near this text at all",
                                reconcile=recon)
    assert how == "model" and span.quote == "desirable difficulty"
    assert len(calls) == 1
    # ...and never for a quote the string work already placed.
    anchors.resolve(TEXT, "**desirable difficulty**", reconcile=recon)
    assert len(calls) == 1


def test_a_model_answer_that_does_not_resolve_is_rejected():
    # Trusting it blind would anchor the comment somewhere it was never asked to.
    with pytest.raises(SpanError):
        anchors.resolve(TEXT, "nowhere near this text",
                        reconcile=lambda t, q: "text that is not in the document")


def test_the_returned_span_is_a_slice_of_the_document():
    for q in ("**desirable difficulty**", "# Adaptive Difficulty in Introductory",
              "scored 12 points higher on the fina1 exam than the control"):
        span, _ = anchors.resolve(TEXT, q)
        assert TEXT[span.start:span.end] == span.quote, "selection navigates by offset"


# --- edge cases -------------------------------------------------------------

EDGE = (
    "The estimate is noisy; we expect it to settle.\n"
    "Costs fell 20% year over year (see appendix).\n"
    "He said “the result is robust” in the review.\n"
    "Use the --strict flag when parsing input.\n"
    # Docs stores list numbering as formatting, so the digits are not in the
    # character stream even though the Markdown export writes them.
    "First step\nSecond step\n"
)


@pytest.mark.parametrize("quote", ["", "   ", "**", "*_`*", "[^1]", "#"])
def test_a_quote_with_no_words_is_refused(quote):
    # Stripping syntax can empty a quote; an empty pattern must not match position 0.
    with pytest.raises(SpanError):
        anchors.resolve(EDGE, quote)


def test_nested_emphasis_is_stripped():
    span, how = anchors.resolve(EDGE, "***the result is robust***")
    assert span.quote == "the result is robust"


def test_inline_code_markers_are_stripped():
    span, _ = anchors.resolve(EDGE, "Use the `--strict` flag when parsing")
    assert span.quote == "Use the --strict flag when parsing"


def test_an_ordered_list_marker_is_stripped():
    span, _ = anchors.resolve(EDGE, "2. Second step")
    assert span.quote == "Second step"


def test_a_blockquote_marker_is_stripped():
    span, _ = anchors.resolve(EDGE, "> Costs fell 20% year over year")
    assert span.quote == "Costs fell 20% year over year"


def test_a_markdown_link_keeps_its_text():
    span, _ = anchors.resolve(EDGE, "[Costs fell 20%](http://example.com/a_b) year")
    assert span.quote == "Costs fell 20% year"


def test_curly_quotes_survive_unchanged():
    # Docs substitutes typographic quotes; mangling them would break the match.
    span, _ = anchors.resolve(EDGE, "“the result is robust” in the review")
    assert "“" in span.quote


def test_regex_metacharacters_in_a_quote_are_literal():
    # The whitespace rung builds a pattern from the quote; unescaped this would throw.
    span, _ = anchors.resolve(EDGE, "Costs fell 20% year over year (see appendix).")
    assert span.quote.endswith("(see appendix).")


def test_a_quote_that_is_the_whole_document_resolves():
    span, _ = anchors.resolve(EDGE, EDGE.strip())
    assert span.start == 0


def test_a_span_is_never_returned_from_a_failed_model_answer():
    for answer in (None, "", "   ", "text absent from the document"):
        with pytest.raises(SpanError):
            anchors.resolve(EDGE, "nowhere near any of this",
                            reconcile=lambda t, q, a=answer: a)


def test_to_plain_is_idempotent():
    once = anchors.to_plain("## **Bold heading** with `code`[^2]")
    assert anchors.to_plain(once) == once


def test_the_rung_that_placed_each_quote_is_reported():
    # The model rung is only healthy while it is rare, and a rising share means
    # something upstream changed. Without this it looks like everything working.
    from marginal import config as config_mod
    from marginal.submission import Submitter

    def summary(rungs):
        sub = Submitter("text", config_mod.Config(), "m")
        sub.rungs = list(rungs)
        return sub.summary()

    assert summary(["verbatim", "verbatim", "model"]) == "2 verbatim, 1 model"
    assert summary([]) == "none placed"
    # ordered cheapest first, whatever order they arrived in
    assert summary(["model", "verbatim"]) == "1 verbatim, 1 model"
