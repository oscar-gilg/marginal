"""The tighten-only editing pass over a finished comment.

One place, two callers. In API mode a worker thread calls `tighten` directly; in
agent mode `run.context` hands the coding agent the same prompt and the same
contract to run in a subagent. If the two drifted apart, a comment would be edited
differently depending on which produced it, and comparing the two would
stop meaning anything.

The critic may only shorten. It never decides whether a comment was worth making —
that judgement stays with the generator, which is the only stage that can see the
whole document. A critic that could also reject would need the document to reject
fairly, and the whole point of this pass is that it is cheap and parallel.
"""

from __future__ import annotations

from . import model
from .config import Config

RECONCILE_SYSTEM = """
You are matching a quotation to the document it came from.

The reviewer read a Markdown rendering of the document and quoted from it. The
quotation must now be located in the document's own plain text, which has no
Markdown syntax: no asterisks, no heading marks, no table pipes, and no footnote
text. The two therefore differ, and the quotation may have no exact counterpart.

Return the shortest span of the PLAIN TEXT that identifies the same claim the
quotation points at. Copy it character for character from the plain text — it must
appear there exactly once. Prefer the author's own sentence over a table cell or a
footnote, since those may not exist in the plain text at all.

Return a JSON object with one key:

  "quote" — the span, copied verbatim from the plain text. Use null if the passage
genuinely is not present.
""".strip()


def reconcile_anchor(text: str, quote: str, cfg: Config, usage=None) -> str | None:
    """Ask a model where a Markdown quotation lives in the plain document.

    Reached only when every deterministic rung in `anchors.resolve` has failed —
    typically a quote taken from a pipe table or a footnote body, which have no
    counterpart in the document's character stream and so cannot be recovered by
    rewriting the string. Returns None rather than raising: a comment that cannot
    be placed is dropped, and dropping one is better than anchoring it wrongly.
    """
    prompt = (
        f"The quotation, taken from the Markdown:\n\n{quote}\n\n"
        f"The document's plain text:\n\n{text}"
    )
    try:
        raw = model.converse(
            RECONCILE_SYSTEM,
            [{"role": "user", "content": [{"type": "text", "text": prompt}]}],
            model=cfg.fallback_model,
            max_tokens=1024,
            provider=cfg.provider,
            effort=cfg.fallback_effort,
            cache=True,
            usage=usage,
        )
        out = model.extract_json_object(raw)
    except Exception:
        return None
    found = out.get("quote")
    return found.strip() if isinstance(found, str) and found.strip() else None


def tighten(
    body: str, anchored: str, cfg: Config, usage: model.Usage | None = None
) -> tuple[str, str]:
    """Edit one comment. Returns (body, what-was-cut).

    Falls back to the original body on any failure. A critic outage should cost a
    round of polish, not the comment — the generator's text is already postable,
    and losing it to a 529 from the editing pass would be absurd.
    """
    prompt = (
        f"The comment is anchored to this passage:\n\n{anchored}\n\n"
        f"The comment:\n\n{body}"
    )
    try:
        raw = model.converse(
            cfg.critique_system(),
            [{"role": "user", "content": [{"type": "text", "text": prompt}]}],
            model=cfg.critic_model,
            max_tokens=1024,
            provider=cfg.provider,
            effort=cfg.critic_effort,
            # The critique prompt is byte-identical on every comment and is the whole
            # of the request bar the comment itself, so it caches cleanly. This is the
            # one optimisation that pays the same in both modes: it does not depend on
            # comments being written in sequence, only on there being more than one.
            cache=True,
            usage=usage,
        )
        out = model.extract_json_object(raw)
    except Exception as e:
        return body, f"critic failed ({type(e).__name__}), kept original"
    edited = (out.get("body") or "").strip()
    if not edited:
        return body, "critic returned nothing, kept original"
    # A tighten-only pass that lengthened the comment did something other than what
    # it was asked. The original is the safer artifact.
    if len(edited.split()) > len(body.split()):
        return body, "critic lengthened the comment, kept original"
    return edited, (out.get("cut") or "").strip() or "nothing"
