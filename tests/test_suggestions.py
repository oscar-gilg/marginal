"""Reading tracked changes, and keeping the two document sources in step over them.

The docx reader has always *excluded* suggested deletions from the stream and
*included* suggested insertions (`test_tracked_changes.py` pins that). These tests
pin two additions:

- `suggestions_from_docx` reports each suggestion site with the exact characters
  it deleted and inserted, at an offset into that same stream — what verification
  of a posted suggestion matches against.
- the API reader applies the same rule. It used to count suggested-deletion runs,
  so on a document containing any suggestion the two sources disagreed about every
  offset after it — a silent wrong-anchor, the failure this tool most needs to
  avoid.
"""

from test_structure import build
from test_tracked_changes import deleted, docx, inserted, para, run

from marginal.browser_source import suggestions_from_docx, text_from_docx


# --- reading suggestion sites out of the docx --------------------------------


def test_a_replacement_is_one_site_with_both_halves():
    blob = docx(para(run("Alpha "), inserted("better "), deleted("worse "), run("omega.")))
    assert suggestions_from_docx(blob) == [
        {"deleted": "worse ", "inserted": "better ", "offset": 6}
    ]


def test_the_offset_indexes_the_live_stream():
    # Inserted text is live and counts toward later offsets; deleted text does not.
    blob = docx(
        para(run("Alpha "), inserted("new "), run("mid "), deleted("gone "), run("end.")),
    )
    text = text_from_docx(blob)
    sites = suggestions_from_docx(blob)
    assert text == "Alpha new mid end.\n"
    assert sites[0] == {"deleted": "", "inserted": "new ", "offset": 6}
    assert sites[1] == {"deleted": "gone ", "inserted": "", "offset": 14}
    assert text[sites[1]["offset"] :].startswith("end.")


def test_sites_are_split_by_live_text_and_merged_without_it():
    two = docx(para(run("A "), deleted("x "), run("B "), deleted("y "), run("C.")))
    assert len(suggestions_from_docx(two)) == 2
    one = docx(para(run("A "), deleted("x "), inserted("y "), run("B.")))
    assert len(suggestions_from_docx(one)) == 1


def test_a_site_in_a_later_paragraph_counts_the_newlines_before_it():
    blob = docx(para(run("First.")), para(run("Then "), deleted("gone."), run("kept.")))
    [site] = suggestions_from_docx(blob)
    text = text_from_docx(blob)
    assert site["offset"] == text.index("kept.")


def test_a_deleted_tab_survives_as_its_character():
    # The deleted text must be the exact characters the stream lost — a deleted tab
    # that came back as nothing would make a verified suggestion differ from its
    # quote by one character while still reading as present.
    body = (
        "<w:p>" + run("A")
        + '<w:del w:id="9" w:author="a"><w:r><w:tab/><w:delText>gone</w:delText></w:r></w:del>'
        + run("B") + "</w:p>"
    )
    [site] = suggestions_from_docx(docx(body))
    assert site["deleted"] == "\tgone"


def test_a_clean_document_has_no_sites():
    assert suggestions_from_docx(docx(para(run("Nothing here.")))) == []


# --- the API reader applies the same rule ------------------------------------


def sug_para(*runs):
    return {"paragraph": {"elements": list(runs),
                          "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"}}}


def test_the_api_stream_excludes_suggested_deletions():
    t = build([sug_para(
        {"textRun": {"content": "Keep this "}},
        {"textRun": {"content": "and drop this ", "suggestedDeletionIds": ["s.1"]}},
        {"textRun": {"content": "too.\n"}},
    )])
    assert t["text"] == "Keep this too.\n"


def test_the_api_stream_includes_suggested_insertions():
    t = build([sug_para(
        {"textRun": {"content": "Before "}},
        {"textRun": {"content": "new ", "suggestedInsertionIds": ["s.2"]}},
        {"textRun": {"content": "after.\n"}},
    )])
    assert t["text"] == "Before new after.\n"


def test_both_readers_agree_across_a_suggestion():
    # The invariant itself: same document, one suggested replacement, two readers,
    # one stream. Offsets after the suggestion must not depend on the source.
    api = build([
        sug_para({"textRun": {"content": "Alpha "}},
                 {"textRun": {"content": "better ", "suggestedInsertionIds": ["s.1"]}},
                 {"textRun": {"content": "worse ", "suggestedDeletionIds": ["s.1"]}},
                 {"textRun": {"content": "omega.\n"}}),
        sug_para({"textRun": {"content": "target sentence.\n"}}),
    ])
    browser = text_from_docx(docx(
        para(run("Alpha "), inserted("better "), deleted("worse "), run("omega.")),
        para(run("target sentence.")),
    ))
    assert api["text"] == browser
    assert api["text"].index("target sentence.") == browser.index("target sentence.")


def test_a_footnote_body_excludes_suggested_deletions():
    fn = {"fn1": {"content": [{"paragraph": {"elements": [
        {"textRun": {"content": "Kept. "}},
        {"textRun": {"content": "Dropped.", "suggestedDeletionIds": ["s.3"]}},
    ]}}]}}
    t = build([sug_para(
        {"footnoteReference": {"footnoteId": "fn1", "footnoteNumber": 1}},
        {"textRun": {"content": "Body.\n"}},
    )], fn)
    assert t["footnotes"][0]["text"] == "Kept."
