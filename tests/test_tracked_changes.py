"""Suggestions in the docx export must not shift offsets.

The credential-free reader derives the character stream from the docx. Text inside
a suggested deletion is struck through in the editor and is not part of the live
document, so counting it would displace every span after the suggestion — a silent
wrong-anchor, which is the failure this tool most needs to avoid.

Suggestions cannot be created through the Docs API (only a person in suggesting
mode makes one), so these build the export's XML directly rather than round-tripping
a real document.
"""

import zipfile
from io import BytesIO

from marginal.browser_source import text_from_docx

NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def docx(*paragraphs: str) -> bytes:
    body = "".join(paragraphs)
    xml = f'<?xml version="1.0"?><w:document {NS}><w:body>{body}</w:body></w:document>'
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", xml)
    return buf.getvalue()


def run(text):
    return f"<w:r><w:t>{text}</w:t></w:r>"


def deleted(text):
    # A suggested deletion: struck through, and OOXML stores it as delText.
    return f'<w:del w:id="1" w:author="a"><w:r><w:delText>{text}</w:delText></w:r></w:del>'


def inserted(text):
    return f'<w:ins w:id="2" w:author="a">{run(text)}</w:ins>'


def para(*bits):
    return "<w:p>" + "".join(bits) + "</w:p>"


def test_a_suggested_deletion_is_not_in_the_stream():
    out = text_from_docx(docx(para(run("Keep this "), deleted("and drop this "), run("too."))))
    assert out == "Keep this too.\n"


def test_a_suggested_insertion_is_in_the_stream():
    # Inserted text is displayed and the caret traverses it, so it must be counted.
    out = text_from_docx(docx(para(run("Before "), inserted("new "), run("after."))))
    assert out == "Before new after.\n"


def test_a_deletion_nested_inside_an_insertion_is_still_excluded():
    # The direct-child guard cannot see this one; delText is what saves it.
    nested = f'<w:ins w:id="3" w:author="a">{deleted("gone ")}{run("kept ")}</w:ins>'
    out = text_from_docx(docx(para(run("A "), nested, run("B."))))
    assert "gone" not in out
    assert out == "A kept B.\n"


def test_deltext_in_a_bare_run_is_excluded():
    bare = f"<w:r><w:delText>{'vanished '}</w:delText></w:r>"
    out = text_from_docx(docx(para(run("A "), bare, run("B."))))
    assert out == "A B.\n"


def test_offsets_after_a_suggestion_do_not_shift():
    # The whole point: a span located after a suggestion must sit where it would
    # have without one.
    clean = text_from_docx(docx(para(run("Alpha ")), para(run("target sentence."))))
    marked = text_from_docx(
        docx(para(run("Alpha "), deleted("REMOVED ")), para(run("target sentence.")))
    )
    assert clean == marked
    assert clean.index("target sentence.") == marked.index("target sentence.")
