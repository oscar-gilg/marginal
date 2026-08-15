"""Turn a quotation the model copied out of Markdown into a span of the document.

The reviewer reads Markdown, because that is the only form in which headings stay
headings and a table stays a table. Comments must anchor to the *document*, whose
character stream has none of that syntax — so every quote makes one trip back.

The trip is a ladder, cheapest first, and the cheap rungs carry almost all of it:

    1. the quote already matches                  plain prose, the common case
    2. Markdown syntax stripped                   emphasis, headings, list markers
    3. whitespace made flexible                   Markdown rewraps; the doc did not
    4. fuzzy window                               stray character, smart quote
    5. ask a model                                last resort, and usually skipped

Rungs 1-4 are pure string work: no network, no tokens, and deterministic, which is
what you want deciding where a comment lands. Rung 5 exists because the failures
that reach it — a quote from a pipe table, or from a footnote body that has no
counterpart in the document at all — cannot be repaired by rewriting the string.

Whatever comes back is a slice of the document's own text, never of the Markdown.
Anything else would desynchronise selection from what the editor holds.
"""

from __future__ import annotations

import difflib
import re

from .docs_ui import Span, SpanError, resolve_quote

# Google's Markdown export escapes punctuation that could be read as syntax — a
# footnote ending "in Table 2." comes back as "in Table 2\.".
_ESCAPE = re.compile(r"\\([.\-*_`#\[\]()+!|>~])")
_FOOTNOTE_REF = re.compile(r"\[\^[^\]]*\]")
# Runs of emphasis markers, decided one run at a time. A marker between two
# alphanumerics is a literal the document contains — `foo_bar` is an identifier, not
# italics — and removing it produced `foobar`, which then anchored to an unrelated
# passage and verified as exact because the anchor matched what was selected.
_MARKER_RUN = re.compile(r"[*_`]+")
_LEADING_MARK = re.compile(r"^\s*(#{1,6}\s+|[-*+]\s+|\d+\.\s+|>\s+)")
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
# How far the best fuzzy candidate must beat the next best to count as identifying
# one passage rather than winning a coin toss.
_FUZZY_MARGIN = 0.03


def _drop_markers(q: str) -> str:
    """Remove emphasis delimiters, keep the ones that are part of a word."""

    def decide(m: re.Match) -> str:
        before = q[m.start() - 1] if m.start() else ""
        after = q[m.end()] if m.end() < len(q) else ""
        return m.group() if before.isalnum() and after.isalnum() else ""

    return _MARKER_RUN.sub(decide, q)


def to_plain(quote: str) -> str:
    """Strip Markdown syntax, keeping the words the document actually contains."""
    q = _FOOTNOTE_REF.sub("", quote)
    q = _LINK.sub(r"\1", q)  # keep the link text, drop the target
    q = _LEADING_MARK.sub("", q)
    q = _drop_markers(q)
    q = _ESCAPE.sub(r"\1", q)
    return q.strip()


def _flexible(text: str, quote: str) -> Span | None:
    """Match with whitespace treated as elastic.

    Markdown wraps and indents on its own terms — a quote spanning a line break in
    the export can carry a newline the document writes as a space. Matching the
    words rigidly and the gaps loosely recovers those without loosening what counts
    as a match.
    """
    words = [re.escape(w) for w in quote.split()]
    if not words:
        return None
    found = list(re.finditer(r"\s+".join(words), text))
    if len(found) != 1:  # ambiguous is as bad as absent — see resolve_quote
        return None
    m = found[0]
    return Span(m.start(), m.end(), text[m.start() : m.end()])


def _fuzzy(text: str, quote: str, floor: float = 0.9) -> Span | None:
    """Best near-match, when a character differs but the passage is unmistakable.

    Scoped to the paragraph that looks most like the quote rather than the whole
    document, so a high ratio means "this passage, slightly mistyped" and not "some
    passage elsewhere that happens to score well".
    """
    if len(quote) < 20:  # too short to be confident, and 20 is the anchor minimum
        return None
    paras, pos = [], 0
    for line in text.split("\n"):
        paras.append((pos, line))
        pos += len(line) + 1
    best, ratio, runner_up = None, floor, 0.0
    for start, line in paras:
        if len(line) < len(quote) // 2:
            continue
        # Span from the first matching run to the last, rather than the longest
        # single run: one wrong character in the middle halves the longest run and
        # would send an otherwise unmistakable quote to the model.
        blocks = [b for b in difflib.SequenceMatcher(None, line, quote).get_matching_blocks() if b.size]
        if not blocks:
            continue
        a0, a1 = blocks[0].a, blocks[-1].a + blocks[-1].size
        cand = line[a0:a1]
        r = difflib.SequenceMatcher(None, cand, quote).ratio()
        if r > ratio:
            best, ratio, runner_up = Span(start + a0, start + a1, cand), r, ratio
        elif r > runner_up:
            runner_up = r
    # A near-tie does not identify one place, and the winner would otherwise be
    # whichever came first in the document. A margin rather than exact equality:
    # 0.980 against 0.971 is two paragraphs the quote resembles almost equally, and
    # the ratios need not collide for the choice between them to be a coin toss.
    # Rejecting is cheap here — an unambiguous quote matched on an earlier rung —
    # and a guess is a comment on the wrong paragraph that still verifies as exact.
    if best is not None and runner_up and ratio - runner_up < _FUZZY_MARGIN:
        return None
    return best


def explain(text: str, quote: str) -> str:
    """Why a quote could not be placed, phrased so it can be acted on.

    The submitter is a model with the document in front of it, so a rejection is
    only useful if it says what to do next. "not found" sends it guessing; "the
    nearest line is X" lets it correct the quote in one turn.
    """
    plain = to_plain(quote)
    if not plain:
        return f"{quote[:60]!r} contains no words once Markdown syntax is removed."
    n = text.count(plain)
    if n > 1:
        return (
            f"{plain[:60]!r} appears {n} times in the document, so it does not "
            "identify one place. Extend it with the words either side until it is "
            "unique."
        )
    near = _fuzzy(text, plain, floor=0.6)
    if near is not None:
        return (
            f"{plain[:60]!r} is not in the document. The closest passage is "
            f"{near.quote[:90]!r} — quote that instead, copied exactly."
        )
    return (
        f"{plain[:60]!r} is not in the document. It may come from a table cell, a "
        "footnote, or a heading that the document does not contain as text. Quote "
        "the author's own sentence nearest the point you are making."
    )


def resolve(text: str, quote: str, reconcile=None) -> tuple[Span, str]:
    """Locate `quote` in the document text. Returns (span, which rung matched).

    `reconcile` is called only if every deterministic rung fails; omit it and this
    raises instead, which is what the reviewer's own retry loop wants — there the
    model is mid-conversation and can simply re-anchor for free.
    """
    for how, candidate in (("verbatim", quote), ("stripped", to_plain(quote))):
        if not candidate:
            continue
        try:
            return resolve_quote(text, candidate), how
        except SpanError:
            pass
    plain = to_plain(quote)
    for how, span in (("whitespace", _flexible(text, plain)), ("fuzzy", _fuzzy(text, plain))):
        if span is not None:
            return span, how
    if reconcile is not None:
        fixed = reconcile(text, quote)
        if fixed:
            # Trust nothing the model returns: it has to resolve like any other
            # quote, or the comment would anchor somewhere it was never asked to.
            return resolve_quote(text, fixed), "model"
    raise SpanError(f"could not place quote in the document: {quote[:60]!r}")
