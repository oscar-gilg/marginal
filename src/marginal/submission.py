"""Everything that happens to a comment after somebody has written it.

One class, both modes. The commenter — whichever it is — produces a quote and a
body and hands them over; from that point the two paths are the same code, which
is the only way "API mode and agent mode behave identically" can be a property
of the system rather than a thing two implementations happen to agree on.

The stages, in order, and why the order is this one:

    1. **anchor**       the ladder in `anchors`, ending in the model fallback
    2. **dedup**        one comment per passage
    3. **budget**       the cap is a cap, not a hint
    4. **critique**     an LLM edits the comment; the only stage that is not local
    5. **attribution**  the header, so a reader knows what wrote it
    6. **place**        select, post, and later verify

Steps 1-3 are deterministic and cost nothing, so they run first: a comment that is
going to be rejected should be rejected before anybody pays a critic to polish it,
and the commenter should hear about it while it can still act. Step 4 is an LLM in
both modes — a Python rule can shorten a sentence but cannot tell which clause was
load bearing, so nothing here tries.

Where the modes differ is only in *who executes step 4*: API mode calls
`critique` on a worker thread; agent mode's submitter subagent runs the same
prompt before it ever calls `post-batch`. Steps 1-3 and 5-6 are literally the same
function calls either way.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from . import anchors, attribution, critic, ledger
from . import post as post_mod
from .config import Config
from .docs_ui import SpanError


@dataclass
class Verdict:
    """The answer to "can this comment be placed?", phrased so it can be acted on.

    `reason` is written for a model holding the document, because in both modes
    that is who reads it: the commenter in API mode, the submitter subagent in
    agent mode. "not found" sends either of them guessing.
    """

    ok: bool
    quote: str = ""
    reason: str = ""
    rung: str = ""


class Submitter:
    """The downstream half of a run. One per run, shared by both modes.

    Holds the state that has to be shared across comments — what has already been
    anchored, how much of the budget is left — because that state is exactly what
    used to live in two places and disagree. `vet` deduplicated and capped; the
    sequential reviewer did neither, so an API-mode run could anchor two comments
    to one passage and an agent-mode run could not.
    """

    def __init__(
        self,
        doc_text: str,
        cfg: Config,
        label: str,
        budget: int | None = None,
        usage=None,
        notes: list[str] | None = None,
        reconcile=None,
        fresh_tab=None,
    ):
        self.doc_text = doc_text
        self.cfg = cfg
        self.label = label
        self.budget = budget
        self.usage = usage
        self.notes = notes if notes is not None else []
        self.reconcile = reconcile
        self.fresh_tab = fresh_tab
        self.rungs: list[str] = []
        self._seen: set[str] = set()
        self._accepted = 0
        # `accept` runs on the generator thread and `critique`/`place` on workers,
        # but `notes` is appended from all of them.
        self._lock = threading.Lock()

    # ---- 1-3: local, deterministic, free -------------------------------------

    def accept(self, quote: str, comment: str) -> Verdict:
        """Anchor, deduplicate and budget-check one proposal. No network.

        Deliberately says nothing about whether the comment is any good. That
        judgement belongs to whoever wrote it, which is the only party that has read
        the whole document.
        """
        quote = (quote or "").strip()
        comment = (comment or "").strip()
        if not quote or not comment:
            return Verdict(False, reason="a submission needs both a quote and a comment.")
        with self._lock:
            if self.budget is not None and self._accepted >= self.budget:
                return Verdict(
                    False,
                    reason=f"the budget of {self.budget} comments is already used.",
                )
        try:
            span, rung = anchors.resolve(self.doc_text, quote, reconcile=self.reconcile)
        except SpanError:
            return Verdict(False, reason=anchors.explain(self.doc_text, quote))
        with self._lock:
            if span.quote in self._seen:
                return Verdict(
                    False,
                    reason=(
                        f"{span.quote[:50]!r} already has a comment on it. Anchor to a "
                        "different passage, or drop this one."
                    ),
                )
            self._seen.add(span.quote)
            self._accepted += 1
            self.rungs.append(rung)
        return Verdict(True, quote=span.quote, rung=rung)

    # ---- 4-5: the editing pass -----------------------------------------------

    def critique(self, quote: str, body: str) -> str:
        """Shorten the comment with a model, then attach the attribution header.

        Every posted comment passes through here, by one of two routes: `_edit` on
        the pipeline path, or `_post` on the agent-mode path. Agent mode
        used to reach it by a third — a subagent instructed to tighten before
        calling `post-batch` — which meant a caller who skipped the instruction
        skipped the editing pass with no error and no symptom.

        An editing outage costs the polish, never the comment — and *here* is why
        that sentence is now true. `critic.tighten` already falls back internally,
        so this catches only what gets past it, but the callers above used to answer
        that for themselves and answered differently: `_post` posted the comment
        unedited, `_async` recorded a failed result and dropped it, and `_sync` had
        no handler at all and took the whole run down. One event, three outcomes,
        chosen by a schedule that is supposed to change only what overlaps what.

        The draft is what makes the choice: only this frame still holds it, so this
        is the only place that *can* keep the comment. `_async`'s poster has a future
        and a quote and could not have posted the body even if it wanted to.

        The header is deliberately outside the guard. A `header` that will not format
        is a configuration error that fails on every comment identically, not an
        outage costing one of them, and it should be loud.
        """
        if self.cfg.critic:
            try:
                body, cut = critic.tighten(body, quote, self.cfg, self.usage)
            except Exception as e:
                self.note(f"editing stage failed on {quote[:40]!r}: {type(e).__name__}: {e}")
                return self.stamp(body)
            if cut and cut != "nothing":
                self.note(f"tightened {quote[:40]!r}: {cut}")
        return self.stamp(body)

    def stamp(self, body: str) -> str:
        """The attribution header. Applied in one place so both modes carry one."""
        return attribution.apply(body, self.label, self.cfg.header)

    # ---- 6: the browser ------------------------------------------------------

    def place(self, page, tab: dict, quote: str, body: str):
        """Select the span and post. `ok` stays provisional until `verify`."""
        return post_mod.post_one(
            page,
            tab,
            quote,
            body,
            self.cfg.strategy,
            fresh_tab=self.fresh_tab,
        )

    def record(self, doc_id: str, result) -> None:
        ledger.record(doc_id, result.comment_id, result.quote, result.body, self.label)

    def note(self, text: str) -> None:
        with self._lock:
            self.notes.append(text)

    def summary(self) -> str:
        """How each anchor was placed, cheapest rung first.

        Reported because the model fallback is only healthy while it is rare. A
        rising share means something upstream changed — a new export quirk, an
        escaping rule — and without this it looks like everything still working,
        only slower and more expensive.
        """
        if not self.rungs:
            return "none placed"
        order = ["verbatim", "stripped", "whitespace", "fuzzy", "model"]
        counts = {k: self.rungs.count(k) for k in order if self.rungs.count(k)}
        return ", ".join(f"{n} {k}" for k, n in counts.items())
