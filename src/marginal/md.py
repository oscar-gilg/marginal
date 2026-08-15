"""Prepare the Markdown export for reading: strip the image payloads, split by tab.

Drive's Markdown export is what the commenter reads, but it arrives in two shapes
it cannot use.

**Images come inlined as base64.** A figure exports as `![][image1]` with a
definition at the foot of the file holding the whole file as a `data:` URI. Even
a small logo can outweigh all the prose around it, so a paper with eight real
figures would spend tens of thousands of tokens on bytes no model can read.
They are replaced by a named placeholder, which is also more use to a reviewer:
it says a figure is there and that its content is unavailable.

**The export covers the whole document, not one tab.** A run comments on a single
tab, so Markdown describing the others would invite quotes that cannot anchor. The
export is sliced per tab and the slice is then checked against that tab's own text;
a slice that fails the check is discarded rather than trusted.
"""

from __future__ import annotations

import re

# `[image3]: <data:image/png;base64,...>` — the payload, always its own line.
_IMAGE_DEF = re.compile(r"^\[image\d+\]:\s*<[^>]*>\s*$\n?", re.MULTILINE)
# `![alt][image3]` and `![alt](url)` — the reference in the prose.
_IMAGE_REF = re.compile(r"!\[([^\]]*)\]\(([^)]*)\)|!\[([^\]]*)\]\[([^\]]*)\]")


def sanitise(markdown: str) -> str:
    """Replace image payloads with placeholders. Returns Markdown, prose intact."""
    n = 0

    def placeholder(m: re.Match) -> str:
        nonlocal n
        n += 1
        alt = (m.group(1) or m.group(3) or "").strip()
        return f"[figure {n}: {alt}]" if alt else f"[figure {n}]"

    return _IMAGE_DEF.sub("", _IMAGE_REF.sub(placeholder, markdown)).strip()


def _distinctive(text: str, limit: int = 12) -> list[str]:
    """Lines long enough to identify a tab, longest first.

    Short lines recur across tabs — a heading like "Summary" proves nothing about
    which tab a slice came from.
    """
    lines = {ln.strip() for ln in text.split("\n") if len(ln.strip()) >= 40}
    return sorted(lines, key=len, reverse=True)[:limit]


_ESCAPE = re.compile(r"\\([.\-*_`#\[\]()+!|>~])")
_ANCHOR = re.compile(r"\{#[^}]*\}")
_MARKS = re.compile(r"\*\*|__|\*|_|`|#")


def _norm(s: str) -> str:
    """Plain words, for comparing a Markdown slice against a tab's own text.

    The two never match literally: the export escapes punctuation (`\\~40%`), bolds
    headings, and appends `{#slug}` anchors. Comparing raw made every tab on a
    many-tab document fail its own coverage check and fall back to plain text —
    the boundaries had been right all along.
    """
    s = _ANCHOR.sub("", s)
    s = _ESCAPE.sub(r"\1", s)
    s = _MARKS.sub("", s)
    return " ".join(s.split())


def _covers(slice_: str, text: str) -> float:
    """Share of a tab's distinctive lines present in a candidate slice."""
    lines = _distinctive(text)
    if not lines:
        return 0.0
    hay = _norm(slice_)
    return sum(1 for ln in lines if _norm(ln) in hay) / len(lines)


_HEADING = re.compile(r"^(#{1,6})[ \t]+(.*)$", re.MULTILINE)
_TRAILING_ANCHOR = re.compile(r"\s*\{#[^}]*\}?\s*$")


def _heading_text(raw: str) -> str:
    """The words of a heading, with the export's decorations removed.

    Three of them, all seen on one document: a nested tab renders its label as
    `## # One-pager`, so leading hashes can survive into the text; headings carry
    bold markers; and some carry a `{#slug}` anchor suffix.
    """
    text = _TRAILING_ANCHOR.sub("", raw).strip()
    text = re.sub(r"^(#+\s*)+", "", text)
    return text.replace("**", "").replace("__", "").strip()


def _title_positions(markdown: str, titles: list[str]) -> list[int | None]:
    """Where each tab's label sits in the export, or None where it cannot be found.

    Tabs export in document order, so the labels are matched by walking the export's
    headings once and consuming titles in order. Missing one is survivable: it costs
    that tab its Markdown, not the whole document's — requiring all of them meant a
    single oddly-rendered label (`## # One-pager`) discarded the split for every
    tab.
    """
    wanted = [(_heading_text(t or ""), i) for i, t in enumerate(titles)]
    found: list[int | None] = [None] * len(titles)
    nxt = 0
    for m in _HEADING.finditer(markdown):
        if nxt >= len(wanted):
            break
        text = _heading_text(m.group(2))
        target, index = wanted[nxt]
        if target and text == target:
            found[index] = m.start()
            nxt += 1
    return found


def _title_bounds(markdown: str, titles: list[str]) -> list[tuple[int, int]] | None:
    """Start and end of each tab's region, from the labels that could be located.

    A tab whose own label was not found gets no bounds. A tab whose label was found
    runs to the next *located* label, so a missing one widens its predecessor rather
    than truncating it — the content check downstream is what catches a slice that
    grew too far.
    """
    pos = _title_positions(markdown, titles)
    if not any(p is not None for p in pos):
        return None
    bounds: list[tuple[int, int] | None] = []
    for i, start in enumerate(pos):
        if start is None:
            bounds.append(None)
            continue
        # Begin after the label's own line. The label is the tab's name, not
        # document text, so a quote taken from it could never be anchored — and an
        # empty tab would otherwise come back as nothing but its own heading.
        line_end = markdown.find("\n", start)
        body = line_end + 1 if line_end != -1 else len(markdown)
        later = [p for p in pos[i + 1:] if p is not None]
        bounds.append((body, later[0] if later else len(markdown)))
    return bounds


def for_tab(markdown: str, tabs: list[dict], index: int, floor: float = 0.8) -> str | None:
    """The part of the export belonging to tab `index`, or None if unsure.

    Whatever locates the boundaries, the slice is checked against the tab's own
    text before it is returned: it must contain most of this tab's distinctive
    lines and not be mostly another tab's. Reading one tab while anchoring into
    another is worse than reading the plain text, so an unconfirmed slice is
    discarded.
    """
    if not markdown or not tabs:
        return None
    texts = [t.get("text") or "" for t in tabs]
    if len(tabs) == 1:
        return sanitise(markdown) if _covers(markdown, texts[0]) >= floor else None

    bounds = _title_bounds(markdown, [t.get("title") or "" for t in tabs])
    if bounds is not None and bounds[index] is not None:
        start, end = bounds[index]
    else:
        bounds = None  # this tab's label was not located; fall back for it alone
        # Fall back to finding where this tab's own prose starts.
        def first_hit(text: str, after: int) -> int | None:
            hits = [markdown.find(ln, after) for ln in _distinctive(text)]
            hits = [h for h in hits if h != -1]
            return min(hits) if hits else None

        start = first_hit(texts[index], 0)
        if start is None:
            return None
        end = len(markdown)
        for later in range(index + 1, len(tabs)):
            at = first_hit(texts[later], start + 1)
            if at is not None:
                end = at
                break

    candidate = sanitise(markdown[start:end])
    if not candidate:
        # An empty tab has nothing to read. Returning "" would rely on the caller
        # treating it as falsy; None says outright that there is no Markdown here.
        return None
    # A tab with no long lines cannot be confirmed by content. Trust it only when an
    # exact title boundary put it there.
    if not _distinctive(texts[index]):
        return candidate if bounds is not None else None

    if bounds is not None:
        # The region is exact: it runs from this tab's own label to the next one.
        # The check is only a guard against having matched the wrong heading, so it
        # is loose — an export reformats enough of a tab's prose (escapes, bold,
        # anchors, rewrapping) that demanding most lines back is demanding too much.
        #
        # No cross-tab check here. Tabs legitimately share text: a child tab that
        # condenses its parent scored as high against the parent's slice as the
        # parent did, and rejecting on that discarded a correctly-bounded region.
        return candidate if _covers(candidate, texts[index]) >= 0.4 else None

    # Content-matched bounds are a guess, so they are held to the strict floor and
    # must not look mostly like some other tab.
    if _covers(candidate, texts[index]) < floor:
        return None
    for other, text in enumerate(texts):
        if other != index and _covers(candidate, text) > 0.5:
            return None
    return candidate
