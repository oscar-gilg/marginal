"""Fetch a document's figures so the commenter can actually look at them.

Without this the commenter is told a figure exists and that it cannot see it, which
is honest but weak: in the drafts this tool is for, a figure often carries the
result the surrounding prose is claiming. A reviewer who cannot see it can only ever
ask whether the claim is supported, never whether the figure supports it.

The bytes come from the Docs API's `contentUri` for each inline object. They are
sent alongside the Markdown, which carries a `[figure N]` marker at the position the
image occupies, so the model can tell which figure is which.

Two limits, because images are expensive and a document can hold many: a per-image
byte cap and a per-document count. Anything past either is left as a marker with no
image, exactly as before — a figure the model cannot see is the old behaviour, not a
failure.
"""

from __future__ import annotations

import base64
import urllib.request

TIMEOUT = 30
# Anthropic accepts these; anything else is passed over rather than guessed at.
MEDIA_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}


def fetch(figures: list[dict], limit: int, max_bytes: int) -> list[dict]:
    """Download up to `limit` figures. Returns [{'at', 'media_type', 'data'}].

    Failures are skipped rather than raised: a figure that will not download should
    cost its own image, not the whole review.
    """
    out: list[dict] = []
    for fig in figures:
        if len(out) >= limit:
            break
        uri = fig.get("uri")
        if not uri:
            continue
        try:
            with urllib.request.urlopen(uri, timeout=TIMEOUT) as r:
                media = (r.headers.get("content-type") or "").split(";")[0].strip()
                if media not in MEDIA_TYPES:
                    continue
                blob = r.read(max_bytes + 1)
        except Exception:
            continue
        if len(blob) > max_bytes:
            continue  # too large to be worth the tokens; the marker still stands
        out.append(
            {
                "at": fig.get("at"),
                "alt": fig.get("alt") or "",
                "media_type": media,
                "data": base64.b64encode(blob).decode(),
            }
        )
    return out


def caption(i: int, img: dict) -> str:
    """What figure `i` is called. Shared, so it is called that in both modes."""
    return f"[figure {i}]" + (f" ({img['alt']})" if img.get("alt") else "")


def save(images: list[dict], dest) -> list[dict]:
    """Write the images to `dest` as files. Returns [{'path', 'alt'}], in order.

    Agent mode's brief is a string handed to a CLI, so it cannot carry image blocks
    the way an API-mode request does. A coding agent can open a file, though, so
    the same bytes reach it by a different road — which is the only way the two
    modes can show the commenter the same document.
    """
    from pathlib import Path

    out: list[dict] = []
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    for i, img in enumerate(images, start=1):
        suffix = {"image/jpeg": ".jpg"}.get(img["media_type"], "." + img["media_type"].split("/")[-1])
        path = dest / f"figure-{i}{suffix}"
        path.write_bytes(base64.b64decode(img["data"]))
        out.append({"path": str(path), "alt": img.get("alt") or ""})
    return out


def blocks(images: list[dict]) -> list[dict]:
    """Content blocks for the images, in the Anthropic shape.

    One shape, chosen here; the OpenRouter transport rewrites it to the OpenAI
    `image_url` form on its way out. It has to happen there rather than here because
    `provider="auto"` may fall back mid-request, so the caller does not know which
    route will carry the message.
    """
    out: list[dict] = []
    for i, img in enumerate(images, start=1):
        out.append({"type": "text", "text": f"{caption(i, img)}, as it appears in the document:"})
        out.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": img["media_type"],
                    "data": img["data"],
                },
            }
        )
    return out
