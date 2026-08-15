"""A record of every comment this tool posted.

Needed for two things that both matter. `unpost` can only delete what it knows it
created. And because the model posts under the signed-in human's account, author
identity cannot distinguish a model comment from a human one — the ledger is the
only way to know which threads are ours to answer.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

# Absolute, and overridable. A path relative to the process working directory
# creates a different ledger per invocation directory, which silently loses which
# threads are ours — the one thing the ledger exists to know.
def _default_ledger() -> Path:
    """Repo-local in a checkout, user data dir once installed.

    `parents[2]` used to be assumed the repo; installed, that wrote into
    `site-packages`. A checkout keeps `runs/ledger.jsonl` so existing history and
    the tests that read it stay put.
    """
    from .config import source_checkout

    root = source_checkout()
    if root is not None:
        return root / "runs" / "ledger.jsonl"
    data_home = os.environ.get("XDG_DATA_HOME")
    base = Path(data_home) if data_home else Path.home() / ".local" / "share"
    return base / "marginal" / "ledger.jsonl"


LEDGER = Path(os.environ.get("MARGINAL_LEDGER") or _default_ledger())


def record(
    doc_id: str,
    comment_id: str,
    quote: str,
    body: str,
    model_id: str,
    kind: str = "comment",
    path: Path = LEDGER,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
        "doc_id": doc_id,
        "comment_id": comment_id,
        "quote": quote,
        "body": body,
        "model": model_id,
        "kind": kind,
    }
    with path.open("a") as f:
        f.write(json.dumps(row) + "\n")


def rows(doc_id: str | None = None, path: Path = LEDGER) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if doc_id is None or row["doc_id"] == doc_id:
            out.append(row)
    return out


def our_comment_ids(doc_id: str, path: Path = LEDGER) -> set[str]:
    return {r["comment_id"] for r in rows(doc_id, path) if r["kind"] == "comment"}
