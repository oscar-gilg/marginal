"""The docx export's character stream, and the anchors read out of the same file.

Both are slices of one coordinate space. Every case here is a character the editor
shows and the caret crosses: if the export contributes a different number of them
than the Docs API does, every offset after it moves, and a comment lands on the
wrong words while still verifying as exact.
"""

import zipfile
from io import BytesIO

import pytest

from marginal import browser_source
from marginal.browser_source import LINE_BREAK, read_comments, text_from_docx

NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def docx(*paragraphs: str, comments: str = "") -> bytes:
    body = "".join(paragraphs)
    doc = f'<?xml version="1.0"?><w:document {NS}><w:body>{body}</w:body></w:document>'
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", doc)
        if comments:
            z.writestr(
                "word/comments.xml",
                f'<?xml version="1.0"?><w:comments {NS}>{comments}</w:comments>',
            )
    return buf.getvalue()


def run(text):
    return f"<w:r><w:t>{text}</w:t></w:r>"


def para(*bits):
    return "<w:p>" + "".join(bits) + "</w:p>"


TAB = "<w:r><w:tab/></w:r>"
BR = "<w:r><w:br/></w:r>"
CR = "<w:r><w:cr/></w:r>"
DELETED = '<w:del w:id="9" w:author="a"><w:r><w:delText>gone </w:delText></w:r></w:del>'


def comment(cid, text):
    return f'<w:comment w:id="{cid}">{para(run(text))}</w:comment>'


def start(cid):
    return f'<w:commentRangeStart w:id="{cid}"/>'


def end(cid):
    return f'<w:commentRangeEnd w:id="{cid}"/>'


@pytest.fixture
def comments_of(monkeypatch):
    """Read comments straight from export bytes, with no browser in the way."""

    def read(blob: bytes):
        monkeypatch.setattr(browser_source, "export", lambda *a, **k: blob)
        return read_comments(page=None, doc_id="doc")

    return read


class TestDocumentStream:
    def test_a_tab_is_one_character(self):
        # It was dropped, so every offset after it was short by one and every
        # selection in the rest of the paragraph landed a character early.
        assert text_from_docx(docx(para(run("A"), TAB, run("B")))) == "A\tB\n"

    def test_a_line_break_is_one_character(self):
        assert text_from_docx(docx(para(run("A"), BR, run("B")))) == f"A{LINE_BREAK}B\n"

    def test_a_break_does_not_split_the_paragraph(self):
        # Shift+Enter stays one paragraph; treating it as a split would renumber
        # every paragraph after it for the paragraph-jump strategy.
        out = text_from_docx(docx(para(run("A"), BR, run("B")), para(run("C"))))
        assert out.count("\n") == 2
        assert out.split("\n")[0] == f"A{LINE_BREAK}B"

    def test_a_carriage_return_is_one_character(self):
        # `w:cr` is the other spelling of a soft break and contributes the same
        # single character as `w:br`. Untested until both walks were folded onto one
        # mapping, which is exactly the sort of element that used to be added to one
        # walk and forgotten in the other.
        assert text_from_docx(docx(para(run("A"), CR, run("B")))) == f"A{LINE_BREAK}B\n"

    def test_a_suggested_deletion_is_still_excluded(self):
        assert text_from_docx(docx(para(run("keep "), DELETED, run("this")))) == "keep this\n"


class TestAnchors:
    def test_an_anchor_carries_the_tab_it_spans(self, comments_of):
        blob = docx(
            para(start(1), run("before"), TAB, run("after"), end(1)),
            comments=comment(1, "note"),
        )
        assert comments_of(blob) == [{"body": "note", "anchored": "before\tafter"}]

    def test_an_anchor_carries_the_carriage_return_it_spans(self, comments_of):
        # The same character the document stream contributes, so the anchor stays a
        # slice of it. The two walks share one mapping; this pins that they agree.
        blob = docx(
            para(start(1), run("before"), CR, run("after"), end(1)),
            comments=comment(1, "note"),
        )
        assert comments_of(blob) == [
            {"body": "note", "anchored": f"before{LINE_BREAK}after"}
        ]

    def test_an_anchor_excludes_a_suggested_deletion(self, comments_of):
        # The document stream skips struck-through text. An anchor that counted it
        # would describe a span the coordinate space does not contain — and this walk
        # used to count it while `text_from_docx` did not.
        blob = docx(
            para(start(1), run("keep "), DELETED, run("this"), end(1)),
            comments=comment(1, "note"),
        )
        assert comments_of(blob)[0]["anchored"] == "keep this"

    def test_an_anchor_across_paragraphs_keeps_the_boundary(self, comments_of):
        blob = docx(
            para(start(1), run("first")),
            para(run("second"), end(1)),
            comments=comment(1, "note"),
        )
        assert comments_of(blob)[0]["anchored"] == "first\nsecond"

    def test_the_anchor_is_a_slice_of_the_document_stream(self, comments_of):
        # The property that matters, stated directly: whatever the anchor says, the
        # document contains those characters, in that order, once.
        blob = docx(
            para(run("lead in ")),
            para(run("x"), TAB, start(1), run("anchored"), BR, run("tail"), end(1)),
            comments=comment(1, "note"),
        )
        assert comments_of(blob)[0]["anchored"] in text_from_docx(blob)
