from marginal import attribution


def test_header_names_the_model():
    body = attribution.apply("The dichotomy is asserted, not argued.", "claude-fable-5")
    assert body.startswith("**fable-5**\n")
    assert body.endswith("The dichotomy is asserted, not argued.")


def test_applying_twice_does_not_stack_headers():
    once = attribution.apply("point", "claude-fable-5")
    assert attribution.apply(once, "claude-fable-5") == once


def test_empty_header_is_a_no_op():
    assert attribution.apply("point", "claude-fable-5", header="") == "point"


def test_our_comments_are_recognised_by_their_header():
    assert attribution.is_ours(attribution.apply("point", "claude-fable-5"))
    assert attribution.is_ours(attribution.apply("point", "some-other-model"))


def test_human_comments_are_not_ours():
    for body in ("this is unclear", "**bold** start, no break", "", "point"):
        assert not attribution.is_ours(body)


def test_ownership_is_unknowable_without_a_header():
    # With headers disabled nothing distinguishes model from human, so the caller
    # must fall back to the ledger rather than getting a false positive.
    assert not attribution.is_ours("**fable-5**\npoint", header="")


def test_strip_recovers_what_the_model_wrote():
    assert attribution.strip(attribution.apply("point", "claude-fable-5")) == "point"
    assert attribution.strip("untouched human comment") == "untouched human comment"


def test_custom_header_round_trips():
    header = "[AI: {model}]"
    body = attribution.apply("point", "claude-opus-5", header=header)
    assert body.startswith("[AI: opus-5]")  # the vendor prefix is stripped everywhere
    assert attribution.is_ours(body, header=header)
    assert not attribution.is_ours("a human wrote this", header=header)


def test_paragraphs_from_browser_text_match_offsets():
    from marginal.browser_source import paragraphs_from_text

    text = "Title\n\nFirst body line.\nSecond.\n"
    # Not `text.split("\n")`: a terminating newline leaves a trailing empty string
    # that is the last paragraph's terminator, not a paragraph. Comparing against
    # the raw split is how the phantom span this used to produce went unnoticed —
    # both sides manufactured the same extra item and agreed with each other.
    lines = text.split("\n")[:-1]
    paras = paragraphs_from_text(text)
    # Every paragraph's slice must land where its offsets say it does.
    for line, p in zip(lines, paras, strict=True):
        assert text[p["start"] : p["end"]].rstrip("\n") == line
    assert paras[0]["start"] == 0
    assert paras[1]["start"] == len("Title\n")


def test_browser_paragraph_spans_end_where_the_text_does():
    # The disagreement that mattered: the browser source reported one more paragraph
    # than `gdocs.tab_text` does for the same document, the extra one starting at
    # `len(text)`. Two sources describing one coordinate space must agree about how
    # many paragraphs it has, or the paragraph-jump strategy counts differently
    # depending on which read the document.
    from marginal.browser_source import paragraphs_from_text

    for text in ("A\nB\n", "one\n", "a\n\nb\n"):
        paras = paragraphs_from_text(text)
        assert paras[-1]["end"] == len(text), (text, paras[-1])
        assert len(paras) == text.count("\n"), text
        assert all(b["start"] == a["end"] for a, b in zip(paras, paras[1:]))
    assert paragraphs_from_text("") == []
