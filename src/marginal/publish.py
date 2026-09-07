"""Render a markdown file into a Google Doc — as a new tab, or as a new document.

This is the other direction from the rest of Marginal: instead of reading a
document and leaving comments in it, it writes one. It exists for the same
working loop the commenting does. A draft lives as markdown in a repository, the
people reviewing it live in Google Docs, and the version they comment on has to
be a real Doc — headings that fold, tables that are tables, figures that render —
or the comments come back about the formatting.

Three decisions shape everything here.

**A tab per version.** `publish_tab` appends a tab to a document that already
exists rather than creating a new file. Comments are attached to a document, so a
new file per revision scatters the review across a pile of orphaned drafts;
a new tab leaves every past round of comments exactly where its author left it,
next to the text it was about. `publish_doc` is the first-time case: it creates
the document, renames its single default tab, and then follows the identical
path — `_render_into` — so the two entry points cannot drift into rendering
markdown differently.

**Index arithmetic is predicted, then checked.** The Docs API addresses content
by offset into a tab, in UTF-16 code units, and every insertion moves every
offset after it. `TabWriter` computes where the body will end after a batch and
compares that against what the server reports. Silent drift is the failure this
guards: one wrong offset does not raise, it writes the next paragraph's styling
across the tail of the previous one, and the tab looks *nearly* right. On a
mismatch the whole render is abandoned and the tab (or, for a brand-new
document, the document) is deleted, because a half-written version people can
open is worse than no version at all.

**Images are borrowed, not given.** `insertInlineImage` takes a URI that Google's
own servers fetch, so each local figure is uploaded to Drive and shared publicly
for the length of the render; a figure that is already a URL is passed straight
through, because Docs can fetch that itself and a copy on our Drive would be
republishing somebody else's image. Docs then re-hosts what it fetched under
googleusercontent.com. Only once every `contentUri` in the finished tab is a
googleusercontent one — proof that Docs took its own copy — is the temporary file
unshared and trashed, in that order: a trashed Drive file is still served to
anyone holding its link. Revoke earlier and the tab shows broken images; skip the
check and a world-readable copy of every figure is left on the account, so when
the proof does not arrive the files are left in place and named on stderr rather
than quietly abandoned. Handing the copies back is the last step and never the
fatal one — the document is already verified by then, and a Drive error while
tidying up must not undo a publish that worked.

Pillow is an optional dependency and is imported only when the markdown actually
references a figure; a text-only publish works on a bare install.
"""

from __future__ import annotations

import datetime
import re
import shutil
import sys
import tempfile
from pathlib import Path

from . import gdocs

IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
INLINE_RE = re.compile(r"(\*\*.+?\*\*|\*.+?\*|`[^`]+`)")
# [text](url) — but not ![alt](path), which IMAGE_RE claims first.
LINK_RE = re.compile(r"(?<!!)\[([^\]]+)\]\((https?://[^)\s]+)\)")
BULLET_RE = re.compile(r"^(\s*)- (.*)$")
NUMBER_RE = re.compile(r"^(\d+)\. (.*)$")

MAX_FIGURE_WIDTH = 1100
QUANTIZE_COLORS = 128
# Hairline black frame around every figure, in pixels at the shrunk size. At the
# 6.4 in placement width a 1100 px figure renders ~2.4 px per point, so one pixel
# is a ~0.4 pt rule: the thin border the drafts are meant to carry.
FIGURE_BORDER_PX = 1
FIGURE_BORDER_COLOR = (0, 0, 0)
FIGURE_WIDTH_PT = 6.4 * 72  # 6.4 in
QUOTE_INDENT_PT = 36.0
# Text-style fields reset on every paragraph before inline styles are applied,
# because insertText inherits the styling of the text it is inserted next to.
RESET_TEXT_FIELDS = "bold,italic,underline,strikethrough,weightedFontFamily,fontSize,foregroundColor"

BULLET_PRESET = "BULLET_DISC_CIRCLE_SQUARE"
NUMBER_PRESET = "NUMBERED_DECIMAL_ALPHA_ROMAN"

# Named here rather than written into the raise, because the test that proves a
# figureless publish never imports PIL and the test that proves a figured one
# explains itself should be checking the same string.
PIL_MISSING = (
    "this markdown references figures, and Pillow is not installed. Marginal keeps "
    "one dependency; figure shrinking is the optional extra. Install it with one of:\n"
    "  pip install 'marginal[publish]'\n"
    "  uvx --from 'marginal[publish]' marginal ...\n"
    "  uv tool install 'marginal[publish]'"
)


class PublishError(RuntimeError):
    """Anything that should reach the user as one line, not a traceback."""


class RenderError(PublishError):
    """The render or its verification failed, so what was written must be undone.

    A subclass because the two are handled differently by exactly one caller: a
    `PublishError` raised before anything was created (a missing figure, no
    Pillow) leaves nothing behind, while this one means a tab or a document
    exists and has to be removed before the command can exit.
    """


# --- figures -----------------------------------------------------------------


def repo_root(source: Path) -> Path:
    """The nearest ancestor holding a `.git`, or the file's own directory."""
    here = source.resolve().parent
    for directory in [here, *here.parents]:
        if (directory / ".git").exists():
            return directory
    return here


def figure_targets(source: Path) -> dict[str, Path]:
    """Every local figure reference, keyed by the target exactly as written.

    Relative paths resolve against the markdown file's own directory first and
    the enclosing repository root second. That order is the general one: a
    markdown file is usually written to be read where it sits, and a path that
    only works from a repository root is the special case, not the default.
    Remote images are left out entirely — Docs fetches those itself.

    Keyed by the written target rather than returning bare paths because that
    string is what the renderer later has to find a shrunk copy by. Resolving it
    a second time at render time, from the basename, is how `a/chart.png` and
    `b/chart.png` became one figure repeated twice in the tab.
    """
    root = repo_root(source)
    found: dict[str, Path] = {}
    for _, target in IMAGE_RE.findall(source.read_text()):
        if target.startswith(("http://", "https://")) or target in found:
            continue
        if Path(target).is_absolute():
            candidate = Path(target)
        else:
            candidate = (source.parent / target).resolve()
            if not candidate.exists():
                candidate = root / target
        if not candidate.exists():
            raise PublishError(
                f"figure referenced by {source.name} not found: {target}\n"
                f"  looked in {source.parent} and {root}"
            )
        found[target] = candidate
    return found


def figure_paths(source: Path) -> list[Path]:
    """Every distinct local figure file the markdown references, in order."""
    return list(dict.fromkeys(figure_targets(source).values()))


def _pillow():
    """Import Pillow, or explain how to get it. Never called without a figure."""
    try:
        from PIL import Image, ImageOps
    except Exception as exc:  # ImportError, or a sys.modules entry set to None
        raise PublishError(PIL_MISSING) from exc
    return Image, ImageOps


def shrink_figures(figures: list[Path], out_dir: Path) -> tuple[int, dict[Path, Path]]:
    """Downscale, frame and quantize each figure into `out_dir`.

    Returns the total bytes written and a {source file: shrunk copy} mapping.
    The copies are named by their position in the list rather than by their
    original basename: two figures called `chart.png`, in different directories,
    both used to be written to `out_dir/chart.png`, so the second silently
    overwrote the first and the finished tab showed one of them twice. The
    mapping is returned rather than reconstructed later for the same reason —
    a basename is not an identity.

    Returns before touching Pillow when there is nothing to shrink, which is what
    makes the dependency genuinely optional rather than optional-until-you-run-it.
    """
    if not figures:
        return 0, {}
    Image, ImageOps = _pillow()

    out_dir.mkdir(parents=True, exist_ok=True)
    total = 0
    # The frame is added to the pixels, so the resize aims at the inner box and
    # the framed figure still lands on MAX_FIGURE_WIDTH.
    inner_width = MAX_FIGURE_WIDTH - 2 * FIGURE_BORDER_PX
    shrunk: dict[Path, Path] = {}
    for number, figure in enumerate(figures, start=1):
        image = Image.open(figure)
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        if image.width > inner_width:
            height = round(image.height * inner_width / image.width)
            image = image.resize((inner_width, height), Image.LANCZOS)
        fill = FIGURE_BORDER_COLOR if image.mode == "RGB" else 0
        image = ImageOps.expand(image, border=FIGURE_BORDER_PX, fill=fill)
        image = image.quantize(colors=QUANTIZE_COLORS)
        target = out_dir / f"figure-{number:02d}{figure.suffix or '.png'}"
        image.save(target, optimize=True)
        total += target.stat().st_size
        shrunk[figure] = target
    return total, shrunk


# --- markdown ----------------------------------------------------------------


def u16(text: str) -> int:
    """Length in UTF-16 code units, which is how Docs indexes content.

    Not `len`. A non-BMP character — an emoji, a rarer CJK glyph — is one Python
    character and two Docs indices, so a paragraph containing one is two units
    longer than it looks and every offset computed after it is wrong by two.
    """
    return len(text.encode("utf-16-le")) // 2


def inline_spans(text: str) -> tuple[str, list[tuple[int, int, object]]]:
    """Strip inline markers, returning plain text and (start, end, style) spans.

    Offsets are UTF-16 code units, to be added to the index the paragraph starts
    at. A style is either the string "bold"/"italic"/"code" or the tuple
    ("link", url) for markdown links, rendered as real Docs hyperlinks.
    """
    plain = ""
    spans: list[tuple[int, int, object]] = []

    def add_styled(chunk: str) -> None:
        nonlocal plain
        for part in INLINE_RE.split(chunk):
            if not part:
                continue
            if part.startswith("**") and part.endswith("**") and len(part) > 4:
                content, style = part[2:-2], "bold"
            elif part.startswith("`") and part.endswith("`") and len(part) > 2:
                content, style = part[1:-1], "code"
            elif part.startswith("*") and part.endswith("*") and len(part) > 2:
                content, style = part[1:-1], "italic"
            else:
                content, style = part, None
            start = u16(plain)
            plain += content
            if style:
                spans.append((start, u16(plain), style))

    cursor = 0
    for match in LINK_RE.finditer(text):
        add_styled(text[cursor:match.start()])
        start = u16(plain)
        plain += match.group(1)
        spans.append((start, u16(plain), ("link", match.group(2))))
        cursor = match.end()
    add_styled(text[cursor:])
    return plain, spans


def parse_markdown(source: Path, note: str = "") -> list[dict]:
    """Parse the markdown subset drafts use into a flat list of blocks.

    Deliberately small: headings, paragraphs, bullets, numbered lists, pipe
    tables, blockquotes and images, with inline bold/italic/code/links. A block
    kind this does not produce is one the renderer does not have to handle, and
    the count of headings, tables and images it produces is what the finished tab
    is verified against.

    `note` becomes an italic line under the first level-one heading — where a
    version marker belongs, so a reader who opens the wrong tab can see it.
    """
    lines = source.read_text().splitlines()
    blocks: list[dict] = []
    index = 0
    first_h1 = True
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()

        image = IMAGE_RE.fullmatch(stripped)
        if image:
            blocks.append({"kind": "image", "alt": image.group(1), "path": image.group(2)})
            index += 1
            continue

        heading = re.match(r"^(#{1,3}) (.*)$", line)
        if heading:
            level = len(heading.group(1))
            blocks.append({"kind": "heading", "level": level, "text": heading.group(2).strip()})
            if level == 1 and first_h1 and note:
                blocks.append({"kind": "note", "text": note})
            if level == 1:
                first_h1 = False
            index += 1
            continue

        if stripped == "---":
            index += 1
            continue

        if stripped.startswith("|") and stripped.endswith("|"):
            rows: list[list[str]] = []
            while index < len(lines) and lines[index].strip().startswith("|"):
                cells = [c.strip() for c in lines[index].strip().strip("|").split("|")]
                # The `|---|---|` separator row describes the table rather than
                # belonging to it.
                if not all(re.fullmatch(r":?-{3,}:?", c) for c in cells):
                    rows.append(cells)
                index += 1
            if rows:
                blocks.append({"kind": "table", "rows": rows})
            continue

        if stripped.startswith(">"):
            chunks: list[str] = []
            while index < len(lines) and lines[index].strip().startswith(">"):
                text = lines[index].strip()[1:].lstrip()
                # A blank quote line is a paragraph break inside the quote; NUL
                # marks it so the join below can split the quote back apart.
                chunks.append(text if text else "\x00")
                index += 1
            for chunk in " ".join(chunks).split("\x00"):
                if chunk.strip():
                    blocks.append({"kind": "quote", "text": chunk.strip()})
            continue

        numbered = NUMBER_RE.match(line)
        if numbered:
            content = [numbered.group(2)]
            index += 1
            while index < len(lines):
                nxt = lines[index]
                if (nxt.strip() and not NUMBER_RE.match(nxt)
                        and not nxt.startswith(("#", ">", "|"))
                        and not nxt.strip().startswith(("![", "- "))
                        and nxt.startswith("   ")):
                    content.append(nxt.strip())
                    index += 1
                else:
                    break
            blocks.append({"kind": "numbered", "level": 0, "text": " ".join(content)})
            continue

        bullet = BULLET_RE.match(line)
        if bullet:
            indent, content = len(bullet.group(1)), [bullet.group(2)]
            index += 1
            while index < len(lines):
                nxt = lines[index]
                if (nxt.strip() and not BULLET_RE.match(nxt)
                        and not nxt.startswith("#") and nxt.strip() != "---"
                        and not nxt.strip().startswith("![")
                        and (len(nxt) - len(nxt.lstrip())) > indent):
                    content.append(nxt.strip())
                    index += 1
                else:
                    break
            blocks.append({"kind": "bullet", "level": min(indent // 2, 2), "text": " ".join(content)})
            continue

        if stripped:
            content = [stripped]
            index += 1
            while index < len(lines) and lines[index].strip() and not re.match(
                r"^(\s*- |#|---|!\[|>|\||\d+\. )", lines[index].strip()
            ) and not lines[index].startswith("#"):
                content.append(lines[index].strip())
                index += 1
            blocks.append({"kind": "para", "text": " ".join(content)})
            continue

        index += 1
    return blocks


# --- reading a tab back ------------------------------------------------------


def tab_body(doc: dict, tab_id: str) -> list[dict]:
    for tab, _ in gdocs.walk_tabs(doc.get("tabs") or []):
        if (tab.get("tabProperties") or {}).get("tabId") == tab_id:
            return ((tab.get("documentTab") or {}).get("body") or {}).get("content") or []
    raise RenderError(f"tab {tab_id} not found in document")


def body_end(doc: dict, tab_id: str) -> int:
    content = tab_body(doc, tab_id)
    if not content:
        raise RenderError(f"tab {tab_id} has an empty body")
    return content[-1]["endIndex"]


def tab_stats(doc: dict, tab_id: str) -> dict:
    """Count headings, tables and inline images actually present in the tab.

    Compared against the same counts taken from the parsed markdown. It is a
    coarse check on purpose: it cannot tell that a paragraph landed in the wrong
    order, but it catches every failure mode that loses or duplicates structure,
    and it costs one read.
    """
    content = tab_body(doc, tab_id)
    stats = {"headings": 0, "tables": 0, "images": 0}

    def count_paragraph(paragraph: dict) -> None:
        style = (paragraph.get("paragraphStyle") or {}).get("namedStyleType", "")
        if style.startswith("HEADING_"):
            stats["headings"] += 1
        for element in paragraph.get("elements") or []:
            if "inlineObjectElement" in element:
                stats["images"] += 1

    def walk(elements: list[dict]) -> None:
        for element in elements:
            if "paragraph" in element:
                count_paragraph(element["paragraph"])
            elif "table" in element:
                stats["tables"] += 1
                for row in element["table"]["tableRows"]:
                    for cell in row["tableCells"]:
                        walk(cell["content"])

    walk(content)
    return stats


def image_content_uris(doc: dict, tab_id: str) -> list[str]:
    """Where each inline image in the tab is served from, as Docs reports it."""
    for tab, _ in gdocs.walk_tabs(doc.get("tabs") or []):
        if (tab.get("tabProperties") or {}).get("tabId") == tab_id:
            objects = (tab.get("documentTab") or {}).get("inlineObjects") or {}
            return [
                ((obj.get("inlineObjectProperties") or {}).get("embeddedObject") or {})
                .get("imageProperties", {})
                .get("contentUri", "")
                for obj in objects.values()
            ]
    return []


# --- the writer --------------------------------------------------------------


class TabWriter:
    """Appends rendered markdown blocks to one tab, tracking indices exactly.

    The invariant across every flush is that the tab body ends with an empty
    paragraph and `self.pos` is the index of its terminating newline, which is
    where the next insertion goes. Index arithmetic inside a batch is computed
    analytically and then checked against the server after the batch, so drift
    fails loudly instead of producing a scrambled tab.
    """

    def __init__(self, doc_id: str, tab_id: str, token: str) -> None:
        self.doc_id = doc_id
        self.tab_id = tab_id
        self.token = token
        self.reqs: list[dict] = []
        self.bullets: list[dict] = []
        self.removals = 0  # leading tabs createParagraphBullets will delete
        self.pos = 0
        self.sync()

    # ---- primitives

    def loc(self, index: int) -> dict:
        return {"index": index, "tabId": self.tab_id}

    def rng(self, start: int, end: int) -> dict:
        return {"startIndex": start, "endIndex": end, "tabId": self.tab_id}

    def sync(self) -> int:
        doc = gdocs.get_tabs(self.doc_id, self.token)
        self.pos = body_end(doc, self.tab_id) - 1
        return self.pos

    def flush(self) -> None:
        if not self.reqs and not self.bullets:
            return
        # Bullets run last, highest index first: createParagraphBullets deletes
        # the leading tabs that encode nesting, which shifts everything after.
        batch = self.reqs + list(reversed(self.bullets))
        expected = self.pos - self.removals
        self.reqs, self.bullets, self.removals = [], [], 0
        gdocs.batch_update(self.doc_id, batch, self.token)
        actual = self.sync()
        if actual != expected:
            raise RenderError(
                f"index drift after batch: expected end-of-body {expected}, server says {actual}"
            )

    def style_spans(self, start: int, spans, offset: int = 0) -> None:
        for span_start, span_end, style in spans:
            begin = start + offset + span_start
            end = start + offset + span_end
            if end <= begin:
                continue
            if isinstance(style, tuple) and style[0] == "link":
                text_style = {
                    "link": {"url": style[1]},
                    "underline": True,
                    "foregroundColor": {"color": {"rgbColor": {
                        "red": 0x11 / 255, "green": 0x55 / 255, "blue": 0xCC / 255}}},
                }
                fields = "link,underline,foregroundColor"
            elif style == "bold":
                text_style, fields = {"bold": True}, "bold"
            elif style == "italic":
                text_style, fields = {"italic": True}, "italic"
            else:
                text_style = {
                    "weightedFontFamily": {"fontFamily": "Courier New"},
                    "fontSize": {"magnitude": 10, "unit": "PT"},
                }
                fields = "weightedFontFamily,fontSize"
            self.reqs.append({
                "updateTextStyle": {
                    "range": self.rng(begin, end),
                    "textStyle": text_style,
                    "fields": fields,
                }
            })

    def paragraph(self, text: str, *, named: str = "NORMAL_TEXT", spans=(), tabs: int = 0,
                  indent_pt: float = 0.0, text_style: dict | None = None,
                  text_fields: str = "") -> tuple[int, int]:
        """Append one paragraph; return (start index, UTF-16 length of its text)."""
        body = ("\t" * tabs) + text
        start = self.pos
        length = u16(body)
        self.reqs.append({"insertText": {"location": self.loc(start), "text": body + "\n"}})
        self.reqs.append({
            "updateParagraphStyle": {
                "range": self.rng(start, start + length + 1),
                "paragraphStyle": {
                    "namedStyleType": named,
                    "indentStart": {"magnitude": indent_pt, "unit": "PT"},
                    "indentFirstLine": {"magnitude": indent_pt, "unit": "PT"},
                },
                "fields": "namedStyleType,indentStart,indentFirstLine",
            }
        })
        if length:
            self.reqs.append({
                "updateTextStyle": {
                    "range": self.rng(start, start + length),
                    "textStyle": {},
                    "fields": RESET_TEXT_FIELDS,
                }
            })
            if text_style:
                self.reqs.append({
                    "updateTextStyle": {
                        "range": self.rng(start, start + length),
                        "textStyle": text_style,
                        "fields": text_fields,
                    }
                })
            self.style_spans(start, spans, offset=tabs)
        self.pos = start + length + 1
        return start, length

    def bullet_run(self, ranges: list[tuple[int, int]], preset: str, tabs: int) -> None:
        """One createParagraphBullets over a contiguous run of list paragraphs."""
        if not ranges:
            return
        first = ranges[0][0]
        last_start, last_len = ranges[-1]
        last = last_start + max(last_len, 1)
        self.bullets.append({
            "createParagraphBullets": {
                "range": self.rng(first, last),
                "bulletPreset": preset,
            }
        })
        self.removals += tabs

    # ---- composite blocks

    def image(self, uri: str, caption: str, width_pt: float,
              height_pt: float | None = None) -> None:
        """Insert one image with its caption. `height_pt` may be left unstated.

        Docs derives the missing dimension from the image's own resolution, which
        is the only option for a figure this tool never sees: a remote image is
        fetched by Google, not by us, so its aspect ratio is not ours to compute.
        """
        start = self.pos
        size = {"width": {"magnitude": width_pt, "unit": "PT"}}
        if height_pt is not None:
            size["height"] = {"magnitude": height_pt, "unit": "PT"}
        self.reqs.append({
            "insertInlineImage": {
                "location": self.loc(start),
                "uri": uri,
                "objectSize": size,
            }
        })
        # The image occupies exactly one index unit; the caption goes into a
        # paragraph of its own, and the trailing empty paragraph is restored.
        self.reqs.append({"insertText": {"location": self.loc(start + 1), "text": "\n" + caption + "\n"}})
        caption_start = start + 2
        length = u16(caption)
        for span in ((start, start + 2), (caption_start, caption_start + length + 1)):
            self.reqs.append({
                "updateParagraphStyle": {
                    "range": self.rng(*span),
                    "paragraphStyle": {
                        "namedStyleType": "NORMAL_TEXT",
                        "indentStart": {"magnitude": 0, "unit": "PT"},
                        "indentFirstLine": {"magnitude": 0, "unit": "PT"},
                    },
                    "fields": "namedStyleType,indentStart,indentFirstLine",
                }
            })
        if length:
            self.reqs.append({
                "updateTextStyle": {
                    "range": self.rng(caption_start, caption_start + length),
                    "textStyle": {
                        "italic": True,
                        "fontSize": {"magnitude": 9, "unit": "PT"},
                        "foregroundColor": {"color": {"rgbColor": {"red": 0.4, "green": 0.4, "blue": 0.4}}},
                    },
                    "fields": "italic,fontSize,foregroundColor",
                }
            })
        self.pos = start + length + 3
        self.flush()  # verify the +1 image assumption immediately

    def table(self, rows: list[list[str]]) -> None:
        """Insert a table and fill it, reading the cell offsets back from the server.

        Tables are the one structure whose indices are not predictable from what
        was inserted: Docs decides the internal layout. So the offsets are read
        rather than computed, and the cells are filled highest-index-first so an
        earlier insertion cannot invalidate a later cell's start.
        """
        self.flush()
        width = max(len(row) for row in rows)
        gdocs.batch_update(self.doc_id, [{
            "insertTable": {
                "endOfSegmentLocation": {"tabId": self.tab_id},
                "rows": len(rows),
                "columns": width,
            }
        }], self.token)

        cells = self._table_cells()
        fills = []
        for (row, col, start) in sorted(cells, key=lambda item: -item[2]):
            text = rows[row][col] if col < len(rows[row]) else ""
            plain, _ = inline_spans(text)
            if plain:
                fills.append({"insertText": {"location": self.loc(start), "text": plain}})
        if fills:
            gdocs.batch_update(self.doc_id, fills, self.token)

        styles: list[dict] = []
        self.reqs = []
        for (row, col, start) in self._table_cells():
            text = rows[row][col] if col < len(rows[row]) else ""
            plain, spans = inline_spans(text)
            if not plain:
                continue
            end = start + u16(plain)
            styles.append({
                "updateTextStyle": {
                    "range": self.rng(start, end),
                    "textStyle": {"bold": True} if row == 0 else {},
                    "fields": "bold" if row == 0 else RESET_TEXT_FIELDS,
                }
            })
            self.style_spans(start, spans)
        styles.extend(self.reqs)
        self.reqs = []
        if styles:
            gdocs.batch_update(self.doc_id, styles, self.token)
        self.sync()

    def _table_cells(self) -> list[tuple[int, int, int]]:
        content = tab_body(gdocs.get_tabs(self.doc_id, self.token), self.tab_id)
        tables = [element for element in content if "table" in element]
        if not tables:
            raise RenderError("insertTable produced no table in the tab body")
        table = tables[-1]["table"]
        cells = []
        for row_index, row in enumerate(table["tableRows"]):
            for col_index, cell in enumerate(row["tableCells"]):
                cells.append((row_index, col_index, cell["content"][0]["startIndex"]))
        return cells


def render_blocks(writer: TabWriter, blocks: list[dict], locals_: dict[str, Path],
                  images: dict[str, str]) -> None:
    """Render parsed blocks into the tab.

    `images` maps each figure's written target to the URI Docs will fetch;
    `locals_` maps the local ones to their shrunk copy on disk. A target missing
    from `locals_` is a remote image, which has no copy and no dimensions here.
    """
    index = 0
    while index < len(blocks):
        block = blocks[index]
        kind = block["kind"]

        if kind in ("bullet", "numbered"):
            # A run of list items becomes one createParagraphBullets, because the
            # request numbers a range: applied per item, a numbered list restarts
            # at 1 on every line.
            preset = BULLET_PRESET if kind == "bullet" else NUMBER_PRESET
            ranges, tabs = [], 0
            while index < len(blocks) and blocks[index]["kind"] == kind:
                item = blocks[index]
                plain, spans = inline_spans(item["text"])
                level = item.get("level", 0)
                ranges.append(writer.paragraph(plain, spans=spans, tabs=level))
                tabs += level
                index += 1
            writer.bullet_run(ranges, preset, tabs)
            continue

        if kind == "heading":
            plain, spans = inline_spans(block["text"])
            writer.paragraph(plain, named=f"HEADING_{block['level']}", spans=spans)
        elif kind == "note":
            writer.paragraph(
                block["text"],
                text_style={"italic": True, "fontSize": {"magnitude": 9, "unit": "PT"}},
                text_fields="italic,fontSize",
            )
        elif kind == "quote":
            plain, spans = inline_spans(block["text"])
            writer.paragraph(plain, spans=spans, indent_pt=QUOTE_INDENT_PT)
        elif kind == "para":
            plain, spans = inline_spans(block["text"])
            writer.paragraph(plain, spans=spans)
        elif kind == "table":
            writer.table(block["rows"])
        elif kind == "image":
            target = block["path"]
            caption = f"{block['alt']}  [{target}]".strip()
            local = locals_.get(target)
            if local is None:
                # Remote: there is no file here to measure, so only the width is
                # stated and Docs keeps the aspect ratio. Opening Pillow for a
                # figure we never downloaded would also make a remote-only draft
                # need the optional extra for nothing.
                writer.image(images[target], caption, FIGURE_WIDTH_PT)
            else:
                Image, _ = _pillow()
                with Image.open(local) as handle:
                    ratio = handle.height / handle.width
                writer.image(images[target], caption, FIGURE_WIDTH_PT,
                             round(FIGURE_WIDTH_PT * ratio, 2))
        else:  # pragma: no cover - the parser emits nothing else
            raise RenderError(f"unhandled block kind {kind!r}")
        index += 1
    writer.flush()


# --- the shared render path --------------------------------------------------


def _prepare(source: Path, note: str, out) -> tuple[list[dict], dict[str, Path], Path]:
    """Shrink the figures and parse the markdown. No network, nothing created yet.

    Returns the parsed blocks, a {figure target: shrunk copy} mapping for the
    local figures, and the temporary directory holding those copies — which the
    caller must remove. The directory is named in the return rather than in the
    progress line because it is scaffolding, not a result: printing it invited
    the reader to go and look at a directory that is deleted by the time the
    command exits.
    """
    source = Path(source)
    if not source.exists():
        raise PublishError(f"source markdown not found: {source}")

    targets = figure_targets(source)
    figures = list(dict.fromkeys(targets.values()))
    workdir = Path(tempfile.mkdtemp(prefix="marginal-publish-"))
    total, shrunk = shrink_figures(figures, workdir / "figures")
    locals_ = {target: shrunk[path] for target, path in targets.items()}
    if figures:
        out(f"figures: {len(figures)} shrunk to {total // 1024} KB")

    blocks = parse_markdown(source, note=note)
    counts = _expected(blocks)
    out(f"parsed:  {len(blocks)} blocks, {counts['headings']} headings, "
        f"{counts['tables']} tables, {counts['images']} images")
    return blocks, locals_, workdir


def _expected(blocks: list[dict]) -> dict:
    return {
        "headings": sum(1 for b in blocks if b["kind"] == "heading"),
        "tables": sum(1 for b in blocks if b["kind"] == "table"),
        "images": sum(1 for b in blocks if b["kind"] == "image"),
    }


def _release(uploads: dict[str, dict], token: str) -> list[str]:
    """Revoke the public link, then trash. Best effort; returns what was left behind.

    Unshare *before* trash, always. A trashed Drive file is still served to
    anyone holding its link until it is purged, so trashing alone leaves every
    figure of every draft world-readable — the exact state this whole dance
    exists to avoid.

    Each copy is released under its own guard so one Drive failure cannot strand
    the rest, and never raises: by the time this runs the document may already be
    correct, and a tidy-up error must not be mistaken for a render failure.
    """
    left: list[str] = []
    for path, info in uploads.items():
        try:
            if info["permission"]:
                gdocs.unshare(info["id"], info["permission"], token)
            gdocs.trash(info["id"], token)
        except Exception as exc:
            left.append(info["id"])
            print(f"warn: temporary Drive copy of {path} LEFT IN PLACE as {info['id']}, "
                  f"possibly still world-readable: {exc}", file=sys.stderr)
    return left


def _render_into(doc_id: str, tab_id: str, blocks: list[dict], locals_: dict[str, Path],
                 token: str, out) -> tuple[dict, dict, int]:
    """Upload the figures, render the blocks, verify the result.

    The one path both entry points take, so `publish` and `publish-tab` cannot
    render the same markdown two different ways. Raises `RenderError` when the
    render fails or the finished tab does not match the parsed markdown, having
    first released its own temporary uploads; the caller decides whether the
    wreckage to remove is a tab or a whole document.

    Returns `(uploads, the verified document, expected image count)` and stops
    there. Releasing the uploads is deliberately the caller's next step rather
    than the tail of this one: it happens after the tab is known to be good, and
    a Drive error while tidying up used to arrive as a `RenderError` and trash a
    document that had already verified.
    """
    expected = _expected(blocks)

    # Every distinct local figure gets one temporary, world-readable Drive copy —
    # Docs fetches the image by URI from its own servers, so a private file is
    # simply not visible to it. A remote figure is already a URI Google can
    # fetch; copying it to Drive would republish somebody else's image.
    uploads: dict[str, dict] = {}
    uris: dict[str, str] = {}
    try:
        for block in blocks:
            if block["kind"] != "image" or block["path"] in uris:
                continue
            target = block["path"]
            if target.startswith(("http://", "https://")):
                uris[target] = target
                continue
            meta = gdocs.upload_png(locals_[target], token)
            # Recorded before it is shared, so a share that fails still leaves a
            # file the rollback below knows about rather than an orphan.
            uploads[target] = {"id": meta["id"], "permission": None}
            permission = gdocs.share_anyone(meta["id"], token)
            uploads[target]["permission"] = permission["id"]
            uris[target] = f"https://drive.google.com/uc?export=view&id={meta['id']}"
    except Exception as exc:
        # Outside the rollback envelope this loop used to leave every copy made
        # before the failure public on the account forever: the caller deleted
        # the tab and nothing ever touched Drive again.
        _release(uploads, token)
        raise RenderError(str(exc)) from exc
    if uploads:
        out(f"drive:   {len(uploads)} temporary image(s) uploaded and shared")

    try:
        writer = TabWriter(doc_id, tab_id, token)
        render_blocks(writer, blocks, locals_, uris)
        doc = gdocs.get_tabs(doc_id, token)
        stats = tab_stats(doc, tab_id)
    except Exception as exc:
        _release(uploads, token)
        raise RenderError(str(exc)) from exc

    out(f"verify:  headings {stats['headings']}/{expected['headings']}, "
        f"tables {stats['tables']}/{expected['tables']}, "
        f"images {stats['images']}/{expected['images']}")
    if stats != expected:
        _release(uploads, token)
        raise RenderError("the rendered tab does not match the parsed markdown")
    out("verify:  ok")
    return uploads, doc, expected["images"]


def _release_images(uploads: dict[str, dict], doc: dict, tab_id: str, images: int,
                    token: str, out) -> None:
    """Give back the temporary Drive copies now the tab is known to be good.

    Only once Docs has taken its own copy of every image is it safe to revoke the
    sharing and trash the upload; otherwise the tab would show dead images. When
    that proof does not arrive the copies are left in place and named, because a
    world-readable copy of every figure quietly abandoned on the account is the
    worse of the two failures.

    Nothing here is fatal. Verification has already passed, so the document is
    correct and the exit code says so; what can go wrong is only ever leftover
    scaffolding, which is reported and not raised.
    """
    if not uploads:
        return
    hosted_all = image_content_uris(doc, tab_id)
    hosted = [uri for uri in hosted_all if "googleusercontent.com" in uri]
    if len(hosted) == len(hosted_all) == images:
        left = _release(uploads, token)
        if left:
            out(f"images:  {len(uploads) - len(left)} of {len(uploads)} temporary Drive "
                f"copies released; {len(left)} left in place (see the warnings above)")
        else:
            out(f"images:  {len(hosted)} re-hosted by Docs; temporary Drive copies "
                "unshared and trashed")
        return
    print("images:  contentUri still points at the source - temporary Drive copies "
          "LEFT IN PLACE and still world-readable; delete them by hand:",
          file=sys.stderr)
    for path, info in uploads.items():
        print(f"  {path} -> {info['id']}", file=sys.stderr)


# --- entry points ------------------------------------------------------------


def default_tab_title(today: datetime.date | None = None) -> str:
    """`v1 — DD-MM`. What the first tab of a new document is called."""
    return f"v1 — {(today or datetime.date.today()).strftime('%d-%m')}"


def publish_tab(doc_id: str, source, tab_title: str, note: str, token: str, out=print) -> int:
    """Render `source` into a new tab of an existing document. 0, or 3 on failure.

    The tab is created before the render because every request in the render is
    scoped by its id. That is also why a failure deletes it: an empty or
    half-written tab is visible to everyone the document is shared with, and it
    is indistinguishable from a version somebody meant to publish.
    """
    blocks, locals_, workdir = _prepare(Path(source), note, out)

    # The shrunk copies are scaffolding for one render; left behind they
    # accumulate a directory of every figure of every draft in the system
    # temporary directory, which nothing ever cleans up.
    try:
        try:
            props = gdocs.add_tab(doc_id, tab_title, token)
        except RuntimeError as exc:
            # `add_tab` raises when the reply carries no tabId, which means the
            # Docs API answered in a shape this code does not know. Nothing was
            # created, so it is a message rather than a traceback — but it is not
            # a message the user can act on, so it says what the server said.
            raise PublishError(f"could not add a tab to {doc_id}: {exc}") from exc
        tab_id = props["tabId"]
        out(f"tab:     {tab_id} ({props.get('title')}) index={props.get('index')}")

        try:
            uploads, doc, images = _render_into(doc_id, tab_id, blocks, locals_, token, out)
        except RenderError as exc:
            print(f"render failed: {exc}", file=sys.stderr)
            print(f"cleanup: deleting tab {tab_id}", file=sys.stderr)
            gdocs.delete_tab(doc_id, tab_id, token)
            return 3

        _release_images(uploads, doc, tab_id, images, token, out)

        # Docs hands back tabIds already prefixed with "t."; the anchor is ?tab=t.<id>.
        anchor = tab_id if tab_id.startswith("t.") else f"t.{tab_id}"
        out("")
        out(f"https://docs.google.com/document/d/{doc_id}/edit?tab={anchor}")
        return 0
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def publish_doc(title: str, source, tab_title: str, note: str, token: str, out=print) -> int:
    """Create a document and render `source` into its first tab. 0, or 3 on failure.

    A new document starts with exactly one tab, which is unnamed; renaming it
    rather than adding a second one means the next version can be published with
    `publish_tab` against the same file and sit beside this one, which is the
    whole point of the tab-per-version arrangement.

    On failure the document itself is trashed, not just the tab. Nothing was here
    before this call, so there is nothing to preserve, and a document cannot be
    left with zero tabs anyway.
    """
    blocks, locals_, workdir = _prepare(Path(source), note, out)

    try:
        created = gdocs.create_document(title, token)
        doc_id = created.get("documentId")
        if not doc_id:
            raise PublishError(f"documents.create returned no documentId: {created!r}")

        # `_default_tab` sits inside the cleanup, not before it. Its fallback
        # fetch is a network call like any other, and a document created one line
        # earlier used to survive that call failing — a titled, empty file left on
        # the account by the very function whose contract is to leave nothing.
        try:
            tab_id = _default_tab(created, doc_id, token)
            out(f"created: {doc_id} (tab {tab_id})")
            gdocs.update_tab_title(doc_id, tab_id, tab_title, token)
            uploads, doc, images = _render_into(doc_id, tab_id, blocks, locals_, token, out)
        except (PublishError, gdocs.GoogleApiError) as exc:
            print(f"publish failed: {exc}", file=sys.stderr)
            print(f"cleanup: trashing the new document {doc_id}", file=sys.stderr)
            gdocs.trash(doc_id, token)
            return 3

        # Past this line the document has verified, so nothing left to do may
        # trash it: the release of the temporary uploads reports its own failures
        # and the command still exits 0.
        _release_images(uploads, doc, tab_id, images, token, out)

        out("")
        out(f"https://docs.google.com/document/d/{doc_id}/edit")
        return 0
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _default_tab(created: dict, doc_id: str, token: str) -> str:
    """The tabId of the single tab a freshly created document has.

    `documents.create` has been observed to answer both with and without `tabs`;
    the fetch is the fallback rather than the default because it costs a round
    trip to learn something the create response usually already said.
    """
    for payload in (created, None):
        doc = payload if payload is not None else gdocs.get_tabs(doc_id, token)
        tabs = doc.get("tabs") or []
        if tabs:
            tab_id = (tabs[0].get("tabProperties") or {}).get("tabId")
            if tab_id:
                return tab_id
    raise PublishError(f"the new document {doc_id} reported no tabs to render into")
