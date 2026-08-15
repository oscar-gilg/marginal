"""Google Docs/Drive access: read document text, read/write comment threads.

Everything here is API-only and validated. The Drive API can read anchored
comments and create *replies*, but it cannot create an anchored comment — the
Docs UI is the only way to do that without the Developer Preview. See
`docs_ui.py` for the browser path.

Auth is owned by Marginal and supports named accounts.  An explicitly named
workspace-MCP credential remains as a deprecated migration path; it is never
discovered implicitly.
"""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
import warnings
from pathlib import Path

DOC_ID_RE = re.compile(r"/document/d/([A-Za-z0-9_-]+)")

# Every Google call is bounded: a stalled endpoint should fail, not hang a run.
TIMEOUT = 60


def doc_id_from_url(url: str) -> str:
    """Accept a full Docs URL or a bare document id."""
    m = DOC_ID_RE.search(url)
    if m:
        return m.group(1)
    if "/" not in url and len(url) > 20:
        return url
    raise ValueError(f"could not find a document id in {url!r}")


def tab_from_url(url: str) -> str | None:
    """The tab id a Docs URL names, or None if it names none.

    The URL a person copies out of the address bar carries the tab they were
    looking at. Reading only the document id out of it and leaving the tab
    unspecified is how a run reviewed a different tab than the one pasted — an
    error nothing downstream could detect, because every anchor in the wrong tab
    verifies perfectly.

    `tab` is in the query on some URLs and after the `#` on others; check both.
    """
    parts = urllib.parse.urlsplit(url)
    for source in (parts.query, parts.fragment):
        tab = urllib.parse.parse_qs(source).get("tab")
        if tab and tab[0]:
            return tab[0]
    return None


def access_token(creds_path: Path | None = None, *, account: str | None = None) -> str:
    """Mint an access token for an owned account or explicit legacy credential.

    Configurable so the model can post and reply as its own Google account rather
    than as the human running the tool. That matters beyond cosmetics: a separate
    account makes `author.me` on each comment authoritative, replacing the
    ledger-text heuristic for deciding which threads are the model's own.

    The positional path is retained only for existing configurations.  There is no
    scan of another tool's token directory: owned accounts are selected explicitly,
    by configured default, or when exactly one exists.
    """
    from . import auth

    if creds_path is not None:
        warnings.warn(
            "using an explicit legacy Google credential; run `marginal auth` "
            "to move this account into Marginal's owned token store",
            FutureWarning,
            stacklevel=2,
        )
        return auth.legacy_access_token(Path(creds_path))
    return auth.access_token(account)


def _call(url: str, token: str, method: str = "GET", body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Authorization": "Bearer " + token}
    if data:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        raw = r.read()
    return json.loads(raw) if raw else {}


# --- document text -----------------------------------------------------------


def get_tabs(doc_id: str, token: str) -> dict:
    q = urllib.parse.urlencode(
        {"includeTabsContent": "true", "suggestionsViewMode": "SUGGESTIONS_INLINE"}
    )
    return _call(f"https://docs.googleapis.com/v1/documents/{doc_id}?{q}", token)


def get_revision_id(doc_id: str, token: str) -> str:
    """Fetch only the concurrency token used to guard the next browser post."""
    q = urllib.parse.urlencode({"fields": "revisionId"})
    revision = _call(f"https://docs.googleapis.com/v1/documents/{doc_id}?{q}", token).get(
        "revisionId"
    )
    if not isinstance(revision, str) or not revision:
        raise RuntimeError("Docs API response contained no revisionId")
    return revision


def walk_tabs(tabs: list[dict], _level: int = 0):
    for t in tabs:
        yield t, _level
        yield from walk_tabs(t.get("childTabs", []), _level + 1)


def tab_text(tab: dict) -> dict:
    """Flatten one tab to {'id', 'title', 'text', 'paragraphs'}.

    'text' is the canonical character stream that spans are expressed in. Table
    cell content is walked inline, in document order — note that the editor's
    caret may not traverse tables the same way, which is why span selection is
    verified against the API after posting rather than trusted.
    """
    out: list[str] = []
    paragraphs: list[dict] = []
    tables: list[dict] = []
    figures: list[dict] = []
    refs: list[dict] = []
    pos = 0

    def walk(content: list[dict], in_table: bool) -> None:
        nonlocal pos
        for el in content:
            if "paragraph" in el:
                para = el["paragraph"]
                start = pos
                for pe in para.get("elements", []):
                    # Anything that is not a text run contributes no characters, so
                    # it is recorded by position rather than inserted — `text` is the
                    # coordinate space anchors resolve in and must not shift.
                    if "inlineObjectElement" in pe:
                        figures.append(
                            {"at": pos, "id": pe["inlineObjectElement"].get("inlineObjectId")}
                        )
                        continue
                    if "footnoteReference" in pe:
                        fr = pe["footnoteReference"]
                        refs.append(
                            {
                                "at": pos,
                                "id": fr.get("footnoteId"),
                                "number": fr.get("footnoteNumber"),
                            }
                        )
                        continue
                    tr = pe.get("textRun")
                    s = tr.get("content", "") if tr else ""
                    if not s:
                        continue
                    out.append(s)
                    pos += len(s)
                style = para.get("paragraphStyle", {}).get("namedStyleType", "NORMAL_TEXT")
                p = {"start": start, "end": pos, "style": style}
                if "bullet" in para:
                    p["bullet"] = para["bullet"].get("nestingLevel", 0)
                if in_table:
                    p["in_table"] = True
                paragraphs.append(p)
            elif "table" in el:
                rows = el["table"].get("tableRows", [])
                start = pos
                for row in rows:
                    for cell in row.get("tableCells", []):
                        walk(cell.get("content", []), True)
                tables.append(
                    {
                        "start": start,
                        "end": pos,
                        "rows": len(rows),
                        "cols": max((len(r.get("tableCells", [])) for r in rows), default=0),
                    }
                )

    walk(tab["documentTab"]["body"].get("content", []), False)

    # Footnote bodies live outside body.content, so nothing above reaches them. They
    # were dropped entirely, which let the model call a claim unsupported when its
    # support was in a footnote it had never been shown.
    bodies = tab["documentTab"].get("footnotes", {}) or {}
    footnotes = []
    for ref in refs:
        note = bodies.get(ref["id"]) or {}
        text = "".join(
            pe.get("textRun", {}).get("content", "")
            for e in note.get("content", [])
            for pe in e.get("paragraph", {}).get("elements", [])
        ).strip()
        footnotes.append({"number": ref["number"], "at": ref["at"], "text": text})

    # The image bytes live in a sibling map, keyed by the id recorded above.
    objects = tab["documentTab"].get("inlineObjects", {}) or {}
    for fig in figures:
        embedded = (objects.get(fig["id"]) or {}).get("inlineObjectProperties", {}).get(
            "embeddedObject", {}
        )
        fig["uri"] = (embedded.get("imageProperties") or {}).get("contentUri")
        fig["alt"] = embedded.get("description") or embedded.get("title") or ""

    props = tab.get("tabProperties", {})
    return {
        "id": props.get("tabId"),
        "title": props.get("title"),
        "text": "".join(out),
        "paragraphs": paragraphs,
        "tables": tables,
        "figures": figures,
        "footnotes": footnotes,
    }


def export_markdown(doc_id: str, token: str) -> str:
    """The document as Markdown, via Drive export.

    What the reviewer reads. Headings, list markers and pipe tables survive, and
    footnote bodies come through inline — none of which exist in the plain text
    stream. It is *not* the anchor space: `select_span` navigates by character
    offset, so a stream containing `**` or `#` would displace every span after the
    first marker. See `anchors.py` for the way back.
    """
    url = (
        f"https://www.googleapis.com/drive/v3/files/{doc_id}/export"
        f"?mimeType={urllib.parse.quote('text/markdown')}"
    )
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read().decode("utf-8")


def read_doc(doc_id: str, token: str) -> dict:
    """Return title, tabs, and the revision those coordinate streams came from."""
    raw = get_tabs(doc_id, token)
    revision = raw.get("revisionId")
    if not isinstance(revision, str) or not revision:
        raise RuntimeError("Docs API response contained no revisionId")
    tabs = [tab_text(t) for t, _ in walk_tabs(raw.get("tabs", []))]
    for tab in tabs:
        # Structure beside the coordinate stream, never inside it.  Keeping the
        # baseline on the tab lets every posting entry point enforce freshness.
        tab["revision_id"] = revision
    return {
        "title": raw.get("title"),
        "revision_id": revision,
        "tabs": tabs,
    }


# --- comment threads ---------------------------------------------------------

_FIELDS = (
    "nextPageToken,comments(id,author(displayName,me),createdTime,modifiedTime,"
    "resolved,content,quotedFileContent(value),anchor,"
    "replies(id,author(displayName,me),createdTime,content,action))"
)


def list_comments(doc_id: str, token: str) -> list[dict]:
    base = f"https://www.googleapis.com/drive/v3/files/{doc_id}/comments?"
    out: list[dict] = []
    page = None
    while True:
        q = {"fields": _FIELDS, "pageSize": 100}
        if page:
            q["pageToken"] = page
        r = _call(base + urllib.parse.urlencode(q), token)
        out += r.get("comments", [])
        page = r.get("nextPageToken")
        if not page:
            return out


def create_reply(doc_id: str, comment_id: str, content: str, token: str) -> dict:
    """Replies need no anchor, so unlike a top-level comment this needs no browser."""
    url = (
        f"https://www.googleapis.com/drive/v3/files/{doc_id}/comments/{comment_id}/replies"
        "?fields=id,content,author(displayName),createdTime"
    )
    return _call(url, token, method="POST", body={"content": content})


def delete_comment(doc_id: str, comment_id: str, token: str) -> None:
    """Permanent. Only works on comments we authored — this is the unpost path."""
    _call(
        f"https://www.googleapis.com/drive/v3/files/{doc_id}/comments/{comment_id}",
        token,
        method="DELETE",
    )
