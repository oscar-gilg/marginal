from marginal.docs_ui import SpanError, paragraph_of, resolve_quote

TEXT = "Alpha beta gamma.\nDelta epsilon beta.\nZeta eta theta.\n"
PARAS = [
    {"start": 0, "end": 18},
    {"start": 18, "end": 38},
    {"start": 38, "end": 54},
]


def test_unique_quote_resolves_to_its_offsets():
    span = resolve_quote(TEXT, "Delta epsilon")
    assert (span.start, span.end) == (18, 31)
    assert TEXT[span.start : span.end] == "Delta epsilon"


def test_ambiguous_quote_fails_loudly():
    try:
        resolve_quote(TEXT, "beta")
    except SpanError as e:
        assert "2 times" in str(e)
    else:
        raise AssertionError("ambiguous quote should not resolve")


def test_missing_quote_fails_loudly():
    try:
        resolve_quote(TEXT, "not in the document")
    except SpanError:
        pass
    else:
        raise AssertionError("absent quote should not resolve")


def test_paragraph_lookup_picks_the_containing_paragraph():
    assert paragraph_of(PARAS, 0) == (0, 0)
    assert paragraph_of(PARAS, 20) == (1, 18)
    assert paragraph_of(PARAS, 53) == (2, 38)


def test_paragraph_lookup_clamps_past_the_end():
    assert paragraph_of(PARAS, 9999) == (2, 38)
    assert paragraph_of([], 5) == (0, 0)
