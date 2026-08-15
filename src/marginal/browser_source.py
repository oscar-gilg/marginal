"""Read the document and its comments through the browser, with no Google API.

This is the zero-credential path: the Chrome we already drive is signed in, so its
own session can pull the document's export endpoints. No OAuth client, no consent
screen, no tokens, nothing for a user to configure.

**Mechanism.** A page-context `fetch` cannot do this — `/export` 302-redirects to a
`googleusercontent.com` download host that sends no CORS headers, so the fetch
fails. A *navigation* is not subject to CORS, so the export is triggered as a
navigation and captured with CDP's download interception instead.

**Both halves work, so tier A verifies as well as reads:**

* `format=txt` gives the document text. Paragraph boundaries come from newlines,
  which is what the API's text stream marks them with anyway, so span resolution is
  unchanged.
* `format=docx` contains `word/comments.xml` (comment bodies) and
  `commentRangeStart`/`End` markers in `word/document.xml` (the anchored ranges) —
  which is exactly what post-hoc verification needs. Confirmed against a real
  document: every comment and every anchor range recovered.

The docx route is slower than the Drive API (a file download and a zip parse per
check, rather than one JSON call), so it is the fallback for users with no
credentials rather than the default.
"""

from __future__ import annotations

import shutil
import time
import xml.etree.ElementTree as ET
import zipfile
from io import BytesIO
from pathlib import Path
from tempfile import mkdtemp

from . import reactions
from .cdp import Page

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

# Exports of the same document within one run, kept because each download costs an
# entry in the browser's download history and a file on disk. Reading the document
# and taking the pre-post snapshot ask for the same bytes moments apart; verifying
# afterwards must not, so `invalidate` is called once a comment has been posted.
_CACHE: dict[tuple[str, str], bytes] = {}


def invalidate(doc_id: str | None = None) -> None:
    """Forget cached exports. Call after changing the document."""
    if doc_id is None:
        _CACHE.clear()
        return
    for key in [k for k in _CACHE if k[0] == doc_id]:
        del _CACHE[key]


def _download(page: Page, url: str, timeout: float) -> bytes | None:
    """The fallback: let Chrome download the export, read it, then clean up.

    The temp directory is removed on every path. It used to be removed only when
    nothing arrived, so each *successful* export leaked one, and they accumulated
    unnoticed.
    """
    outdir = Path(mkdtemp(prefix="marginal-export-"))
    try:
        page.send(
            "Browser.setDownloadBehavior",
            {"behavior": "allow", "downloadPath": str(outdir), "eventsEnabled": True},
        )
        page.send("Page.navigate", {"url": url})
        deadline = time.time() + timeout
        while time.time() < deadline:
            done = [p for p in outdir.iterdir() if p.suffix != ".crdownload"]
            if done:
                return done[0].read_bytes()
            time.sleep(0.25)
        return None
    finally:
        shutil.rmtree(outdir, ignore_errors=True)


def export(
    page: Page,
    doc_id: str,
    fmt: str = "txt",
    timeout: float = 20.0,
    attempts: int = 3,
    tab_id: str | None = None,
) -> bytes:
    """Download one of the document's exports using the browser's own session.

    `tab_id` scopes the export to one tab. Without it the endpoint returns the
    *whole* document, which on a multi-tab document is the worst version of the
    wrong-tab bug: the text handed downstream was every tab concatenated while
    the tab dict still claimed to be one, so every offset past the first tab
    addressed the wrong characters. Verified against the Docs API — for
    a named tab, `&tab=` and `documents.get` agree exactly, where the unscoped
    export returns the whole document starting in a different tab.

    Retried: exporting the same document repeatedly in quick succession sometimes
    produces no download at all, then succeeds again moments later — which looks
    like throttling on Google's side rather than anything local. One long wait would just stall, so this makes
    several shorter attempts instead.
    """
    # The tab belongs in the cache key: keyed on (doc, fmt) alone, the first tab
    # exported would be served for every later one.
    key = (doc_id, fmt, tab_id)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached
    url = f"https://docs.google.com/document/d/{doc_id}/export?format={fmt}"
    if tab_id:
        url += f"&tab={tab_id}"
    # An in-page `fetch` was tried and does not work: /export 302-redirects to a
    # googleusercontent host with no CORS headers, so it fails with "Failed to
    # fetch". A navigation is not subject to CORS, which is
    # why this stays a download despite every download costing an entry in the
    # browser's history.
    for attempt in range(1, attempts + 1):
        got = _download(page, url, timeout)
        if got is not None:
            _CACHE[key] = got
            return got
        if attempt < attempts:
            time.sleep(2.0 * attempt)
    raise TimeoutError(
        f"export?format={fmt} produced no download after {attempts} attempts. "
        "Either this Chrome is not signed into an account with access to the "
        "document, or Google is throttling repeated exports — retry shortly."
    )


def read_tab(page: Page, doc_id: str, tab_id: str | None = None) -> dict:
    """Return {'id', 'title', 'text', 'paragraphs'} — shaped like `gdocs.tab_text`,
    so nothing downstream can tell which source produced it.

    Text comes from the **docx**, not the txt export. The txt export injects
    footnote-style comment markers inline at every anchor position
    (`Survey Coverage[a] in the Pilot[b]`), appends every comment body at the end,
    and includes bullet characters — so its offsets shift each time a comment is
    posted, which would silently misplace later spans.

    Measured against the same document, docx-derived text matched the Docs API text
    except where a smart chip renders (the API omits the chip; the docx includes it).
    The chip occupies caret positions, so including it likely models what the caret
    traverses *better* than the API does — untested, but it is the drift that forced
    self-correcting selection in the first place.
    """
    text = text_from_docx(export(page, doc_id, "docx", tab_id=tab_id))
    return {
        "id": tab_id,
        "title": None,
        "text": text,
        "paragraphs": paragraphs_from_text(text),
    }


def text_from_docx(blob: bytes) -> str:
    """Body text, one line per paragraph. Comment ranges are XML elements here, not
    inline markers, so nothing anchor-related contaminates the offsets."""
    z = zipfile.ZipFile(BytesIO(blob))
    body = ET.fromstring(z.read("word/document.xml")).find(f"{W}body")
    if body is None:
        return ""
    return "\n".join(_live_text(p) for p in body.iter(f"{W}p")) + "\n"


def paragraphs_from_text(text: str) -> list[dict]:
    """Paragraph spans derived from newlines.

    The API's text stream ends every paragraph with a newline, so splitting on them
    reproduces the boundaries the paragraph-jump strategy needs.

    The trailing empty string a terminating newline leaves behind is *not* a
    paragraph — it is the previous paragraph's terminator, already counted. Emitting
    a span for it produced one starting at `len(text)` and ending past it: one more
    paragraph than the caret traverses, and one more than `gdocs.tab_text` reports
    for the same document. Harmless for any offset inside the text, which is why it
    survived, but it is a disagreement between the two sources about a coordinate
    structure, and this file exists to be indistinguishable from the other one.
    """
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    out: list[dict] = []
    pos = 0
    for line in lines:
        end = pos + len(line) + 1  # include the newline, as the API's offsets do
        out.append({"start": pos, "end": end, "style": "NORMAL_TEXT"})
        pos = end
    return out


def _runs(node: ET.Element) -> str:
    return "".join(t.text or "" for t in node.iter(f"{W}t"))


def _comment_text(comment: ET.Element) -> str:
    """A comment's body, with its line breaks intact.

    The docx stores each line of a comment as its own `w:p`, so concatenating every
    run drops the newlines — which silently broke matching for any comment carrying
    an attribution header (`**fable-5**\\nBody` came back as `**fable-5**Body`).
    Verification matches on the body, so every headed comment failed to verify while
    being posted perfectly well.
    """
    return "\n".join(_runs(p) for p in comment.iter(f"{W}p")).strip()


# A soft line break (Shift+Enter) is one character in the document stream, not a
# paragraph split. The Docs API spells it U+000B, so that is what the export has to
# contribute for the two sources to agree. Pinned by an end-to-end test rather than
# by belief: `tests/e2e/test_anchor_fidelity.py` compares the two streams directly,
# and if this constant is wrong it is wrong by exactly one character per break.
LINE_BREAK = "\v"


def _contributes(child: ET.Element) -> str | None:
    """The characters `child` adds to the stream, or None meaning "descend into it".

    The single mapping from an export element to the coordinate space. Both walks in
    this file go through it — the document stream below, and the comment anchors in
    `read_comments` — because they used to be two walks and separate meant different:
    one skipped suggested deletions and the other did not, so an anchor overlapping a
    suggestion disagreed with the space it was supposed to index into. Keeping them
    in step was a rule enforced by a comment; now there is nothing to keep in step.

    `""` and `None` are not interchangeable. `""` means "this element contributes no
    characters and has no interior worth reading" — a suggested deletion is displayed
    but is not part of the live document — while `None` means "a container: read its
    children". Returning None for `w:del` would splice struck-through text back into
    the stream and shift every offset after it.

    Not every visible character lives in a `w:t`. A tab and a line break each occupy
    a caret position and each has its own element, and dropping them shifted every
    offset after them — the failure this file exists to avoid.
    """
    tag = child.tag
    if tag == f"{W}del":
        return ""
    if tag == f"{W}t":
        return child.text or ""
    if tag == f"{W}tab":
        return "\t"
    if tag in (f"{W}br", f"{W}cr"):
        return LINE_BREAK
    return None


def _visible(node: ET.Element, out: list[str]) -> None:
    """Append the visible characters of `node`'s subtree to `out`."""
    for child in node:
        chars = _contributes(child)
        if chars is None:
            _visible(child, out)
        else:
            out.append(chars)


def _live_text(para: ET.Element) -> str:
    """Visible text of a paragraph, skipping suggested deletions."""
    parts: list[str] = []
    _visible(para, parts)
    return "".join(parts)


def read_comments(page: Page, doc_id: str) -> list[dict]:
    """Comments and their anchored text, read from the docx export.

    Returns [{'body', 'anchored'}] — enough to verify that a posted comment landed
    on the intended span without any Google API access. No comment ids: the export
    numbers comments per-document, and those numbers are not Drive's ids, so callers
    match on body text (which is what `post.py` matches on anyway).
    """
    blob = export(page, doc_id, "docx")
    z = zipfile.ZipFile(BytesIO(blob))
    names = z.namelist()
    if "word/comments.xml" not in names:
        return []

    bodies = {
        c.get(f"{W}id"): _comment_text(c)
        for c in ET.fromstring(z.read("word/comments.xml")).iter(f"{W}comment")
    }

    # Walk the body in order, accumulating text while inside a comment range. A span
    # can contain several runs, and ranges can nest, so track the open ids as a set.
    # The character rules are `_contributes`, the same ones `text_from_docx` uses: an
    # anchor is a slice of that stream, and a walk that counted a suggested deletion —
    # or dropped a tab — would report an anchor the document does not have. Only the
    # range bookkeeping is this walk's own.
    anchored: dict[str, list[str]] = {cid: [] for cid in bodies}
    open_ids: set[str] = set()

    def walk(node: ET.Element) -> None:
        for child in node:
            tag = child.tag
            if tag == f"{W}commentRangeStart":
                open_ids.add(child.get(f"{W}id"))
            elif tag == f"{W}commentRangeEnd":
                open_ids.discard(child.get(f"{W}id"))
            else:
                chars = _contributes(child)
                if chars is None:
                    walk(child)
                    continue
                for cid in open_ids:
                    anchored.setdefault(cid, []).append(chars)

    body = ET.fromstring(z.read("word/document.xml")).find(f"{W}body")
    for para in body.iter(f"{W}p") if body is not None else []:
        walk(para)
        # A range spanning a paragraph boundary contains that boundary's newline,
        # exactly as the document stream does.
        for cid in open_ids:
            anchored.setdefault(cid, []).append("\n")

    out = []
    for cid in bodies:
        # The export appends each reaction to the comment's own text, so the body a
        # caller compares against changes the moment somebody reacts. Split here,
        # once, rather than in each caller: `post.verify` matches bodies for exact
        # equality and would report a reacted comment as one that could not be read
        # back — live, correctly anchored, and counted as a failure.
        body, reacts = reactions.split_body(bodies[cid])
        row = {"body": body, "anchored": "".join(anchored.get(cid, [])).strip()}
        if reacts:
            row["reactions"] = reacts
        out.append(row)
    return out
