"""Who wrote this comment.

The model posts through whichever Google account the browser is signed into —
usually the human's own. So a header naming the model does two jobs at once:

* collaborators can see at a glance that a comment is model-written, on a document
  where every comment otherwise carries the human's name; and
* ownership becomes a property of the comment itself. "Does the body start with our
  header" is deterministic, needs no second Google account, survives losing the
  ledger, and works from any machine — strictly better than matching reply text
  against a local file.

Set `header = ""` to disable, which is what you want when the point of a run is to
observe how people respond to comments without telling them the source.
"""

from __future__ import annotations

import re

DEFAULT_HEADER = "**{model}**"
SEPARATOR = "\n"

# Matches our own header at the start of a body, for any model name. Kept
# deliberately loose on the name so a body written under a different configured
# model is still recognised as ours.
# The bolded token must look like a model id: no spaces, and containing a digit or a
# hyphen. Without that, a human comment opening "**Note**\n" reads as ours, and
# `respond` would skip a human reply believing we had written it.
_HEADER_RE = re.compile(r"^\*\*(?=[^*\n]*[\d-])[\w.\-/]{1,60}\*\*\n")


def short(model: str) -> str:
    """`claude-fable-5` -> `fable-5`. The vendor prefix is noise in a Docs sidebar."""
    return model.removeprefix("claude-").removeprefix("anthropic/")


def apply(body: str, model: str, header: str = DEFAULT_HEADER) -> str:
    """Prefix `body` with the attribution header. No-op when header is empty."""
    if not header:
        return body
    rendered = header.format(model=short(model))
    if body.startswith(rendered + SEPARATOR):
        return body
    return f"{rendered}{SEPARATOR}{body}"


def strip(body: str) -> str:
    """The comment without its header — what the model actually wrote."""
    return _HEADER_RE.sub("", body, count=1)


def is_ours(body: str, header: str = DEFAULT_HEADER) -> bool:
    """Whether a comment body carries our attribution header.

    Returns False when headers are disabled, since then nothing distinguishes a
    model comment from a human one and the caller must fall back to the ledger.
    """
    if not header:
        return False
    if header == DEFAULT_HEADER:
        return bool(_HEADER_RE.match(body or ""))
    # A custom header is matched by its literal parts. One that *starts* with the
    # placeholder has no leading literal, so match on its first line rather than
    # returning False and silently losing ownership of our own comments.
    body = body or ""
    before, _, after = header.partition("{model}")
    if before:
        return body.startswith(before)
    if after:
        return after.strip() in body.split(SEPARATOR, 1)[0]
    return False
