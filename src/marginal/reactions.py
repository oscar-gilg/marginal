"""Emoji reactions on comments: the one signal no Google API returns.

Reactions live in the Docs *discussion* model. The Drive Comment resource has no
reactions field and neither does Reply, so `gdocs.list_comments` cannot see them
however it is asked.

They are in the docx export, written as trailing paragraphs inside the comment
they belong to:

    Does the pilot's response rate hold once the sample is district-wide?
    1 total reaction
    Ada Lovelace reacted with 😬 at 2026-01-02 5:43 PM UTC

So the whole of this module is text: `split_body` takes that trailer off, and
`attach` hangs it on the API's comments. The export is the same one
`browser_source` already downloads to read the document and verify anchors, which
is why reading reactions costs nothing new.

There was briefly a second route: scraping the rendered comment panel for the
emoji chips. It worked, and it is kept only as a note here in case a document ever
forbids its own export, but it was strictly worse — the reader's own vote renders
as the literal "You" rather than a name, it carries no timestamp, and it depends on
a Docs class name and on scrolling a virtualised list. Two routes to one fact
drift; this is the one to keep.

Reactions are read, never written: posting one would put this tool's opinion into
data collected about the tool's comments.
"""

from __future__ import annotations

import re

from . import attribution

# The trailer, as the export writes it.
_TOTAL_RE = re.compile(r"^\d+ total reactions?$", re.I)
_REACTED_RE = re.compile(r"^(?P<who>.+?) reacted with (?P<emoji>\S+) at (?P<at>.+?)\.?$", re.I)

# Below this length a containment join stops meaning anything: "+1" and "agreed"
# appear inside half the comments on a document. Such a post goes unjoined and is
# reported as unmatched rather than attached to a comment it might not belong to.
MIN_JOIN = 15


def split_body(text: str) -> tuple[str, list[dict]]:
    """A docx comment's text into `(body, reactions)`.

    The trailer is part of the comment's text in the export, so a caller that does
    not split it sees a comment whose body changed the moment somebody reacted to
    it. `post.verify` compares bodies for exact equality, which made a reacted
    comment read back as "posted comment could not be read back" — a live,
    correctly anchored comment reported as a failure.
    """
    lines = (text or "").split("\n")
    cut = next((i for i, ln in enumerate(lines) if _TOTAL_RE.match(ln.strip())), None)
    if cut is None:
        return text, []
    out = []
    for line in lines[cut + 1:]:
        m = _REACTED_RE.match(line.strip())
        if m:
            out.append({
                "emoji": m.group("emoji"),
                "count": 1,
                "voters": [m.group("who").strip()],
                "at": m.group("at").strip(),
            })
    # Only a trailer if it parsed as one. A comment whose own last line happens to
    # read "3 total reactions" would otherwise lose that line.
    if not out:
        return text, []
    return "\n".join(lines[:cut]).strip(), out


def from_export(page, doc_id: str) -> list[dict]:
    """Every reacted comment in the document, as `[{text, reactions}]`."""
    from . import browser_source

    return [
        {"text": c["body"], "reactions": c["reactions"]}
        for c in browser_source.read_comments(page, doc_id)
        if c.get("reactions")
    ]


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip().lower()


def _body(obj: dict) -> str:
    """A comment's text, normalised and without our attribution header.

    `content` is the Drive API's field, `body` the docx reader's. Both are comment
    bodies and both reach this. The header comes off because the export and the API
    do not agree on it, and every comment this tool posts carries one — joining on
    the raw text failed on precisely the comments reactions are collected about,
    and failed as "no reactions", which reads as a document nobody reacted to.
    """
    return _norm(attribution.strip(obj.get("content") or obj.get("body") or ""))


def attach(comments: list[dict], posts: list[dict]) -> list[str]:
    """Hang each reacted post on the comment or reply it belongs to, by its text.

    Mutates `comments`, adding a `reactions` list to whichever object carries one.
    Returns the posts that matched nothing, which is a real outcome to report
    rather than swallow: an unjoined reaction is one the caller can see in the
    document and will not find in the output.
    """
    unmatched = []
    for post in posts:
        card = _norm(post["text"])
        hits = [
            obj
            for c in comments
            for obj in [c] + list(c.get("replies") or [])
            if len(_body(obj)) >= MIN_JOIN and _body(obj) in card
        ]
        # Exactly one, or none. A card whose text contains two bodies identifies
        # neither, and a tie-break here would be the same mistake as one in
        # `anchors.resolve`: a plausible guess, silently wrong, indistinguishable in
        # the output from a correct answer.
        if len(hits) != 1:
            why = "no comment" if not hits else f"{len(hits)} comments"
            unmatched.append(f"{post['text'][:80]} — matched {why}")
            continue
        hits[0].setdefault("reactions", []).extend(post["reactions"])
    return unmatched
