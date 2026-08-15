"""What the commenter is given, as one ordered list of sections.

Both modes hand the commenter the same things in the same order: how to review,
how to submit, how many comments, what has already been said, the figures, and the
document. That list used to be spelled out twice — once as content blocks in
`reviewer.propose_stream`, once as a string in `run.context` — and every ingredient
added to one and forgotten in the other became a bug. Agent mode went without the
figures, then without the comment budget, then without the name of the editing
model, each discovered separately and each the same mistake.

So the list lives here once, and the two renderers are the only thing that differs:

    sections(...)  ->  [Section, ...]      the content, identical in both modes
    as_blocks(...) ->  API request         images inline, cache breakpoint placed
    as_text(...)   ->  stdout              images written to files, paths listed

The renderers differ because the destinations genuinely differ — one is an HTTPS
request body, the other is text a coding agent reads in a terminal — and not for
any reason to do with reviewing. A figure reaches the commenter in both; the bytes
travel by the only road each destination has. Everything above that line, including
which figures, their numbering and their captions, is shared code.

Adding something the commenter should see means adding a `Section` here. Both
renderers pick it up, and `test_mode_parity` fails if one of them drops it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import figures, structure
from .config import Config


@dataclass
class Section:
    """One part of the brief.

    `system` marks the parts that are instructions rather than material. API mode
    puts those in the system prompt, where they cache separately from the document;
    agent mode has one channel and simply concatenates. That is a fact about the
    Messages API, not about what the commenter is told.
    """

    name: str
    text: str
    images: list[dict] = field(default_factory=list)
    system: bool = False


def document_text(doc_title: str, doc_text: str, tab: dict | None) -> str:
    """What the commenter reads: Markdown when available, else text plus its shape.

    Markdown is strictly better to read — headings, list markers and pipe tables all
    survive, and footnote bodies appear inline — so the structural summary is only
    assembled for sources that cannot produce it. Either way the commenter never
    sees the anchor space directly; `anchors.resolve` maps what it quotes back.
    """
    md = (tab or {}).get("markdown")
    if md:
        return f"--- {doc_title} (Markdown) ---\n{md}"
    shape = structure.describe(tab) if tab else ""
    head = f"{shape}\n\n" if shape else ""
    return f"{head}--- {doc_title} ---\n{doc_text}"


# What the commenter is told about searching, in both modes. The mechanism differs
# — a server-side tool in API mode, the coding agent's own search in agent mode —
# but when a search is worth making is a judgement about reviewing, so it is said
# once, here, and both renderers carry it.
RESEARCH = """
# Looking things up

You can search the web. Use it when the document rests on an external fact you can
check and the comment turns on whether that fact holds: a cited result, a claim
about prior work, a number attributed to someone else, a standard or API the
argument assumes behaves a certain way.

Do not search to find out whether the field agrees with the author, and do not
turn a comment into a literature review. A comment is still about this document's
reasoning; a search is only worth making when what you find would change the
comment you were already going to write.

Cite what you found in the comment itself, briefly and in your own words, so the
author can check it. Do not paste URLs as a substitute for making the point.
""".strip()


FIGURE_PREAMBLE = (
    "# Figures\n\n"
    "The document marks each figure `[figure N]` where it sits in the text. Look at "
    "them before commenting on anything the surrounding prose claims about them."
)


def sections(
    doc_title: str,
    doc_text: str,
    tab: dict | None,
    cfg: Config,
    contract: str,
    handoff: str,
    budget: int,
    focus: str | None = None,
    prior: str = "",
) -> list[Section]:
    """The whole brief, in the order the commenter reads it.

    `handoff` is the one genuinely mode-specific sentence — "call this tool" against
    "run this command in a subagent" — and it is a parameter rather than a branch so
    that the difference is visible at the two call sites instead of buried here.
    """
    ask = f"Leave at most {budget} comments on this document."
    if focus:
        ask += f"\n\nFocus on: {focus}"

    out = [
        Section("instructions", cfg.commenter_system(), system=True),
        Section("contract", contract, system=True),
        Section("handoff", handoff, system=True),
    ]
    if cfg.web_search:
        out.append(Section("research", RESEARCH, system=True))
    out.append(Section("ask", ask))
    if prior:
        out.append(Section("prior", prior))
    # Last on purpose, in both modes: it is the bulk of the brief, and the
    # instructions are worth reading before the thing they apply to.
    return out + material(doc_title, doc_text, tab, cfg)


def material(doc_title: str, doc_text: str, tab: dict | None, cfg: Config) -> list[Section]:
    """The document and its figures: what the model reads rather than what it is told.

    Separate from the instructions because two jobs read it. The commenter gets it
    at the end of its brief; the responder gets it before the thread it is
    answering, so that a reply can check the passage around the one it commented on
    instead of guessing from sixty quoted characters.

    It is also the whole of what is worth caching — fixed for a document, identical
    across the threads answered in one run — which is why it is assembled in one
    place: a responder that built the document block its own way would differ by a
    byte and pay full price for every reply.
    """
    out = []
    imgs = _figures(tab, cfg)
    if imgs:
        out.append(Section("figures", FIGURE_PREAMBLE, images=imgs))
    out.append(Section("document", document_text(doc_title, doc_text, tab)))
    return out


def _figures(tab: dict | None, cfg: Config) -> list[dict]:
    if not (cfg.figures and tab and tab.get("figures")):
        return []
    try:
        return figures.fetch(tab["figures"], cfg.max_figures, cfg.max_figure_bytes)
    except Exception:
        # A figure that will not download costs its own image, not the review.
        return []


def as_blocks(parts: list[Section]) -> tuple[str, list[dict]]:
    """Render for the Messages API. Returns (system prompt, content blocks).

    The cache breakpoint goes on the final block: everything in this message is
    fixed for the whole run, so the entire brief is the stable prefix and the
    rolling marker in `reviewer._rolling_cache` handles the conversation after it.
    """
    system = "\n".join(s.text for s in parts if s.system)
    blocks: list[dict] = []
    for s in parts:
        if s.system:
            continue
        blocks.append({"type": "text", "text": s.text})
        blocks += figures.blocks(s.images)
    if blocks:
        blocks[-1]["cache_control"] = {"type": "ephemeral"}
    return system, blocks


def as_text(parts: list[Section], figure_dir) -> str:
    """Render for stdout. Images are written to `figure_dir` and listed by path.

    A coding agent opens a file; that is the whole of the difference from
    `as_blocks`. The captions and the numbering come from `figures`, so a figure is
    called the same thing in both modes.
    """
    chunks = []
    for s in parts:
        text = s.text
        if s.images:
            saved = figures.save(s.images, figure_dir)
            lines = [
                f"  {figures.caption(i, img)}  {img['path']}"
                for i, img in enumerate(saved, start=1)
            ]
            text = f"{text}\n\n" + "\n".join(lines)
        chunks.append(text)
    return "\n\n".join(c for c in chunks if c)
