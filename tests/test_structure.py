"""The structural summary sent alongside the document text."""

from marginal import structure
from marginal.gdocs import tab_text


def para(text, style="NORMAL_TEXT", bullet=None, extra=None):
    els = (extra or []) + [{"textRun": {"content": text}}]
    p = {"elements": els, "paragraphStyle": {"namedStyleType": style}}
    if bullet is not None:
        p["bullet"] = {"nestingLevel": bullet}
    return {"paragraph": p}


def build(content, footnotes=None):
    return tab_text({
        "tabProperties": {"tabId": "t.0"},
        "documentTab": {"body": {"content": content}, "footnotes": footnotes or {}},
    })


FN = {"fn1": {"content": [{"paragraph": {"elements": [
    {"textRun": {"content": "Measured on the 2026 replication, n=12.\n"}}]}}]}}


def test_the_text_stream_is_unchanged_by_non_text_elements():
    # `text` is the coordinate space anchors resolve in. A figure or footnote marker
    # that added characters would silently move every span after it.
    plain = build([para("Throughput rose sharply.\n")])
    rich = build([para("Throughput rose sharply.\n", extra=[
        {"inlineObjectElement": {"inlineObjectId": "img1"}},
        {"footnoteReference": {"footnoteId": "fn1", "footnoteNumber": 1}}])], FN)
    assert rich["text"] == plain["text"]


def test_footnote_bodies_are_recovered():
    # They live outside body.content and used to be dropped entirely, which let the
    # model call a claim unsupported when the support was in the footnote.
    t = build([para("Throughput rose.\n", extra=[
        {"footnoteReference": {"footnoteId": "fn1", "footnoteNumber": 1}}])], FN)
    assert t["footnotes"] == [
        {"number": 1, "at": 0, "text": "Measured on the 2026 replication, n=12."}
    ]
    assert "Measured on the 2026 replication" in structure.describe(t)


def test_figures_are_announced_with_a_warning_against_false_positives():
    t = build([para("As shown.\n", extra=[
        {"inlineObjectElement": {"inlineObjectId": "img1"}}])])
    out = structure.describe(t)
    assert "1 inline image" in out and "Its content" in out
    assert "unsupported" in out


def test_headings_bullets_and_tables_are_described():
    t = build([
        para("Results\n", "HEADING_1"),
        para("One.\n", bullet=0),
        para("Two.\n", bullet=0),
        {"table": {"tableRows": [
            {"tableCells": [{"content": [para("a\n")]}, {"content": [para("b\n")]}]},
            {"tableCells": [{"content": [para("c\n")]}, {"content": [para("d\n")]}]},
        ]}},
    ])
    out = structure.describe(t)
    assert 'H1   "Results"' in out
    assert "bullet list (2 items)" in out, "consecutive bullets group into one run"
    assert "table 2 rows x 2 cols" in out


def test_a_flat_document_gets_no_summary():
    # The browser source carries no style information. Emitting a structure block
    # there would assert the document is flat, which is a claim we cannot make.
    t = build([para("Just a sentence.\n"), para("And another.\n")])
    assert structure.describe(t) == ""
    assert structure.describe({"text": "x", "paragraphs": []}) == ""


def test_separate_bullet_runs_are_not_merged():
    t = build([
        para("One.\n", bullet=0),
        para("Prose in between.\n"),
        para("Two.\n", bullet=0),
        para("Three.\n", bullet=0),
    ])
    out = structure.describe(t)
    assert "bullet list (1 item)" in out
    assert "bullet list (2 items)" in out


def test_an_overlong_heading_is_truncated():
    # A whole paragraph styled as a heading is a formatting slip; reproduced whole it
    # would crowd every other line out of the summary.
    long = "x" * 300 + "\n"
    t = build([para(long, "HEADING_1")])
    line = next(ln for ln in structure.describe(t).splitlines() if "H1" in ln)
    assert len(line) < 120
    assert '..."' in line, "the heading text itself must be elided"


# --- extraction edge cases --------------------------------------------------

def test_an_empty_document_yields_nothing():
    t = build([])
    assert t["text"] == "" and t["paragraphs"] == []
    assert structure.describe(t) == ""


def test_a_ragged_table_reports_its_widest_row():
    # Merged cells make rows uneven; cols must not be read off the first row alone.
    t = build([{"table": {"tableRows": [
        {"tableCells": [{"content": [para("a\n")]}]},
        {"tableCells": [{"content": [para("b\n")]}, {"content": [para("c\n")]},
                        {"content": [para("d\n")]}]},
    ]}}])
    assert t["tables"][0]["cols"] == 3
    assert "table 2 rows x 3 cols" in structure.describe(t)


def test_a_table_with_no_rows_does_not_divide_by_zero():
    t = build([{"table": {"tableRows": []}}])
    assert t["tables"][0] == {"start": 0, "end": 0, "rows": 0, "cols": 0}


def test_a_nested_table_is_walked_without_losing_text():
    inner = {"table": {"tableRows": [
        {"tableCells": [{"content": [para("deep\n")]}]}]}}
    t = build([{"table": {"tableRows": [
        {"tableCells": [{"content": [para("outer\n"), inner]}]}]}}])
    assert "deep" in t["text"] and "outer" in t["text"]
    assert len(t["tables"]) == 2


def test_a_footnote_reference_with_no_body_is_dropped_not_crashed():
    t = build([para("Claim.\n", extra=[
        {"footnoteReference": {"footnoteId": "missing", "footnoteNumber": 1}}])], {})
    assert t["footnotes"] == [{"number": 1, "at": 0, "text": ""}]
    assert "# Footnotes" not in structure.describe(t), "an empty note says nothing"


def test_several_figures_are_all_recorded():
    img = {"inlineObjectElement": {"inlineObjectId": "i"}}
    t = build([para("a\n", extra=[img]), para("b\n", extra=[img, img])])
    assert [f["at"] for f in t["figures"]] == [0, 2, 2]
    assert "3 inline images" in structure.describe(t) and "Their content" in structure.describe(t)


def test_a_paragraph_of_only_a_figure_still_holds_its_position():
    t = build([para("Before.\n"), {"paragraph": {"elements": [
        {"inlineObjectElement": {"inlineObjectId": "i"}}],
        "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"}}}, para("After.\n")])
    assert [f["at"] for f in t["figures"]] == [8]
    assert t["text"] == "Before.\nAfter.\n"


def test_an_equation_or_person_chip_contributes_no_characters():
    # Any element that is not a text run must not shift offsets.
    t = build([para("Cost is ", extra=None), {"paragraph": {"elements": [
        {"equation": {}}, {"person": {"personId": "p"}},
        {"textRun": {"content": "high.\n"}}],
        "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"}}}])
    assert t["text"] == "Cost is high.\n"
