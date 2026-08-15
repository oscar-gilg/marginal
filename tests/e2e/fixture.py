"""Build a Google Doc containing every feature that can move a character offset.

The unit tests use synthetic Docs API payloads, which prove the extraction handles
a shape but not that the shape matches what Google actually sends, nor that the
editor's caret traverses it the way `select_span` assumes. This builds the real
thing so those two can be checked against each other.

Deliberately includes prose *after* an image, a table and a footnote: an element
that occupies a caret position while contributing no characters to the text stream
would displace every span that follows it, and only text after one can show that.
"""

from __future__ import annotations

import json
import urllib.request

DOCS = "https://docs.googleapis.com/v1/documents"
LOGO = ("https://www.google.com/images/branding/googlelogo/2x/"
        "googlelogo_color_92x30dp.png")

# Phrases the anchor tests aim at, chosen to sit either side of each hazard.
TARGETS = [
    "sits before any of the awkward elements",
    "immediately after the inline image above",
    "follows the bulleted list and should still anchor",
    "comes after the table and is the real test",
    "trails the footnote reference and must not drift",
    "sits after the tab character and would move if it were dropped",
    "sits after the soft line break and would move if it were dropped",
    "is the very last sentence of the document",
]

# A tab and a soft line break occupy a caret position each while living in their own
# OOXML elements. `browser_source._visible` decides what they contribute; the probes
# that follow them are what notice if it decides wrong.
TAB = "\t"
SOFT_BREAK = ""


def _post(path: str, token: str, body: dict) -> dict:
    req = urllib.request.Request(
        f"{DOCS}{path}", method="POST", data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {token}", "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def _get(doc_id: str, token: str) -> dict:
    req = urllib.request.Request(
        f"{DOCS}/{doc_id}?includeTabsContent=true",
        headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def create(token: str, title: str = "Marginal e2e fixture") -> str:
    """Create the fixture document and return its id."""
    doc = _post("", token, {"title": title})
    doc_id = doc["documentId"]
    body = "\n".join([
        "Anchor Fidelity Fixture",
        "This opening sentence sits before any of the awkward elements.",
        "Findings",
        "This sentence comes immediately after the inline image above.",
        "First bullet point about something",
        "Second bullet point about something else",
        "This sentence follows the bulleted list and should still anchor.",
        "This sentence comes after the table and is the real test.",
        "This sentence trails the footnote reference and must not drift.",
        f"Before a tab{TAB}this sentence sits after the tab character and would "
        "move if it were dropped.",
        f"Before a break{SOFT_BREAK}this sentence sits after the soft line break "
        "and would move if it were dropped.",
        "This sentence is the very last sentence of the document.",
        "",
    ])
    _post(f"/{doc_id}:batchUpdate", token,
          {"requests": [{"insertText": {"location": {"index": 1}, "text": body}}]})
    return doc_id


def decorate(doc_id: str, token: str) -> None:
    """Apply the formatting and insert the elements, back to front.

    Back to front because every insertion shifts the indices after it; working from
    the end of the document means the offsets computed here stay valid.
    """
    d = _get(doc_id, token)
    tab = d["tabs"][0]
    tid = tab["tabProperties"]["tabId"]
    paras = []
    for el in tab["documentTab"]["body"]["content"]:
        p = el.get("paragraph")
        if not p:
            continue
        text = "".join(e.get("textRun", {}).get("content", "") for e in p.get("elements", []))
        paras.append((el["startIndex"], el["endIndex"], text.strip()))

    def find(needle: str):
        return next((s, e) for s, e, t in paras if needle in t)

    reqs = []
    # headings
    for needle, style in (("Anchor Fidelity Fixture", "TITLE"), ("Findings", "HEADING_1")):
        s, e = find(needle)
        reqs.append({"updateParagraphStyle": {
            "range": {"startIndex": s, "endIndex": e, "tabId": tid},
            "paragraphStyle": {"namedStyleType": style}, "fields": "namedStyleType"}})
    # bullets
    s1, _ = find("First bullet")
    _, e2 = find("Second bullet")
    reqs.append({"createParagraphBullets": {
        "range": {"startIndex": s1, "endIndex": e2, "tabId": tid},
        "bulletPreset": "BULLET_DISC_CIRCLE_SQUARE"}})
    # bold inside a target sentence, to exercise stripping on a real export
    s, _ = find("comes after the table")
    reqs.append({"updateTextStyle": {
        "range": {"startIndex": s + 5, "endIndex": s + 13, "tabId": tid},
        "textStyle": {"bold": True}, "fields": "bold"}})
    _post(f"/{doc_id}:batchUpdate", token, {"requests": reqs})

    # Insertions, latest position first so earlier indices stay valid.
    d = _get(doc_id, token)
    tab = d["tabs"][0]
    paras = []
    for el in tab["documentTab"]["body"]["content"]:
        p = el.get("paragraph")
        if not p:
            continue
        text = "".join(e.get("textRun", {}).get("content", "") for e in p.get("elements", []))
        paras.append((el["startIndex"], el["endIndex"], text.strip()))

    table_at = find("comes after the table")[0]
    footnote_at = find("trails the footnote reference")[1] - 1
    image_at = find("This sentence comes immediately after")[0]

    _post(f"/{doc_id}:batchUpdate", token, {"requests": [
        {"createFootnote": {"location": {"index": footnote_at, "tabId": tid}}}]})
    _post(f"/{doc_id}:batchUpdate", token, {"requests": [
        {"insertTable": {"rows": 2, "columns": 2,
                         "location": {"index": table_at, "tabId": tid}}}]})
    _post(f"/{doc_id}:batchUpdate", token, {"requests": [
        {"insertInlineImage": {"location": {"index": image_at, "tabId": tid}, "uri": LOGO,
         "objectSize": {"height": {"magnitude": 30, "unit": "PT"},
                        "width": {"magnitude": 92, "unit": "PT"}}}}]})

    # Give the footnote a body so the reader has something to recover.
    d = _get(doc_id, token)
    fns = d["tabs"][0]["documentTab"].get("footnotes") or {}
    if fns:
        fid = next(iter(fns))
        _post(f"/{doc_id}:batchUpdate", token, {"requests": [{"insertText": {
            "location": {"index": 1, "segmentId": fid, "tabId": tid},
            "text": "Footnote body that exists only outside the body content."}}]})


def build(token: str) -> str:
    doc_id = create(token)
    decorate(doc_id, token)
    return doc_id
