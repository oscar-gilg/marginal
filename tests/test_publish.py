"""Publishing markdown into a Doc: the parser, the index arithmetic, the cleanup.

Every test here is offline. The seam is `gdocs` — the module that owns the HTTP —
so what the tests exercise is the whole of `publish.py` including the arithmetic,
rather than a mocked-out renderer that would prove only that the mocks were
called. `FakeDocs` below is a small model of what the Docs API does to a tab:
it keeps the character stream in UTF-16 code units, applies the requests to it,
and answers `documents.get` from the result. If the writer's predicted end-of-body
disagrees with that stream, the same failure fires here as would fire live.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from marginal import gdocs, publish

NL = "\n".encode("utf-16-le")
TAB = "\t".encode("utf-16-le")


def units(text: str) -> list:
    """`text` as a list of one-element UTF-16 code units, the way Docs counts."""
    raw = text.encode("utf-16-le")
    return [raw[i:i + 2] for i in range(0, len(raw), 2)]


def to_text(items: list) -> str:
    return b"".join(u for u in items if isinstance(u, bytes)).decode("utf-16-le")


class Image:
    """One inline image, occupying exactly one index unit."""

    def __init__(self, oid: str, uri: str) -> None:
        self.oid = oid
        self.uri = uri


class Marker:
    """A structural element of a table, occupying exactly one index unit.

    Docs' index space counts structure as well as characters: a table costs one
    index, each row inside it one, each cell one, and each cell's paragraph ends
    in its own newline. A fake that inserted only the cell text would put every
    offset after a table wrong by `1 + rows * (1 + columns)` — and since the
    writer reads its cell offsets back from the server rather than predicting
    them, a fake that got this wrong would agree with itself and prove nothing.
    """

    def __init__(self, kind: str) -> None:
        self.kind = kind

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{self.kind}>"


class FakeDocs:
    """A minimal, honest model of one tab of a Google Doc.

    Honest in the one way that matters: the index space is the UTF-16 code unit
    stream, insertions shift everything after them, and `createParagraphBullets`
    removes the leading tabs that encode nesting. Those are the three things the
    writer's arithmetic is a prediction about.
    """

    def __init__(self, doc_id="doc1", tab_id="t.1", *, rehosted=True) -> None:
        self.doc_id = doc_id
        self.tab_id = tab_id
        self.rehosted = rehosted
        self.units: list = list(units("\n"))  # a fresh tab: one empty paragraph
        self.styles: dict[str, str] = {}
        self.images: list[Image] = []
        self.batches: list[list[dict]] = []
        self.drift = 0  # extra units the "server" inserts, to force a mismatch
        self._armed: dict[str, tuple[int, Exception]] = {}

    # ---- making one call fail

    def fail_once(self, name: str, *, after: int = 0, message: str = "the API refused") -> None:
        """Arm one `GoogleApiError` from `name`, after `after` of its calls succeed.

        Drive is the part of this that fails in practice — a quota, a revoked
        scope, a permission that has already gone — and every defect below is
        about what is left behind when it does. Failing the *second* of two
        shares is the interesting case, which is why this counts rather than
        simply raising on the next call.
        """
        self._armed[name] = (after, gdocs.GoogleApiError(f"{name}: {message}"))

    def _check(self, name: str) -> None:
        armed = self._armed.get(name)
        if armed is None:
            return
        remaining, exc = armed
        if remaining:
            self._armed[name] = (remaining - 1, exc)
            return
        del self._armed[name]
        raise exc

    # ---- the endpoints publish.py calls

    def get_tabs(self, doc_id, token):
        return {"tabs": [{
            "tabProperties": {"tabId": self.tab_id, "title": "tab"},
            "documentTab": {"body": {"content": self._content()}, "inlineObjects": self._objects()},
        }]}

    def batch_update(self, doc_id, requests, token):
        self.batches.append(requests)
        for req in requests:
            self._apply(req)
        if self.drift:
            self.units[0:0] = units("x" * self.drift)
            self.drift = 0
        return {"replies": [{} for _ in requests]}

    # ---- request application

    def _apply(self, req):
        if "insertText" in req:
            r = req["insertText"]
            self.units[r["location"]["index"]:r["location"]["index"]] = units(r["text"])
        elif "insertInlineImage" in req:
            r = req["insertInlineImage"]
            img = Image(f"img{len(self.images)}", r["uri"])
            self.images.append(img)
            self.units.insert(r["location"]["index"], img)
        elif "updateParagraphStyle" in req:
            r = req["updateParagraphStyle"]
            named = r["paragraphStyle"].get("namedStyleType")
            key = self._paragraph_at(r["range"]["startIndex"])
            if named and key:
                self.styles[key] = named
        elif "createParagraphBullets" in req:
            r = req["createParagraphBullets"]
            self._debullet(r["range"]["startIndex"], r["range"]["endIndex"])
        elif "insertTable" in req:
            self._insert_table(req["insertTable"])
        # updateTextStyle changes no characters, so the index space is unmoved.

    def _insert_table(self, r: dict) -> None:
        """Append a table at the end of the segment, the way the API documents it.

        A newline goes in first — the API inserts one before a table added at the
        end of a segment — then the table element, then for each row a row
        element, and inside each row a cell element followed by the cell's own
        empty paragraph. The body's existing final paragraph stays where it is
        and becomes the paragraph after the table, because a body must end in one.
        """
        assert "endOfSegmentLocation" in r, "insertTable must target the end of the segment"
        block: list = [NL, Marker("table")]
        for _ in range(r["rows"]):
            block.append(Marker("row"))
            for _ in range(r["columns"]):
                block.extend([Marker("cell"), NL])
        at = len(self.units) - 1  # before the trailing empty paragraph's newline
        self.units[at:at] = block

    def _paragraph_at(self, index: int) -> str:
        start = index
        while start > 0 and self.units[start - 1] != NL:
            start -= 1
        end = index
        while end < len(self.units) and self.units[end] != NL:
            end += 1
        return to_text(self.units[start:end]).lstrip("\t")

    def _debullet(self, start: int, end: int) -> None:
        """Delete the leading tabs of every paragraph in the range, last first."""
        starts = [i for i in range(start, min(end, len(self.units)))
                  if i == 0 or self.units[i - 1] == NL]
        for s in reversed(starts):
            while s < len(self.units) and self.units[s] == TAB:
                del self.units[s]

    # ---- documents.get

    def _content(self):
        content, elements, pos = [], [], 0
        while pos < len(self.units):
            unit = self.units[pos]
            if isinstance(unit, Marker):
                start = pos
                pos, table = self._read_table(pos)
                content.append({"startIndex": start, "endIndex": pos, "table": table})
                continue
            pos += 1
            if isinstance(unit, Image):
                elements.append({"inlineObjectElement": {"inlineObjectId": unit.oid}})
                continue
            if unit == NL:
                text = to_text([e for e in elements if isinstance(e, bytes)])
                content.append({
                    "startIndex": pos - len(elements) - 1,
                    "endIndex": pos,
                    "paragraph": {
                        "paragraphStyle": {
                            "namedStyleType": self.styles.get(
                                to_text([e for e in elements if isinstance(e, bytes)]).lstrip("\t"),
                                "NORMAL_TEXT",
                            )
                        },
                        "elements": [e for e in elements if not isinstance(e, bytes)]
                        + [{"textRun": {"content": text}}],
                    },
                })
                elements = []
                continue
            elements.append(unit)
        return content

    def _read_table(self, pos: int):
        """Read one table out of the unit stream, reporting the offsets Docs would.

        The offset that matters is `content[0]["startIndex"]` of each cell: the
        writer inserts the cell's text there, and it is one past the cell element
        itself. Everything the writer does with a table is driven by these
        numbers rather than predicted, so this is the part that has to be right.
        """
        assert self.units[pos].kind == "table"
        pos += 1
        rows = []
        while pos < len(self.units) and isinstance(self.units[pos], Marker) \
                and self.units[pos].kind == "row":
            pos += 1
            cells = []
            while pos < len(self.units) and isinstance(self.units[pos], Marker) \
                    and self.units[pos].kind == "cell":
                cell_start = pos
                pos += 1
                para_start = pos
                while pos < len(self.units) and self.units[pos] != NL:
                    pos += 1
                pos += 1  # the newline that ends the cell's paragraph
                cells.append({
                    "startIndex": cell_start,
                    "endIndex": pos,
                    "content": [{
                        "startIndex": para_start,
                        "endIndex": pos,
                        "paragraph": {
                            "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                            "elements": [{"textRun": {
                                "content": to_text(self.units[para_start:pos])}}],
                        },
                    }],
                })
            rows.append({"tableCells": cells})
        return pos, {
            "rows": len(rows),
            "columns": len(rows[0]["tableCells"]) if rows else 0,
            "tableRows": rows,
        }

    def _objects(self):
        host = "googleusercontent.com" if self.rehosted else "drive.google.com"
        return {
            img.oid: {"inlineObjectProperties": {"embeddedObject": {
                "imageProperties": {"contentUri": f"https://lh3.{host}/{img.oid}"}}}}
            for img in self.images
        }


@pytest.fixture
def docs(monkeypatch):
    """A FakeDocs wired into every `gdocs` call `publish.py` makes."""
    fake = FakeDocs()
    drive = {"uploads": [], "shared": [], "unshared": [], "trashed": [],
             "deleted_tabs": [], "created": [], "renamed": [], "log": []}
    fake.drive = drive

    monkeypatch.setattr(gdocs, "get_tabs", fake.get_tabs)
    monkeypatch.setattr(gdocs, "batch_update", fake.batch_update)
    monkeypatch.setattr(
        gdocs, "add_tab",
        lambda doc_id, title, token: {"tabId": fake.tab_id, "title": title, "index": 1},
    )
    monkeypatch.setattr(
        gdocs, "delete_tab",
        lambda doc_id, tab_id, token: drive["deleted_tabs"].append(tab_id) or {},
    )
    monkeypatch.setattr(
        gdocs, "create_document",
        lambda title, token: drive["created"].append(title) or {
            "documentId": fake.doc_id,
            "tabs": [{"tabProperties": {"tabId": fake.tab_id}}],
        },
    )
    monkeypatch.setattr(
        gdocs, "update_tab_title",
        lambda doc_id, tab_id, title, token: drive["renamed"].append((tab_id, title)) or {},
    )
    # Written out rather than as lambdas so each Drive call can be armed to fail,
    # and so `drive["log"]` records the order across all four: unshare-before-trash
    # is an ordering property, and two lists cannot express it.
    def upload_png(path, token):
        fake._check("upload_png")
        drive["uploads"].append(str(path))
        file_id = f"file{len(drive['uploads'])}"
        drive["log"].append(("upload", file_id))
        return {"id": file_id, "name": "f.png"}

    def share_anyone(file_id, token):
        fake._check("share_anyone")
        drive["shared"].append(file_id)
        drive["log"].append(("share", file_id))
        return {"id": f"perm-{file_id}"}

    def unshare(file_id, perm, token):
        fake._check("unshare")
        drive["unshared"].append((file_id, perm))
        drive["log"].append(("unshare", file_id))

    def trash(file_id, token):
        fake._check("trash")
        drive["trashed"].append(file_id)
        drive["log"].append(("trash", file_id))
        return {}

    monkeypatch.setattr(gdocs, "upload_png", upload_png)
    monkeypatch.setattr(gdocs, "share_anyone", share_anyone)
    monkeypatch.setattr(gdocs, "unshare", unshare)
    monkeypatch.setattr(gdocs, "trash", trash)
    return fake


def md(tmp_path, text: str, name="draft.md"):
    path = tmp_path / name
    path.write_text(text)
    return path


def fake_pillow(monkeypatch, ratio=0.5):
    """Stand in for Pillow: enough to shrink nothing and report an aspect ratio."""

    class Handle:
        width, height = 100, int(100 * ratio)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class FakeImage:
        LANCZOS = 1

        @staticmethod
        def open(path):
            return Handle()

    def shrink(figures, out_dir):
        out_dir.mkdir(parents=True, exist_ok=True)
        shrunk = {}
        for number, figure in enumerate(figures, start=1):
            target = out_dir / f"figure-{number:02d}{figure.suffix or '.png'}"
            target.write_bytes(b"png")
            shrunk[figure] = publish.Figure(target, 100, int(100 * ratio))
        return shrunk

    monkeypatch.setattr(publish, "_pillow", lambda: (FakeImage, None))
    monkeypatch.setattr(publish, "shrink_figures", shrink)


# --- the markdown parser -----------------------------------------------------


def test_headings_carry_their_level_and_the_note_lands_under_the_first_h1(tmp_path):
    blocks = publish.parse_markdown(
        md(tmp_path, "# Title\n\nBody text.\n\n## Second\n\n### Third\n\n# Later\n"),
        note="rendered 07-09",
    )
    kinds = [(b["kind"], b.get("level"), b.get("text")) for b in blocks]
    assert kinds[0] == ("heading", 1, "Title")
    # Under the first level-one heading, and only that one: a version marker
    # repeated at every H1 is noise, not a marker.
    assert kinds[1] == ("note", None, "rendered 07-09")
    assert [k for k, _, _ in kinds].count("note") == 1
    assert ("heading", 2, "Second") in kinds and ("heading", 3, "Third") in kinds


def test_inline_spans_are_utf16_offsets_even_past_the_basic_plane():
    """An emoji is one Python character and two Docs indices.

    Using `len` here shifts every span after the first non-BMP character by one
    per emoji — styling that lands half a word to the left and raises nothing.
    """
    plain, spans = publish.inline_spans("🚀 **bold** and *thin* and `code`")
    assert plain == "🚀 bold and thin and code"
    # The rocket is two units, so "bold" starts at 3 in Docs' space and 2 in
    # Python's — which is exactly the bug this pins.
    assert plain.index("bold") == 2 and spans[0][0] == 3
    assert [style for _, _, style in spans] == ["bold", "italic", "code"]
    for start, end, style in spans:
        text = {"bold": "bold", "italic": "thin", "code": "code"}[style]
        assert end - start == publish.u16(text)


def test_a_link_becomes_a_span_carrying_its_url():
    plain, spans = publish.inline_spans("see [the paper](https://example.org/a) for more")
    assert plain == "see the paper for more"
    assert spans == [(4, 4 + len("the paper"), ("link", "https://example.org/a"))]


def test_an_image_line_is_not_read_as_a_link(tmp_path):
    blocks = publish.parse_markdown(md(tmp_path, "![A chart](figs/a.png)\n"))
    assert blocks == [
        {"kind": "image", "alt": "A chart", "path": "figs/a.png", "remote": False}
    ]


def test_bullets_nest_by_indent_and_numbered_lists_stay_separate(tmp_path):
    blocks = publish.parse_markdown(md(tmp_path, (
        "- top\n"
        "  - nested\n"
        "    - deeper\n"
        "\n"
        "1. first\n"
        "2. second\n"
    )))
    bullets = [(b["kind"], b["level"], b["text"]) for b in blocks]
    assert bullets == [
        ("bullet", 0, "top"),
        ("bullet", 1, "nested"),
        ("bullet", 2, "deeper"),
        ("numbered", 0, "first"),
        ("numbered", 0, "second"),
    ]


def test_a_pipe_table_drops_its_separator_row(tmp_path):
    blocks = publish.parse_markdown(md(tmp_path, (
        "| model | score |\n"
        "| --- | ---: |\n"
        "| opus | 0.81 |\n"
    )))
    assert blocks == [{"kind": "table", "rows": [["model", "score"], ["opus", "0.81"]]}]


def test_a_blockquote_splits_on_its_blank_lines(tmp_path):
    blocks = publish.parse_markdown(md(tmp_path, "> one line\n> still one\n>\n> a second\n"))
    assert [b["text"] for b in blocks] == ["one line still one", "a second"]
    assert {b["kind"] for b in blocks} == {"quote"}


# --- figure resolution -------------------------------------------------------


def _figures(source: Path) -> list[Path]:
    """The distinct local figure files `source` references, in order."""
    return list(dict.fromkeys(
        publish.figure_targets(publish.parse_markdown(source), source).values()
    ))


def test_a_figure_resolves_next_to_the_markdown_before_the_repository_root(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "figs").mkdir()
    (tmp_path / "figs" / "a.png").write_bytes(b"root copy")
    sub = tmp_path / "studies"
    sub.mkdir()
    (sub / "figs").mkdir()
    (sub / "figs" / "a.png").write_bytes(b"neighbour copy")
    source = md(sub, "![c](figs/a.png)\n")
    # The file's own directory wins: a markdown file is written to be read where
    # it sits, and the repository root is the special case.
    assert _figures(source) == [sub / "figs" / "a.png"]


def test_a_figure_only_at_the_repository_root_is_still_found(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "figs").mkdir()
    (tmp_path / "figs" / "a.png").write_bytes(b"x")
    sub = tmp_path / "studies"
    sub.mkdir()
    assert _figures(md(sub, "![c](figs/a.png)\n")) == [tmp_path / "figs" / "a.png"]


def test_a_missing_figure_names_the_reference(tmp_path):
    with pytest.raises(publish.PublishError) as e:
        _figures(md(tmp_path, "![c](figs/gone.png)\n"))
    assert "figs/gone.png" in str(e.value) and "draft.md" in str(e.value)


def test_a_remote_image_is_left_to_docs(tmp_path):
    assert _figures(md(tmp_path, "![c](https://example.org/a.png)\n")) == []


# --- Pillow is optional ------------------------------------------------------


def test_figures_without_pillow_say_which_extra_to_install(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "PIL", None)
    with pytest.raises(publish.PublishError) as e:
        publish.shrink_figures([tmp_path / "a.png"], tmp_path / "out")
    message = str(e.value)
    assert "marginal[publish]" in message
    assert "pip install" in message and "uvx --from" in message and "uv tool install" in message


def test_a_publish_with_no_figures_never_imports_pillow(tmp_path, monkeypatch, docs):
    """The whole reason Pillow is an extra: a text-only publish must not need it.

    `sys.modules["PIL"] = None` makes any import of it raise, so this passes only
    if nothing on the path touched it.
    """
    monkeypatch.setitem(sys.modules, "PIL", None)
    source = md(tmp_path, "# Title\n\nA paragraph with **bold** in it.\n\n- one\n- two\n")
    out = []
    assert publish.publish_tab("doc1", source, "v1", "", "tok", out=out.append) == 0
    assert publish.shrink_figures([], tmp_path / "out") == {}


# --- index arithmetic --------------------------------------------------------


def test_the_writer_predicts_the_end_of_body_exactly(tmp_path, docs):
    """The writer's arithmetic against a stream that shifts the way Docs' does.

    Nothing here asserts on a request payload. The assertion is that after every
    batch the writer's prediction and the simulated body agree, which is the
    property a wrong offset breaks — and it breaks it silently, by styling the
    wrong characters rather than by failing.
    """
    source = md(tmp_path, (
        "# Title\n\n"
        "A paragraph with **bold**, *italic*, `code` and a [link](https://example.org).\n\n"
        "- top\n"
        "  - nested 🚀\n"
        "- back to top\n\n"
        "1. first\n"
        "2. second\n\n"
        "- a second list\n"
        "  - also nested\n\n"
        "| model | score |\n"
        "| --- | ---: |\n"
        "| opus | 0.81 |\n"
        "| **glm** | 0.62 |\n\n"
        "> a quotation\n"
    ))
    out = []
    assert publish.publish_tab("doc1", source, "v1 — 07-09", "note", "tok", out=out.append) == 0

    text = to_text(docs.units)
    assert text.startswith("Title\nnote\n")
    assert "nested 🚀" in text and "a quotation" in text and "also nested" in text
    # Two tab-removing bullet runs in one batch, which is what forces them to be
    # applied highest index first: the first run's deletions move the second's.
    bullets = [r for batch in docs.batches for r in batch if "createParagraphBullets" in r]
    starts = [r["createParagraphBullets"]["range"]["startIndex"] for r in bullets]
    assert starts == sorted(starts, reverse=True), starts
    # The tabs that encoded nesting are gone: createParagraphBullets consumed
    # them, which is the shift the writer has to subtract from its prediction.
    assert "\t" not in text
    assert docs.styles["Title"] == "HEADING_1"
    # A table costs indices for its own structure, not only for its text, and the
    # writer reads those offsets back rather than predicting them — so the cell
    # text has to arrive in the right cells for the sync after it to line up.
    tables = [e for e in docs._content() if "table" in e]
    assert len(tables) == 1 and tables[0]["table"]["rows"] == 3
    assert tables[0]["table"]["columns"] == 2
    filled = [to_text(docs.units[c["content"][0]["startIndex"]:c["content"][0]["endIndex"]])
              for row in tables[0]["table"]["tableRows"] for c in row["tableCells"]]
    assert filled == ["model\n", "score\n", "opus\n", "0.81\n", "glm\n", "0.62\n"]
    # The tab still ends in the empty paragraph every insertion goes before.
    assert text.endswith("\n\n")


def test_index_drift_abandons_the_render_deletes_the_tab_and_exits_3(tmp_path, docs, capsys):
    """A mismatch is not recoverable, and a half-written tab must not survive it.

    Whoever the document is shared with cannot tell a truncated tab from a
    version somebody meant to publish, so the failure removes it rather than
    leaving it for inspection.
    """
    docs.drift = 3  # the server's stream ends up three units longer than predicted
    source = md(tmp_path, "# Title\n\nA paragraph.\n")
    out = []
    assert publish.publish_tab("doc1", source, "v1", "", "tok", out=out.append) == 3
    assert docs.drive["deleted_tabs"] == ["t.1"]
    err = capsys.readouterr().err
    assert "index drift" in err and "deleting tab t.1" in err


def test_a_structural_mismatch_fails_verification(tmp_path, docs, monkeypatch):
    """The counts are compared, not assumed. A lost heading exits 3."""
    monkeypatch.setattr(
        publish, "tab_stats", lambda doc, tab_id: {"headings": 0, "tables": 0, "images": 0}
    )
    source = md(tmp_path, "# Title\n\nA paragraph.\n")
    assert publish.publish_tab("doc1", source, "v1", "", "tok", out=lambda *_: None) == 3
    assert docs.drive["deleted_tabs"] == ["t.1"]


# --- creating a new document -------------------------------------------------


def test_publish_doc_creates_renames_the_default_tab_and_renders(tmp_path, docs):
    source = md(tmp_path, "# Title\n\nA paragraph.\n")
    out = []
    assert publish.publish_doc("A draft", source, "v1 — 07-09", "", "tok", out=out.append) == 0
    assert docs.drive["created"] == ["A draft"]
    # Renamed, not added to: the next version is a second tab beside this one.
    assert docs.drive["renamed"] == [("t.1", "v1 — 07-09")]
    assert "Title" in to_text(docs.units)
    assert any("https://docs.google.com/document/d/doc1/edit" == line for line in out)


def test_publish_doc_falls_back_to_a_fetch_when_create_reports_no_tabs(tmp_path, docs, monkeypatch):
    monkeypatch.setattr(gdocs, "create_document", lambda title, token: {"documentId": "doc1"})
    source = md(tmp_path, "# Title\n")
    assert publish.publish_doc("A draft", source, "v1", "", "tok", out=lambda *_: None) == 0
    assert docs.drive["renamed"] == [("t.1", "v1")]


def test_publish_doc_trashes_the_whole_document_when_verification_fails(tmp_path, docs, capsys):
    """Nothing existed before the call, so there is nothing to preserve.

    Deleting the tab instead would leave a titled, empty document on the account
    that looks like a draft somebody abandoned.
    """
    docs.drift = 2
    source = md(tmp_path, "# Title\n\nA paragraph.\n")
    assert publish.publish_doc("A draft", source, "v1", "", "tok", out=lambda *_: None) == 3
    assert docs.drive["trashed"] == ["doc1"]
    assert docs.drive["deleted_tabs"] == []
    assert "trashing the new document doc1" in capsys.readouterr().err


# --- the image lifecycle -----------------------------------------------------


def _figured(tmp_path):
    (tmp_path / "figs").mkdir()
    (tmp_path / "figs" / "a.png").write_bytes(b"png")
    return md(tmp_path, "# Title\n\n![A chart](figs/a.png)\n\nAfter the figure.\n")


def test_temporary_copies_are_unshared_and_trashed_once_docs_rehosts(tmp_path, docs, monkeypatch):
    fake_pillow(monkeypatch)
    source = _figured(tmp_path)
    out = []
    assert publish.publish_tab("doc1", source, "v1", "", "tok", out=out.append) == 0
    assert docs.drive["uploads"] and docs.drive["shared"] == ["file1"]
    # Revoked only after the contentUri proves Docs took its own copy. Earlier and
    # the tab shows a broken image; never, and a public copy of the figure stays.
    assert docs.drive["unshared"] == [("file1", "perm-file1")]
    assert docs.drive["trashed"] == ["file1"]
    assert any("re-hosted by Docs" in line for line in out)


def test_a_copy_docs_has_not_rehosted_is_left_in_place_and_named(tmp_path, docs, monkeypatch, capsys):
    fake_pillow(monkeypatch)
    docs.rehosted = False
    source = _figured(tmp_path)
    assert publish.publish_tab("doc1", source, "v1", "", "tok", out=lambda *_: None) == 0
    assert docs.drive["unshared"] == [] and docs.drive["trashed"] == []
    err = capsys.readouterr().err
    assert "LEFT IN PLACE" in err and "figs/a.png -> file1" in err


def test_a_failed_render_trashes_the_temporary_copies_too(tmp_path, docs, monkeypatch):
    fake_pillow(monkeypatch)
    docs.drift = 1
    source = _figured(tmp_path)
    assert publish.publish_tab("doc1", source, "v1", "", "tok", out=lambda *_: None) == 3
    assert docs.drive["trashed"] == ["file1"]
    assert docs.drive["deleted_tabs"] == ["t.1"]


def _two_figures(tmp_path):
    (tmp_path / "figs").mkdir()
    (tmp_path / "figs" / "a.png").write_bytes(b"png a")
    (tmp_path / "figs" / "b.png").write_bytes(b"png b")
    return md(tmp_path, "# Title\n\n![A](figs/a.png)\n\n![B](figs/b.png)\n")


def test_an_upload_that_fails_halfway_takes_back_the_copies_already_made(
        tmp_path, docs, monkeypatch):
    """The upload loop used to sit outside the rollback envelope.

    Every copy made before the failure stayed on the account, shared with anyone
    holding the link, and nothing ever went back for it: the caller deleted the
    tab and considered the wreckage cleared.
    """
    fake_pillow(monkeypatch)
    docs.fail_once("share_anyone", after=1)  # the second figure's share
    source = _two_figures(tmp_path)
    assert publish.publish_tab("doc1", source, "v1", "", "tok", out=lambda *_: None) == 3
    # The first was shared, so it is unshared and trashed; the second got as far
    # as Drive before the share failed, so it is trashed too rather than orphaned.
    assert docs.drive["unshared"] == [("file1", "perm-file1")]
    assert docs.drive["trashed"] == ["file1", "file2"]
    assert docs.drive["deleted_tabs"] == ["t.1"]


def test_cleanup_revokes_the_link_before_it_trashes_the_copy(tmp_path, docs, monkeypatch):
    """Trashing alone is not revoking.

    A file in the Drive trash is still served to anyone who has its link until it
    is purged, so a cleanup that only trashed left every figure of every failed
    publish world-readable — the state the sharing dance exists to end.
    """
    fake_pillow(monkeypatch)
    docs.drift = 1
    source = _figured(tmp_path)
    assert publish.publish_tab("doc1", source, "v1", "", "tok", out=lambda *_: None) == 3
    assert docs.drive["log"] == [
        ("upload", "file1"), ("share", "file1"), ("unshare", "file1"), ("trash", "file1"),
    ]


def test_a_remote_figure_is_handed_to_docs_as_its_own_url(tmp_path, docs, monkeypatch):
    """`figure_targets` skips remote images; the renderer used to not.

    It rebuilt a local path from the URL's last segment, uploaded whatever
    happened to be sitting there under that name, and shared it — which for a
    URL ending in a name the drafts also use locally published the wrong figure.
    """
    fake_pillow(monkeypatch)
    (tmp_path / "figs").mkdir()
    (tmp_path / "figs" / "chart.png").write_bytes(b"png")
    source = md(tmp_path, (
        "# Title\n\n"
        "![Remote](https://example.org/figs/chart.png)\n\n"
        "![Local](figs/chart.png)\n"
    ))
    assert publish.publish_tab("doc1", source, "v1", "", "tok", out=lambda *_: None) == 0
    assert len(docs.drive["uploads"]) == 1 and docs.drive["shared"] == ["file1"]
    inserts = [r["insertInlineImage"] for batch in docs.batches for r in batch
               if "insertInlineImage" in r]
    assert inserts[0]["uri"] == "https://example.org/figs/chart.png"
    # No height: the file is not here to measure, so Docs keeps its own ratio.
    assert "height" not in inserts[0]["objectSize"]
    assert inserts[1]["uri"] == "https://drive.google.com/uc?export=view&id=file1"
    assert "height" in inserts[1]["objectSize"]
    # One upload, so one release. The remote figure has nothing to release.
    assert docs.drive["trashed"] == ["file1"]


def test_a_cleanup_failure_after_verification_leaves_the_document_alone(
        tmp_path, docs, monkeypatch, capsys):
    """The publish worked. Failing to tidy up afterwards must not undo it.

    The release of the temporary uploads used to run inside the render's own
    try, so a Drive error while revoking a permission reached `publish_doc` as a
    `RenderError` and trashed a document that had already verified — losing the
    whole render to a housekeeping failure.
    """
    fake_pillow(monkeypatch)
    docs.fail_once("unshare")
    source = _figured(tmp_path)
    out = []
    assert publish.publish_doc("A draft", source, "v1", "", "tok", out=out.append) == 0
    assert docs.drive["trashed"] == []  # neither the copy nor, crucially, the document
    err = capsys.readouterr().err
    assert "LEFT IN PLACE as file1" in err
    assert any("https://docs.google.com/document/d/doc1/edit" == line for line in out)


def test_two_figures_with_the_same_basename_stay_two_figures(tmp_path, docs, monkeypatch):
    """Shrunk copies used to be named by basename, so one overwrote the other.

    `a/chart.png` and `b/chart.png` both landed on `figures/chart.png`, and the
    tab showed the second figure twice — a wrong document that verified, because
    the counts still matched.
    """
    pytest.importorskip("PIL")
    from PIL import Image as PILImage

    for name, colour in (("a", (255, 0, 0)), ("b", (0, 0, 255))):
        (tmp_path / name).mkdir()
        PILImage.new("RGB", (8, 8), colour).save(tmp_path / name / "chart.png")
    source = md(tmp_path, "# Title\n\n![A](a/chart.png)\n\n![B](b/chart.png)\n")

    uploaded = []
    inner = gdocs.upload_png

    def record(path, token):
        uploaded.append(Path(path).read_bytes())
        return inner(path, token)

    monkeypatch.setattr(gdocs, "upload_png", record)
    assert publish.publish_tab("doc1", source, "v1", "", "tok", out=lambda *_: None) == 0
    assert len(docs.drive["uploads"]) == 2
    assert len(set(docs.drive["uploads"])) == 2   # two distinct files on disk
    assert uploaded[0] != uploaded[1]             # carrying two distinct figures


def test_publish_doc_trashes_the_new_document_when_the_tab_lookup_fails(
        tmp_path, docs, monkeypatch, capsys):
    """`_default_tab` used to run before the cleanup block.

    Its fallback is a network fetch like any other, and when it failed the
    document created one line earlier was left on the account — by the very
    function whose contract is that a failed publish leaves nothing behind.
    """
    monkeypatch.setattr(gdocs, "create_document", lambda title, token: {"documentId": "doc1"})

    def refuse(doc_id, token):
        raise gdocs.GoogleApiError("GET /v1/documents/doc1: [Errno -2] Name or service not known")

    monkeypatch.setattr(gdocs, "get_tabs", refuse)
    source = md(tmp_path, "# Title\n")
    assert publish.publish_doc("A draft", source, "v1", "", "tok", out=lambda *_: None) == 3
    assert docs.drive["trashed"] == ["doc1"]
    err = capsys.readouterr().err
    assert "Name or service not known" in err and "trashing the new document doc1" in err


def test_the_shrunk_figures_are_deleted_when_the_command_returns(
        tmp_path, docs, monkeypatch):
    """One directory per publish, in the system temp, that nothing ever removed.

    Every draft's figures accumulated there for the life of the machine, at full
    shrunk size, which for a report of a dozen plots is not a rounding error.
    """
    import tempfile as tempfile_mod

    fake_pillow(monkeypatch)
    made = []
    real = tempfile_mod.mkdtemp
    monkeypatch.setattr(
        tempfile_mod, "mkdtemp",
        lambda *a, **k: made.append(real(*a, **k)) or made[-1],
    )
    source = _figured(tmp_path)
    assert publish.publish_tab("doc1", source, "v1", "", "tok", out=lambda *_: None) == 0
    assert len(made) == 1
    assert not Path(made[0]).exists()


# --- the command line --------------------------------------------------------


def test_publish_needs_exactly_one_of_doc_and_title():
    from marginal import cli

    for argv in (["publish", "d.md"], ["publish", "d.md", "--doc", "1" * 25, "--title", "t"]):
        with pytest.raises(SystemExit):
            cli._parser().parse_args(argv)


def test_publish_with_a_doc_url_needs_a_tab_title(monkeypatch, capsys):
    from marginal import cli
    from marginal import config as config_mod

    monkeypatch.setattr(cli.config_mod, "load", lambda *a, **k: config_mod.Config())
    monkeypatch.setattr(gdocs, "access_token", lambda *a, **k: "tok")
    with pytest.raises(SystemExit) as e:
        cli._main(["publish", "d.md", "--doc", "1" * 25])
    assert "--tab-title" in str(e.value)


def test_publish_accepts_a_docs_url_and_reaches_publish_tab(tmp_path, monkeypatch):
    from marginal import cli
    from marginal import config as config_mod

    seen = {}
    monkeypatch.setattr(cli.config_mod, "load", lambda *a, **k: config_mod.Config())
    monkeypatch.setattr(gdocs, "access_token", lambda *a, **k: "tok")
    monkeypatch.setattr(
        cli, "publish_tab",
        lambda doc_id, source, tab_title, note, token: seen.update(
            doc=doc_id, source=source, tab=tab_title, note=note) or 0,
    )
    url = "https://docs.google.com/document/d/ABC123abc456DEF789ghi/edit?tab=t.0"
    assert cli._main(["publish", "d.md", "--doc", url, "--tab-title", "v2", "--note", "n"]) == 0
    assert seen["doc"] == "ABC123abc456DEF789ghi"
    assert seen["tab"] == "v2" and seen["note"] == "n"


def test_publish_with_a_title_defaults_the_tab_title(monkeypatch):
    from marginal import cli
    from marginal import config as config_mod

    seen = {}
    monkeypatch.setattr(cli.config_mod, "load", lambda *a, **k: config_mod.Config())
    monkeypatch.setattr(gdocs, "access_token", lambda *a, **k: "tok")
    monkeypatch.setattr(
        cli, "publish_doc",
        lambda title, source, tab_title, note, token: seen.update(
            title=title, tab=tab_title) or 0,
    )
    assert cli._main(["publish", "d.md", "--title", "A draft"]) == 0
    assert seen["title"] == "A draft"
    assert seen["tab"] == publish.default_tab_title()
    assert seen["tab"].startswith("v1 — ")


def test_publish_turns_a_publish_error_into_one_line(monkeypatch):
    from marginal import cli
    from marginal import config as config_mod

    monkeypatch.setattr(cli.config_mod, "load", lambda *a, **k: config_mod.Config())
    monkeypatch.setattr(gdocs, "access_token", lambda *a, **k: "tok")
    with pytest.raises(SystemExit) as e:
        cli._main(["publish", "does-not-exist.md", "--title", "A draft"])
    assert str(e.value).startswith("marginal: source markdown not found")


THREADS = [{
    "id": "c1",
    "author": {"displayName": "Ada"},
    "createdTime": "2026-09-07T10:00:00Z",
    "resolved": False,
    "content": "This paragraph makes a much longer point than the summary line "
               "has room for, and the part that matters is at the very end: the "
               "denominator here is wrong.",
    "quotedFileContent": {"value": "the evaluation set was drawn from the same pool "
                                   "as the training set, which we consider acceptable"},
    "replies": [{"author": {"displayName": "Grace"}, "createdTime": "2026-09-07T11:00:00Z",
                 "content": "Agreed, and the same applies to the second table below it.",
                 "action": "resolve"}],
}]


def test_list_full_prints_the_whole_thread(monkeypatch, capsys):
    from marginal import cli
    from marginal import config as config_mod

    monkeypatch.setattr(cli.config_mod, "load", lambda *a, **k: config_mod.Config())
    monkeypatch.setattr(gdocs, "access_token", lambda *a, **k: "tok")
    monkeypatch.setattr(gdocs, "list_comments", lambda doc_id, token: THREADS)
    assert cli._main(["list", "1" * 25, "--full"]) == 0
    out = capsys.readouterr().out
    assert "1 thread(s), 1 open" in out
    assert THREADS[0]["content"] in out                      # not cut at 100
    assert THREADS[0]["quotedFileContent"]["value"] in out   # not cut at 70
    assert THREADS[0]["replies"][0]["content"] in out        # not cut at 80
    assert "(resolve)" in out and "2026-09-07T11:00:00Z" in out


def test_list_without_full_is_unchanged(monkeypatch, capsys):
    from marginal import cli
    from marginal import config as config_mod

    monkeypatch.setattr(cli.config_mod, "load", lambda *a, **k: config_mod.Config())
    monkeypatch.setattr(gdocs, "access_token", lambda *a, **k: "tok")
    monkeypatch.setattr(gdocs, "list_comments", lambda doc_id, token: THREADS)
    assert cli._main(["list", "1" * 25]) == 0
    out = capsys.readouterr().out
    assert "thread(s)" not in out
    assert THREADS[0]["content"] not in out
    assert THREADS[0]["content"][:100] in out


# --- what the live API taught (07-09) ------------------------------------------


def test_the_rename_request_uses_the_name_docs_actually_accepts(monkeypatch):
    """Pinned because the obvious name is wrong.

    `updateDocumentTab` reads like the request that renames a tab and is what a
    first draft sent; Docs answers 400 "Unknown name". The accepted request is
    `updateDocumentTabProperties`. A fake that accepts any key would let the wrong
    one back in, so this test reads the request as sent.
    """
    sent = []
    monkeypatch.setattr(gdocs, "batch_update", lambda doc_id, requests, token: sent.append(requests) or {})
    gdocs.update_tab_title("doc1", "t.0", "v1", "tok")
    assert sent == [[{"updateDocumentTabProperties": {"tabProperties": {"tabId": "t.0", "title": "v1"}, "fields": "title"}}]]


def test_a_google_error_carries_the_api_message_not_just_the_status(monkeypatch):
    import io
    import urllib.error
    import urllib.request

    body = b'{"error": {"code": 400, "message": "Unknown name \\"x\\" at requests[0]"}}'

    def refuse(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", {}, io.BytesIO(body))

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    with pytest.raises(gdocs.GoogleApiError) as e:
        gdocs.batch_update("doc1", [{"x": {}}], "tok")
    assert "HTTP 400 from POST /v1/documents/doc1:batchUpdate" in str(e.value)
    assert 'Unknown name "x"' in str(e.value)


def test_publish_doc_trashes_the_new_document_when_the_rename_is_refused(tmp_path, docs, monkeypatch, capsys):
    """The rename is the first write after create; a refusal used to leave a titled,
    empty document behind, outside the cleanup path."""

    def refuse(doc_id, tab_id, title, token):
        raise gdocs.GoogleApiError("HTTP 400 from POST /v1/documents/doc1:batchUpdate: Unknown name")

    monkeypatch.setattr(gdocs, "update_tab_title", refuse)
    source = md(tmp_path, "# Title\n")
    assert publish.publish_doc("A draft", source, "v1", "", "tok", out=lambda *_: None) == 3
    assert "doc1" in docs.drive["trashed"]
    assert "Unknown name" in capsys.readouterr().err


def test_a_connection_that_never_reached_google_still_names_the_call(monkeypatch):
    """A failure below HTTP has no status and no JSON body.

    Only `HTTPError` was wrapped, so a DNS failure or a refused connection
    escaped as a bare `URLError` naming a socket and no call — and, worse, slid
    past every `except GoogleApiError` that exists to undo a half-made publish.
    """
    import urllib.error
    import urllib.request

    def refuse(req, timeout):
        raise urllib.error.URLError("[Errno -2] Name or service not known")

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    with pytest.raises(gdocs.GoogleApiError) as e:
        gdocs.batch_update("doc1", [{"x": {}}], "tok")
    assert "POST /v1/documents/doc1:batchUpdate" in str(e.value)
    assert "Name or service not known" in str(e.value)


def test_the_upload_path_wraps_the_same_failure(tmp_path, monkeypatch):
    """`_call_bytes` is a second copy of the same three lines, and had the same gap."""
    import urllib.error
    import urllib.request

    def refuse(req, timeout):
        raise urllib.error.URLError("[Errno 111] Connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    figure = tmp_path / "a.png"
    figure.write_bytes(b"png")
    with pytest.raises(gdocs.GoogleApiError) as e:
        gdocs.upload_png(figure, "tok")
    assert "POST /upload/drive/v3/files" in str(e.value)
    assert "Connection refused" in str(e.value)


def test_a_malformed_body_is_not_dressed_up_as_a_google_error(monkeypatch):
    """Only transport failures are wrapped.

    A body that is not JSON means this code is talking to something that is not
    the Docs API — a captive portal, a proxy's error page — and reporting that as
    a `GoogleApiError` would send the reader looking for a Google-side cause.
    """
    import json
    import urllib.request

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"<html>proxy error</html>"

    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout: Response())
    with pytest.raises(json.JSONDecodeError):
        gdocs.batch_update("doc1", [{"x": {}}], "tok")
