"""The commenter loop, the editing pass, and the concurrent pipeline."""

import json

from marginal import config as config_mod
from marginal import critic, ledger, model, reviewer, run, submission
from marginal.post import Result

DOC = (
    "The survey will be emailed to every resident of the district.\n"
    "Response rates in the pilot reached forty percent overall.\n"
    "We therefore expect the full rollout to be representative.\n"
)


def _turn(*pairs):
    """One commenter turn: the tool calls it makes, in order."""
    return {
        "content": [
            {
                "type": "tool_use",
                "id": f"call{i}",
                "name": "submit_comment",
                "input": {"quote": q, "comment": c},
            }
            for i, (q, c) in enumerate(pairs)
        ],
        "stop_reason": "tool_use",
    }


DONE = {"content": [{"type": "text", "text": "Nothing further."}], "stop_reason": "end_turn"}


def _submits(monkeypatch, turns, seen=None):
    """Script the commenter turn by turn, recording each request.

    Stubs `exchange` rather than `converse`: the commenter answers with a tool call
    now, so a test that patched only the text view would let a real request out.
    """
    calls = iter(turns)

    def fake(system, messages, **kw):
        if seen is not None:
            seen.append({"system": system, "messages": [dict(m) for m in messages], "kw": kw})
        return next(calls, DONE)

    monkeypatch.setattr(model, "exchange", fake)


def _cfg(**kw):
    return config_mod.Config(**kw)


def _sub(cfg=None, budget=5, notes=None):
    cfg = cfg or _cfg()
    return submission.Submitter(DOC, cfg, cfg.model, budget=budget, notes=notes)


def _stream(turns, monkeypatch, cfg=None, budget=5, seen=None, sub=None):
    _submits(monkeypatch, turns, seen)
    cfg = cfg or _cfg()
    sub = sub or _sub(cfg, budget)
    return list(reviewer.propose_stream("Doc", DOC, budget, cfg, sub))


def test_the_commenter_may_stop_below_the_budget(monkeypatch):
    # The point of submitting one at a time: "no comments is better than bad
    # comments" is only expressible if the loop can end before the budget.
    out = _stream([_turn(("emailed to every resident", "Who is missed?")), DONE], monkeypatch)
    assert len(out) == 1, "a budget of 5 must not force 5 comments"
    assert out[0][0] == "emailed to every resident"


def test_a_bad_anchor_comes_back_through_the_tool(monkeypatch):
    seen = []
    out = _stream(
        [
            _turn(("text that is absent", "First try.")),
            _turn(("forty percent overall", "Second try.")),
            DONE,
        ],
        monkeypatch,
        seen=seen,
    )
    assert [q for q, _ in out] == ["forty percent overall"]
    # The complaint must reach the commenter as a tool result, or it re-anchors blind.
    result = seen[1]["messages"][-1]["content"][-1]
    assert result["type"] == "tool_result" and result["is_error"]
    assert "is not in the document" in result["content"]


def test_a_placed_comment_is_acknowledged_and_nothing_more(monkeypatch):
    # The commenter can act on a rejection and on nothing else, so a success must not
    # spend its attention. Anything the editing pass does is none of its business.
    seen = []
    _stream([_turn(("forty percent overall", "Of whom?")), DONE], monkeypatch, seen=seen)
    result = seen[1]["messages"][-1]["content"][-1]
    assert result["content"] == "Placed."
    assert "is_error" not in result


def test_a_commenter_that_never_anchors_is_stopped(monkeypatch):
    out = _stream([_turn(("absent", "x"))] * 9, monkeypatch)
    assert out == [], "a commenter that never anchors must end the run, not loop"


def test_prose_instead_of_a_tool_call_is_nudged_once(monkeypatch):
    # A chatty preamble used to look identical to "I am finished". One reminder, then
    # the run ends — never a silent early stop on the first quiet turn.
    seen = []
    out = _stream(
        [DONE, _turn(("forty percent overall", "Of whom?")), DONE, DONE], monkeypatch, seen=seen
    )
    assert [q for q, _ in out] == ["forty percent overall"]
    nudge = seen[1]["messages"][-1]["content"][-1]["text"]
    assert "submit_comment" in nudge


def test_one_passage_gets_one_comment(monkeypatch):
    # Deduplication used to live in `vet`, which only agent mode went through, so an
    # API-mode run could anchor twice to the same sentence and an agent-mode run
    # could not.
    out = _stream(
        [
            _turn(("forty percent overall", "first")),
            _turn(("forty percent overall", "second")),
            DONE,
        ],
        monkeypatch,
    )
    assert len(out) == 1


def test_the_budget_is_a_cap_in_both_modes(monkeypatch):
    sub = _sub(budget=1)
    out = _stream(
        [_turn(("forty percent overall", "a")), _turn(("emailed to every resident", "b")), DONE],
        monkeypatch,
        budget=1,
        sub=sub,
    )
    assert len(out) == 1
    # And the same submitter refuses the same surplus arriving from an agent.
    pairs, rejected = reviewer.vet([{"quote": "expect the full rollout", "comment": "c"}], sub)
    assert pairs == [] and "budget" in rejected[0]


def test_the_document_is_cached_and_breakpoints_stay_bounded(monkeypatch):
    # The document is resent every turn. Uncached, comment five pays for it five
    # times; and a request may carry at most four cache breakpoints, so the rolling
    # marker has to move rather than accumulate.
    seen = []
    _stream(
        [
            _turn(("emailed to every resident", "a")),
            _turn(("forty percent overall", "b")),
            _turn(("expect the full rollout", "c")),
            DONE,
        ],
        monkeypatch,
        seen=seen,
    )
    for call in seen:
        assert call["kw"]["cache"] is True, "the system prompt must be a breakpoint"
        marked = [
            b
            for m in call["messages"]
            for b in m["content"]
            if isinstance(b, dict) and "cache_control" in b
        ]
        assert len(marked) <= 3, f"too many breakpoints: {len(marked)}"
        assert DOC in marked[0]["text"], "the document must keep its fixed breakpoint"
    # By the last turn the rolling marker has moved onto the newest turn.
    assert "cache_control" in seen[-1]["messages"][-1]["content"][-1]


def test_the_commenter_is_given_the_submit_tool(monkeypatch):
    seen = []
    _stream([DONE, DONE], monkeypatch, seen=seen)
    names = [t["name"] for t in seen[0]["kw"]["tools"]]
    assert names == ["submit_comment"]


def test_the_critic_may_only_shorten(monkeypatch):
    long = "This is a much longer rewrite that adds several new words and clauses."
    monkeypatch.setattr(model, "converse", lambda *a, **k: json.dumps({"body": long, "cut": "x"}))
    body, note = critic.tighten("Short original.", "passage", _cfg())
    assert body == "Short original.", "a tighten-only pass must not lengthen a comment"
    assert "lengthened" in note


def test_a_critic_outage_keeps_the_comment(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("529")

    monkeypatch.setattr(model, "converse", boom)
    body, note = critic.tighten("The original point.", "passage", _cfg())
    assert body == "The original point.", "losing a comment to an editing failure is absurd"
    assert "critic failed" in note


def test_the_pipeline_posts_every_comment_in_order(monkeypatch):
    posted = []

    _submits(
        monkeypatch,
        [
            _turn(("emailed to every resident", "first")),
            _turn(("forty percent overall", "second")),
            DONE,
        ],
    )
    # Critic runs concurrently; make it slowest on the first comment so a pipeline
    # that let edits overtake each other would reorder the document.
    def slow(body, anchored, cfg, usage=None):
        if body == "first":
            import time

            time.sleep(0.05)
        return body.upper(), "trimmed"

    monkeypatch.setattr(critic, "tighten", slow)
    monkeypatch.setattr(run, "open_doc", lambda *a, **k: _FakePage())
    def fake_post(page, tab, quote, body, strategy, **_kwargs):
        posted.append((quote, body))
        return Result(quote=quote, body=body, ok=True)

    monkeypatch.setattr(run.post_mod, "post_one", fake_post)
    monkeypatch.setattr(run.post_mod, "snapshot", lambda reader: set())
    monkeypatch.setattr(run.post_mod, "verify", lambda r, before, reader: r)
    monkeypatch.setattr(run, "_reader", lambda *a, **k: (lambda: []))
    monkeypatch.setattr(run, "_prior_threads", lambda *a, **k: "")
    # Don't write a real ledger entry for a document that does not exist.
    monkeypatch.setattr(ledger, "record", lambda *a, **k: None)

    tab = {"id": "t.0", "text": DOC, "paragraphs": []}
    doc = {"title": "Doc", "tabs": [tab]}
    cfg = _cfg(header="")
    _run, pairs, notes = run._pipeline("d", "tok", cfg, doc, tab, 5, None, False)

    assert [b for _, b in posted] == ["FIRST", "SECOND"], "posting must follow document order"
    assert len(pairs) == 2
    assert any("trimmed" in n for n in notes)


class _FakePage:
    def close(self):
        pass


def _pipeline_run(monkeypatch, schedule, posted):
    """Drive the whole pipeline with the browser and network stubbed out."""
    _submits(
        monkeypatch,
        [
            _turn(("emailed to every resident", "first")),
            _turn(("forty percent overall", "second")),
            _turn(("expect the full rollout", "third")),
            DONE,
        ],
    )
    monkeypatch.setattr(critic, "tighten", lambda b, a, c, u=None: (b.upper(), "trimmed"))
    monkeypatch.setattr(run, "open_doc", lambda *a, **k: _FakePage())
    monkeypatch.setattr(run.post_mod, "snapshot", lambda reader: set())
    monkeypatch.setattr(run.post_mod, "verify", lambda r, before, reader: r)
    monkeypatch.setattr(run, "_reader", lambda *a, **k: (lambda: []))
    monkeypatch.setattr(run, "_prior_threads", lambda *a, **k: "")
    monkeypatch.setattr(ledger, "record", lambda *a, **k: None)

    def fake_post(page, tab, quote, body, strategy, **_kwargs):
        posted.append(body)
        return Result(quote=quote, body=body, ok=True)

    monkeypatch.setattr(run.post_mod, "post_one", fake_post)
    tab = {"id": "t.0", "text": DOC, "paragraphs": []}
    doc = {"title": "Doc", "tabs": [tab]}
    return run._pipeline("d", "tok", _cfg(header="", schedule=schedule), doc, tab, 5, None, False)


def test_sync_and_async_agree_on_what_lands(monkeypatch):
    # The two schedules differ in ordering only. If they ever disagreed on content
    # or order, "which schedule ran" would confound every comparison.
    sync_posted, async_posted = [], []
    _, sync_pairs, _ = _pipeline_run(monkeypatch, "sync", sync_posted)
    _, async_pairs, _ = _pipeline_run(monkeypatch, "async", async_posted)
    assert sync_posted == async_posted == ["FIRST", "SECOND", "THIRD"]
    assert sync_pairs == async_pairs


def test_the_run_reports_its_token_use(monkeypatch):
    # Caching fails silently — a moved prefix or a too-short prompt just costs more.
    # Reading the totals back is the only way to notice.
    _, _, notes = _pipeline_run(monkeypatch, "sync", [])
    assert any(n.startswith("tokens:") for n in notes)


def test_the_length_band_is_configurable():
    # Length lives in config, not baked into the prompt file, so a run can change
    # it without forking prompts/critique-v1.md.
    short = _cfg(min_words=10, max_words=25, word_ceiling=35).critique_system()
    assert "10–25 words" in short and "35 is a hard ceiling" in short
    assert "{min_words}" not in short and "{ceiling}" not in short
    wide = _cfg(min_words=40, max_words=120, word_ceiling=200).critique_system()
    assert "40–120 words" in wide and "200 is a hard ceiling" in wide


def test_an_impossible_length_band_is_rejected():
    import pytest

    for bad in ({"min_words": 50, "max_words": 20}, {"max_words": 90, "word_ceiling": 40}):
        with pytest.raises(ValueError, match="min <= max <= ceiling"):
            _cfg(**bad).critique_system()


def test_a_prompt_without_placeholders_is_used_verbatim(tmp_path):
    # A vendored critique prompt should not have to be templated to work.
    p = tmp_path / "plain.md"
    p.write_text("Tighten the comment. Return JSON.")
    assert _cfg(critique_prompt=p).critique_system() == "Tighten the comment. Return JSON."


def test_a_transient_selection_failure_is_retried(monkeypatch):
    # Comments have been lost to the selection reading back as a
    # single space; the same quotes then selected correctly three times running.
    from marginal import docs_ui
    from marginal import post as post_mod

    calls = {"n": 0}

    def flaky(page, span, paragraphs, strategy, text=None):
        calls["n"] += 1
        if calls["n"] < 3:
            return False, 1, " "
        return True, 1, span.quote

    monkeypatch.setattr(post_mod, "select_span", flaky)
    monkeypatch.setattr(post_mod, "post_comment", lambda page, body: None)
    tab = {"id": "t.0", "text": DOC, "paragraphs": []}
    r = post_mod.post_one(None, tab, "emailed to every resident", "body", "paragraph")
    assert r.ok and r.error is None
    assert calls["n"] == 3


def test_a_persistent_selection_failure_still_refuses(monkeypatch):
    # Retrying must not turn "could not confirm" into a comment on the wrong text.
    from marginal import post as post_mod

    monkeypatch.setattr(post_mod, "select_span", lambda *a, **k: (False, 1, " "))
    posted = []
    monkeypatch.setattr(post_mod, "post_comment", lambda page, body: posted.append(body))
    tab = {"id": "t.0", "text": DOC, "paragraphs": []}
    r = post_mod.post_one(None, tab, "emailed to every resident", "body", "paragraph", attempts=2)
    assert not r.ok and "selection not confirmed" in r.error
    assert posted == [], "nothing may be posted on an unconfirmed selection"


def test_an_unresolvable_quote_is_not_retried(monkeypatch):
    # A quote absent from the document will be absent on every attempt.
    from marginal import post as post_mod

    calls = {"n": 0}

    def counted(*a, **k):
        calls["n"] += 1
        return (True, 1, "x")

    monkeypatch.setattr(post_mod, "select_span", counted)
    tab = {"id": "t.0", "text": DOC, "paragraphs": []}
    r = post_mod.post_one(None, tab, "text that is not present", "body", "paragraph")
    assert not r.ok and calls["n"] == 0


def test_agent_mode_does_not_call_a_model_by_default(monkeypatch):
    # Agent mode is the path with no API key by design, so re-anchoring is the
    # submitting subagent's job. A model call here would silently do nothing.
    from marginal import critic, ledger
    from marginal import run as runmod

    called = []
    monkeypatch.setattr(critic, "reconcile_anchor", lambda *a, **k: called.append(1))
    tab = {"id": "t.0", "text": DOC, "paragraphs": []}
    monkeypatch.setattr(runmod, "read_document", lambda *a, **k: {"title": "D", "tabs": [tab]})
    monkeypatch.setattr(ledger, "record", lambda *a, **k: None)

    _run, pairs, notes = runmod.post_batch(
        "d", "tok", _cfg(header=""),
        [{"quote": "| a table cell | not in the document |", "comment": "x"}],
        tab_id=None, dry_run=True,
    )
    assert called == [], "no model call unless reconcile_anchors is on"
    assert pairs == []
    assert any("is not in the document" in n for n in notes), notes


def test_a_rejection_names_the_nearest_passage(monkeypatch):
    # The submitter is a model holding the document; a reason it can act on saves a
    # whole round trip over "not found". A merely mistyped quote never gets here —
    # the fuzzy rung places it — so this is a paraphrase, which cannot be.
    from marginal import ledger
    from marginal import run as runmod

    tab = {"id": "t.0", "text": DOC, "paragraphs": []}
    monkeypatch.setattr(runmod, "read_document", lambda *a, **k: {"title": "D", "tabs": [tab]})
    monkeypatch.setattr(ledger, "record", lambda *a, **k: None)
    _run, _pairs, notes = runmod.post_batch(
        "d", "tok", _cfg(header=""),
        [{"quote": "the survey goes out to each resident living in the district",
           "comment": "x"}],
        tab_id=None, dry_run=True,
    )
    assert any("closest passage" in n for n in notes), notes


def test_the_api_fallback_can_still_be_switched_on(monkeypatch):
    from marginal import critic, ledger
    from marginal import run as runmod

    asked = []

    def fake(text, quote, cfg, usage=None):
        asked.append(quote)
        return "emailed to every resident"

    monkeypatch.setattr(critic, "reconcile_anchor", fake)
    tab = {"id": "t.0", "text": DOC, "paragraphs": []}
    monkeypatch.setattr(runmod, "read_document", lambda *a, **k: {"title": "D", "tabs": [tab]})
    monkeypatch.setattr(ledger, "record", lambda *a, **k: None)
    _run, pairs, _notes = runmod.post_batch(
        "d", "tok", _cfg(header="", reconcile_anchors=True),
        [{"quote": "| a table cell | not in the document |", "comment": "x"}],
        tab_id=None, dry_run=True,
    )
    assert asked and pairs and pairs[0][0] == "emailed to every resident"
