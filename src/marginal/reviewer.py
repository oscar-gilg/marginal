"""The model-facing half: decide what to comment on, and how to answer replies.

The prompts live in `prompts/` and are selected by config, because the prompt is
what people will want to change and better ones already exist elsewhere. Only the
output contract below belongs to the code — it is appended at request time so a
vendored prompt file can stay a faithful copy of its original.
"""

from __future__ import annotations

from . import brief, model
from .config import Config

# What the commenter is asked for, in **both** modes. There used to be two of
# these and they had drifted: the API-mode contract made the commenter count
# characters, guarantee uniqueness and re-anchor its own rejections, while the
# agent-mode one asked for none of it. A comment therefore depended on which mode
# wrote it, which is precisely what must not be true.
#
# Everything clerical is gone on purpose. The commenter is the only party that has
# read the whole document, so its attention belongs on the document; counting,
# checking and correcting are the submitter's job in agent mode and the scaffold's
# in API mode, and both of those are downstream of this prompt.
COMMENTER_CONTRACT = """
# Submitting your comments

Write **one comment at a time** and submit each one as soon as it is written, then
carry straight on to the next while it is being placed. For each, give:

  quote   — the sentence or clause your comment is about, copied from the document.
Copy rather than paraphrase, but do not count characters and do not check whether
it is unique: whoever places it will widen or correct it. Quote the author's own
prose, not a table cell or a footnote.
  comment — the comment body, plain text. Say the thing; do not trim it for length
or polish it. A separate editing pass handles that.

You will be told if a quote could not be placed, and the reply will say what to
quote instead. Nothing else comes back — a submission that draws no complaint has
been placed.

You can see the comments you have already made. Do not repeat a point, and do not
comment twice on one passage.

Stop as soon as you have nothing further worth saying. You are not expected to
reach the limit, and stopping at two good comments is a better outcome than
padding to five.
"""

# Kept as an alias: agent mode's brief renders this same text, and calling it by a
# name that says "agent" invited the two to diverge again.
AGENT_CONTRACT = COMMENTER_CONTRACT

# The tool the commenter submits through. The scaffold behind it does the anchoring,
# the deduplication, the budget, the editing pass and the posting — see
# `submission.Submitter`. Agent mode's equivalent is handing the pair to a subagent;
# the commenter's experience is meant to be the same either way, which is why the
# description says nothing the agent-mode brief does not also say.
SUBMIT_TOOL = {
    "name": "submit_comment",
    "description": (
        "Submit one finished comment. It will be edited for length, anchored to the "
        "document and posted. Returns nothing if it was placed, or what to quote "
        "instead if the passage could not be found."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "quote": {
                "type": "string",
                "description": "The passage the comment is about, copied from the document.",
            },
            "comment": {"type": "string", "description": "The comment body, plain text."},
        },
        "required": ["quote", "comment"],
    },
}

# The suggestion tool, offered only when `cfg.suggestions` is on. A second tool
# rather than a `kind` field on `SUBMIT_TOOL`: the two have different required
# fields, and leaving the comment tool byte-identical means a run with
# suggestions off is indistinguishable from one before the feature existed.
SUGGEST_TOOL = {
    "name": "submit_suggestion",
    "description": (
        "Propose one suggested edit: a tracked change replacing the quoted passage. "
        "The quote must match the document exactly; the replacement is applied "
        "verbatim. Returns nothing if it was accepted, or what to fix if not."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "quote": {
                "type": "string",
                "description": (
                    "The exact passage to replace, copied character for character."
                ),
            },
            "replacement": {
                "type": "string",
                "description": "The full text that replaces the quote, final wording.",
            },
        },
        "required": ["quote", "replacement"],
    },
}

# Offered in both modes when `suggestions` is on, and only then — the section is
# added by `brief.sections`, so neither mode can carry it while the other misses
# it. Deliberately stricter than the comment contract about quoting: a comment's
# quote is widened and corrected by whoever places it, a suggestion's never is.
SUGGESTION_CONTRACT = """
# Suggesting edits

You may also propose **suggested edits**: tracked changes the author accepts or
rejects in place. Use one where a concrete rewording says it better than a comment
about the wording would — a suggestion carries the fix itself. Keep making
comments for anything substantive: a point about reasoning, evidence or structure
belongs in a comment, not silently rewritten into the text.

For each suggested edit, give:

  quote       — the exact passage to replace, copied character for character from
the document. Unlike a comment's quote it is never widened or corrected: if it
does not match the document exactly, it comes back to you.
  replacement — the full text that replaces the quote. It is applied exactly as
you write it — no editing pass touches it — so make it final. Stay within one
paragraph, and use plain text only (no tabs). To delete words, quote a passage
wide enough to contain them and write the replacement without them.

Write the replacement in the author's own voice: keep their terminology, tense,
register and phrasing habits, and change only what the edit is for. The result
should read as if the author had written it themselves.

The replacement is typed into the document as plain characters, not rendered:
Markdown does not work here, and `*word*` lands as literal asterisks. It also
carries no formatting of its own, so italics or bold inside the quoted passage
are not reproduced — where formatting matters, quote around it or leave a
comment instead.

A suggested edit cannot begin at the very start of a paragraph or line — the
editor misplaces it there. Start the quote at least one word in; for a line's
opening words, leave a comment instead.

A suggested edit counts toward the same budget as a comment. Suggestions are
typed into the document at the end of the run; you will be told immediately if
one cannot be placed.
"""

# The one genuinely mode-specific sentence in the brief: how to hand a comment over.
# It sits beside the tool it describes so the two cannot say different things, and
# agent mode's counterpart sits beside its CLI in `run.context` for the same reason.
SUBMIT_HANDOFF = """
# How to submit

Submit each comment with the `submit_comment` tool as soon as you have written it,
then carry straight on to the next one. It is placed while you write.
""".strip()

# A commenter that keeps submitting quotations nothing can place is not going to
# start. It costs a comment each time, never the run — the old code returned here
# and threw away the remaining budget over one bad quotation.
_MAX_DROPPED = 3

# A paused turn is resumed by sending it straight back, so a provider that keeps
# pausing would loop forever on one comment. Bounded rather than trusted.
_MAX_PAUSES = 5

# Block types a cache breakpoint may be attached to. Server tools introduced blocks
# that are none of these — `server_tool_use` and `web_search_tool_result` — and the
# breakpoint used to go on whatever block happened to be last, which after a search
# is one of those.
_CACHEABLE = ("text", "image", "tool_use", "tool_result", "document")


def _rolling_cache(messages: list[dict], fixed: int) -> None:
    """Keep exactly one moving cache breakpoint, on the newest turn.

    `fixed` is the index in the first message of the document's own breakpoint,
    which never moves — the document and its figures are the stable prefix. This
    adds a second marker that follows the growing conversation, so each turn
    re-reads the prior turns instead of re-processing them. Old markers are cleared
    first: a request may carry four breakpoints and the conversation would burn
    through them by turn three.

    The marker lands on the last block that can carry one, which is not always the
    last block: a turn that ended in a web search ends in a block type the API
    rejects a breakpoint on, and one rejected request costs the whole run.
    """
    for i, m in enumerate(messages):
        for j, block in enumerate(m["content"]):
            if i == 0 and j == fixed:
                continue
            block.pop("cache_control", None)
    for block in reversed(messages[-1]["content"]):
        if block.get("type") in _CACHEABLE:
            block["cache_control"] = {"type": "ephemeral"}
            return


def propose_stream(
    doc_title: str,
    doc_text: str,
    n: int,
    cfg: Config,
    submitter,
    focus: str | None = None,
    usage: model.Usage | None = None,
    tab: dict | None = None,
    prior: str | None = None,
):
    """Yield the (quote, comment) pairs the commenter submits, as it submits them.

    The commenter holds a conversation and calls `submit_comment` when it has one
    ready. Everything the tool call touches — the anchor ladder, deduplication, the
    budget — lives in `submitter`, which is the same object agent mode's
    `post-batch` goes through. That is what makes the two modes one system: the
    commenter's job is identical in both, and so is everything downstream of it.

    What comes back through the tool is deliberately thin. A placed comment gets no
    acknowledgement worth reading, and a rejected one gets the reason and the
    nearest passage — the only information the commenter can act on. Everything
    else, including the editing pass, happens after this generator has yielded and
    is none of the commenter's business.

    Yielding rather than posting inline is what keeps `async` async: the caller
    hands the pair to the critic pool and comes straight back for the next one, so
    comment K is being edited and posted while K+1 is being written.
    """
    # The brief is built from the same section list agent mode renders, so there is
    # no ingredient one mode can carry and the other miss. Only the rendering is
    # ours: images inline, and the cache breakpoint on the last block, since the
    # whole brief is fixed for the run and is resent on every turn.
    parts = brief.sections(
        doc_title, doc_text, tab, cfg, COMMENTER_CONTRACT, SUBMIT_HANDOFF,
        budget=n, focus=focus, prior=prior or "", suggestions=SUGGESTION_CONTRACT,
    )
    system, blocks = brief.as_blocks(parts)
    fixed = len(blocks) - 1
    messages = [{"role": "user", "content": blocks}]

    # Web search is the provider's own tool: it runs the query and returns the
    # results inside the same reply, so there is nothing here to execute and
    # nothing below to change. The commenter's one submission channel is unchanged.
    tools = [SUBMIT_TOOL]
    if cfg.suggestions:
        tools.append(SUGGEST_TOOL)
    if cfg.web_search:
        tools.append(model.web_search_tool(cfg.web_search_max_uses))
        submitter.note(
            f"web search on (up to {cfg.web_search_max_uses} searches per turn on the "
            "direct route; OpenRouter's web plugin has no per-request cap)"
        )

    made = 0
    dropped = 0
    paused = 0
    nudged = False
    while made < n and dropped < _MAX_DROPPED:
        _rolling_cache(messages, fixed)
        reply = model.exchange(
            system,
            messages,
            model=cfg.model,
            provider=cfg.provider,
            effort=cfg.effort,
            cache=True,
            usage=usage,
            tools=tools,
            max_tokens=cfg.max_tokens,
        )
        messages.append({"role": "assistant", "content": reply["content"]})
        calls = model.tool_calls(reply)
        if not calls and reply.get("stop_reason") == "pause_turn":
            # The provider's search loop hit its own iteration limit part way
            # through the turn. The conversation already ends with the paused
            # assistant turn, which is exactly what resuming asks for: send it back
            # unchanged and the server picks up where it stopped. Checked before the
            # quiet-turn branches below, because a paused turn carries no submission
            # and is neither a commenter that has finished nor one that needs
            # reminding — nudging here interrupts a search still in progress.
            paused += 1
            if paused > _MAX_PAUSES:
                submitter.note(
                    f"stopped after the commenter's turn paused {paused} times in a "
                    "row without submitting anything"
                )
                return
            continue
        paused = 0
        if not calls and reply.get("stop_reason") in ("max_tokens", "length"):
            # A truncated reply has no tool call in it, which is indistinguishable
            # from a commenter that has finished — and that is how a run on a long
            # document came back reporting zero comments and exit 0. Say so instead.
            submitter.note(
                f"the commenter's reply hit the {cfg.max_tokens}-token limit before it "
                "submitted anything; raise max_tokens"
            )
            return
        if not calls:
            # Finished — or talking about a comment instead of submitting it, which
            # is worth one reminder and no more. Returning on the first quiet turn
            # would let a chatty preamble end a run that had not started.
            if nudged:
                return
            nudged = True
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "Submit it with the submit_comment tool. If you have "
                                "nothing further worth saying, reply with nothing."
                            ),
                        }
                    ],
                }
            )
            continue
        nudged = False

        results, placed = [], []
        for call in calls:
            args = call.get("input") or {}
            if call.get("name") == "submit_suggestion":
                # Accepted suggestions are queued, not yielded: they type into the
                # document after every comment has posted, bottom-up, which is the
                # ordering that keeps every later anchor upstream of every edit.
                verdict = submitter.accept_suggestion(
                    args.get("quote", ""), args.get("replacement", "")
                )
                accepted_reply = "Accepted; it will be typed at the end of the run."
            else:
                verdict = submitter.accept(args.get("quote", ""), args.get("comment", ""))
                accepted_reply = "Placed."
            if verdict.ok:
                made += 1
                dropped = 0
                if call.get("name") != "submit_suggestion":
                    placed.append((verdict.quote, (args.get("comment") or "").strip()))
            else:
                dropped += 1
                submitter.note(f"rejected a submission: {verdict.reason}")
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": call.get("id"),
                    "content": accepted_reply if verdict.ok else verdict.reason,
                    **({} if verdict.ok else {"is_error": True}),
                }
            )
        messages.append({"role": "user", "content": results})
        # After the tool results are on the conversation, so a consumer that raises
        # mid-stream leaves the transcript consistent.
        yield from placed
    if dropped >= _MAX_DROPPED:
        submitter.note(f"stopped after {dropped} submissions in a row could not be placed")


def vet(items: list, submitter) -> tuple[list[tuple[str, str]], list[str]]:
    """Validate an agent's batch. Returns (usable pairs, rejection reasons).

    Agent mode's entry into the shared machinery. Every proposal goes through the
    same `submitter.accept` the API-mode tool call goes through, so the anchor
    ladder, the deduplication and the budget are not merely equivalent between the
    modes — they are the same three lines of code.

    The rejections are worded for a model holding the document, because that is who
    reads them: the submitter subagent, which corrects the quote and calls again.
    """
    pairs: list[tuple[str, str]] = []
    rejected: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            rejected.append(f"not an object: {item!r:.60}")
            continue
        if "replacement" in item:
            # A suggested edit. Routed through the same `accept_suggestion` the
            # API-mode tool call goes through, and queued on the submitter rather
            # than returned: suggestions post after comments, bottom-up.
            #
            # Gated exactly as API mode gates the tool: with the setting off the
            # capability does not exist, whichever mode is asking.
            if not submitter.cfg.suggestions:
                rejected.append(
                    "suggested edits are not enabled for this run; pass "
                    "--suggestions or set suggestions = true"
                )
                continue
            verdict = submitter.accept_suggestion(
                item.get("quote") or "", item.get("replacement") or ""
            )
            if not verdict.ok:
                rejected.append(verdict.reason)
            continue
        comment = (item.get("comment") or item.get("body") or "").strip()
        verdict = submitter.accept(item.get("quote") or "", comment)
        if verdict.ok:
            pairs.append((verdict.quote, comment[:2000]))
        else:
            rejected.append(verdict.reason)
    return pairs, rejected


def material_blocks(doc_title: str, tab: dict, cfg: Config) -> list[dict]:
    """The document and its figures, rendered for the API.

    Separate from `respond` because building it *downloads* the figures, and a caller
    answering several threads on one document should pay for that once. The bytes are
    what repeat here, not just the tokens: the token cost was already cached, so
    re-rendering per thread showed up as nothing but time and bandwidth.

    `as_blocks` puts the breakpoint on its last block, so the material is rendered
    alone and the thread appended after it. Appending first would move the breakpoint
    onto the one part that changes per thread, which caches nothing and raises no
    error — a full-price run that looks exactly like a cached one.
    """
    return brief.as_blocks(brief.material(doc_title, tab["text"], tab, cfg))[1]


def respond(
    quoted_text: str,
    thread: list[tuple[str, str]],
    doc_title: str,
    cfg: Config,
    tab: dict | None = None,
    usage=None,
    material: list[dict] | None = None,
) -> str:
    """Compose a reply. `thread` is [(author, text), ...] oldest first.

    The responder reads the document, the same rendering of it the commenter read.
    Without it a reply saw the ~60 characters its comment was anchored to and
    nothing else, so an author's "we do address this in section 3" could only be
    conceded to or restated — the one thing it could not be was checked. Conceding
    is what `respond-v1.md` tells it to prefer, which made the failure invisible.

    The document goes in its own block, ahead of the thread and carrying the cache
    breakpoint, so answering ten threads on one document sends the document once and
    reads it from cache nine times.

    Two ways to supply it, and they are the same material: `tab` renders it here,
    which is what a caller with one thread wants; `material` takes a list
    `material_blocks` already built, which is what `run.respond` passes so that the
    figures are downloaded once for the whole run rather than once per thread.
    Neither given keeps the oldest shape — the caller has no document, and a reply
    from the quoted passage alone still beats none.
    """
    transcript = "\n\n".join(f"{who}: {what}" for who, what in thread)
    ask = (
        f"The comment is anchored to this passage:\n\n{quoted_text}\n\n"
        f"The thread so far:\n\n{transcript}"
    )
    if material is None and tab is not None:
        material = material_blocks(doc_title, tab, cfg)
    if material is None:
        return model.complete(
            cfg.respond_system(),
            f"Document: {doc_title}\n{ask}",
            model=cfg.model,
            max_tokens=1024,
            provider=cfg.provider,
            effort=cfg.effort,
        ).strip()

    # A new list rather than `material.append(...)`: the caller reuses these blocks
    # across threads, and appending to them would send thread two the document plus
    # thread one's transcript.
    blocks = [*material, {"type": "text", "text": ask}]
    return model.reply_text(
        model.exchange(
            cfg.respond_system(),
            [{"role": "user", "content": blocks}],
            model=cfg.model,
            max_tokens=1024,
            provider=cfg.provider,
            effort=cfg.effort,
            cache=True,
            usage=usage,
        )
    ).strip()
