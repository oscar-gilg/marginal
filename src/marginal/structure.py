"""Describe a document's shape to the model, without disturbing its text.

The reader already works out which paragraphs are headings, which are bullets and
which sit inside a table — and then threw all of it away, sending only the flat
character stream. So a heading arrived as a one-word sentence, a table arrived as a
column of orphaned cells, and figures and footnotes did not arrive at all.

Two constraints shape what this can do. `text` is the coordinate space anchors
resolve in, so nothing here may insert a character into it — the shape is described
alongside the document rather than marked up inside it. And the browser source
produces no style information at all, so every field is optional and an empty
description renders as nothing rather than as a misleading claim about a flat
document.
"""

from __future__ import annotations

_HEADINGS = ("TITLE", "SUBTITLE", "HEADING_")


def _is_heading(style: str) -> bool:
    return style.startswith(_HEADINGS)


def _label(style: str) -> str:
    if style.startswith("HEADING_"):
        return "H" + style.removeprefix("HEADING_")
    return style.title()


def _bullet_runs(paragraphs: list[dict]) -> list[tuple[int, int, int]]:
    """Consecutive bulleted paragraphs, as (start, end, count).

    Grouped because a nine-item list is one structural fact, and nine separate
    lines would crowd out everything else in the description.
    """
    runs: list[list[int]] = []
    for p in paragraphs:
        if "bullet" not in p:
            continue
        if runs and runs[-1][1] == p["start"]:
            runs[-1][1] = p["end"]
            runs[-1][2] += 1
        else:
            runs.append([p["start"], p["end"], 1])
    return [(a, b, n) for a, b, n in runs]


def describe(tab: dict) -> str:
    """A short structural summary to sit above the document text. "" if flat.

    Character ranges are included so the model can tell which part of the text each
    element covers — it cannot see the formatting, so the offsets are the only way
    to connect "H1 Results" to the word "Results" in the stream below.
    """
    paragraphs = tab.get("paragraphs") or []
    lines: list[str] = []

    marks: list[tuple[int, str]] = []
    for p in paragraphs:
        if _is_heading(p.get("style", "NORMAL_TEXT")):
            head = tab["text"][p["start"] : p["end"]].strip()
            # A whole paragraph carrying a heading style is a formatting slip in the
            # document, not a heading. Left whole it would crowd out every other line
            # in the summary, so it is named rather than reproduced.
            if len(head) > 90:
                head = head[:87].rstrip() + "..."
            marks.append((p["start"], f'{_label(p["style"]):<4} "{head}"'))
    for start, end, n in _bullet_runs(paragraphs):
        marks.append((start, f"bullet list ({n} item{'s' if n != 1 else ''})"))
    for t in tab.get("tables") or []:
        marks.append((t["start"], f"table {t['rows']} rows x {t['cols']} cols"))

    if marks:
        lines.append("# Document structure")
        lines.append("")
        lines.append(
            "The shape of the document below. Character ranges say which part of the "
            "text each element covers; you cannot see the formatting itself. Quote "
            "only from the document text, never from this summary."
        )
        lines.append("")
        for start, what in sorted(marks):
            lines.append(f"  {what}  (from char {start})")

    figures = tab.get("figures") or []
    if figures:
        many = len(figures) != 1
        lines += [
            "",
            "# Figures",
            "",
            f"The document contains {len(figures)} inline image{'s' if many else ''}, "
            f"at char {', '.join(str(f['at']) for f in figures)}. "
            f"{'Their' if many else 'Its'} content is not available to you. Do not "
            "treat a claim as unsupported, or ask for evidence that is plainly meant "
            "to be in a figure, on the basis that you cannot see it.",
        ]

    notes = [f for f in (tab.get("footnotes") or []) if f.get("text")]
    if notes:
        lines += ["", "# Footnotes", "", "Referenced from the text, in order:"]
        for f in notes:
            lines.append(f"  [{f['number']}] (at char {f['at']}) {f['text']}")

    return "\n".join(lines).strip()
