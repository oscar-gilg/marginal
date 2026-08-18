"""Suggestions through both modes: the tool, `vet`, the strict rungs, the queue.

The mode-parity invariant is the frame: a suggestion must behave identically
whether the API-mode commenter called `submit_suggestion` or an agent handed
`post-batch` an item with a `replacement` — both routes hit the same
`Submitter.accept_suggestion`, and these tests drive each route through it.
"""

import json

from marginal import config as config_mod
from marginal import brief, ledger, model, reviewer, submission

DOC = (
    "The survey will be emailed to every resident of the district.\n"
    "Response rates in the pilot reached forty percent overall.\n"
    "We therefore expect the full rollout to be representative.\n"
)

DONE = {"content": [{"type": "text", "text": "Done."}], "stop_reason": "end_turn"}


def _cfg(**kw):
    kw.setdefault("suggestions", True)
    return config_mod.Config(**kw)


def _sub(cfg=None, budget=5, notes=None):
    cfg = cfg or _cfg()
    return submission.Submitter(DOC, cfg, cfg.model, budget=budget, notes=notes)


def _suggest_turn(*pairs):
    return {
        "content": [
            {
                "type": "tool_use",
                "id": f"s{i}",
                "name": "submit_suggestion",
                "input": {"quote": q, "replacement": r},
            }
            for i, (q, r) in enumerate(pairs)
        ],
        "stop_reason": "tool_use",
    }


def _stream(turns, monkeypatch, cfg=None, sub=None, seen=None):
    calls = iter(turns)

    def fake(system, messages, **kw):
        if seen is not None:
            seen.append({"messages": [dict(m) for m in messages], "kw": kw})
        return next(calls, DONE)

    monkeypatch.setattr(model, "exchange", fake)
    cfg = cfg or _cfg()
    sub = sub if sub is not None else _sub(cfg)
    return list(reviewer.propose_stream("Doc", DOC, 5, cfg, sub)), sub


# --- the API-mode tool -------------------------------------------------------


def test_the_tool_is_offered_only_when_suggestions_are_on(monkeypatch):
    seen = []
    _stream([DONE], monkeypatch, cfg=_cfg(suggestions=True), seen=seen)
    names = [t["name"] for t in seen[0]["kw"]["tools"]]
    assert names == ["submit_comment", "submit_suggestion"]

    seen = []
    off = config_mod.Config()
    _stream([DONE], monkeypatch, cfg=off, sub=_sub(off), seen=seen)
    assert [t["name"] for t in seen[0]["kw"]["tools"]] == ["submit_comment"]


def test_an_accepted_suggestion_is_queued_not_yielded(monkeypatch):
    out, sub = _stream(
        [_suggest_turn(("forty percent overall", "forty percent of openers")), DONE],
        monkeypatch,
    )
    assert out == []  # nothing enters the comment pipeline
    assert sub.suggestions == [("forty percent overall", "forty percent of openers")]


def test_a_rejected_suggestion_comes_back_through_the_tool(monkeypatch):
    seen = []
    _, sub = _stream(
        [_suggest_turn(("text that is absent", "anything")), DONE], monkeypatch, seen=seen
    )
    assert sub.suggestions == []
    result = seen[1]["messages"][-1]["content"][-1]
    assert result["type"] == "tool_result" and result["is_error"]
    assert "never widened" in result["content"]


# --- vet: the agent-mode route ----------------------------------------------


def test_vet_routes_a_replacement_item_to_the_same_acceptance():
    sub = _sub()
    pairs, rejected = reviewer.vet(
        [
            {"quote": "forty percent overall", "replacement": "forty percent of openers"},
            {"quote": "every resident of the district", "comment": "Everyone? Really?"},
        ],
        sub,
    )
    assert pairs == [("every resident of the district", "Everyone? Really?")]
    assert rejected == []
    assert sub.suggestions == [("forty percent overall", "forty percent of openers")]


def test_vet_refuses_a_replacement_item_when_suggestions_are_off():
    # Parity with API mode, where the tool is simply not offered: with the
    # setting off the capability must not exist through the other door either.
    off = config_mod.Config()
    sub = submission.Submitter(DOC, off, off.model, budget=5)
    _, rejected = reviewer.vet(
        [{"quote": "forty percent overall", "replacement": "x"}], sub
    )
    assert sub.suggestions == []
    assert len(rejected) == 1 and "not enabled" in rejected[0]


def test_vet_rejects_a_bad_suggestion_with_the_same_wording_the_tool_uses():
    sub = _sub()
    _, rejected = reviewer.vet([{"quote": "absent text", "replacement": "x"}], sub)
    assert len(rejected) == 1 and "never widened" in rejected[0]


# --- the strict rungs --------------------------------------------------------


def test_a_suggestion_may_not_anchor_through_the_fuzzy_rung():
    # This quote misspells one word ("fourty"), which the fuzzy rung places
    # happily for a comment — and must refuse for a suggestion.
    quote = "Response rates in the pilot reached fourty percent overall."
    sub = _sub()
    assert sub.accept(quote, "a comment").ok
    verdict = _sub().accept_suggestion(quote, "some replacement")
    assert not verdict.ok


def test_a_suggestion_still_anchors_through_the_word_exact_rungs():
    # `stripped` and `whitespace` return the document's own bytes; only guessing
    # is excluded, not exactness.
    verdict = _sub().accept_suggestion(
        "**forty percent overall**", "forty percent of openers"
    )
    assert verdict.ok and verdict.rung == "stripped"
    assert verdict.quote == "forty percent overall"


def test_overlapping_suggestions_are_refused():
    sub = _sub()
    assert sub.accept_suggestion(
        "rates in the pilot reached forty percent", "rates reached forty percent"
    ).ok
    verdict = sub.accept_suggestion("forty percent overall", "most respondents")
    assert not verdict.ok and "overlaps" in verdict.reason


def test_a_multi_paragraph_replacement_is_refused():
    verdict = _sub().accept_suggestion("forty percent overall", "line one\nline two")
    assert not verdict.ok and "one paragraph" in verdict.reason


def test_a_replacement_containing_markdown_emphasis_is_refused():
    # Observed live: the commenter reads the document as Markdown and wrote
    # `*points*` meaning italics; the replacement is typed, so the asterisks
    # landed in the author's prose as literal characters.
    verdict = _sub().accept_suggestion("forty percent overall", "forty percent of *openers*")
    assert not verdict.ok and "typed, not rendered" in verdict.reason
    backticks = _sub().accept_suggestion("forty percent overall", "forty `percent`")
    assert not backticks.ok
    # A lone asterisk is legitimate text and must pass.
    assert _sub().accept_suggestion(
        "forty percent overall", "forty percent overall*"
    ).ok


def test_a_replacement_containing_a_tab_is_refused():
    # A typed tab is an indent command to the editor, observed live to truncate
    # the replacement mid-word.
    verdict = _sub().accept_suggestion("forty percent overall", "forty\tpercent")
    assert not verdict.ok and "tab" in verdict.reason


def test_a_paragraph_initial_quote_is_refused():
    # Probed live: a selection whose left edge is a paragraph's first character
    # lands the typed insertion at the end of the previous paragraph.
    first = _sub().accept_suggestion(
        "The survey will be emailed", "The survey is emailed"
    )
    assert not first.ok and "start of a line" in first.reason
    second = _sub().accept_suggestion(
        "Response rates in the pilot", "Pilot response rates"
    )
    assert not second.ok and "start of a line" in second.reason


def test_a_quote_right_after_a_soft_line_break_is_refused():
    # Observed live: the deletion swallowed the soft break, one character wider
    # than the quote — the same displaced-left-edge family as paragraph starts.
    doc = "A heading\vsits under a soft break in the same paragraph.\n"
    sub = submission.Submitter(doc, _cfg(), "m", budget=5)
    verdict = sub.accept_suggestion("sits under a soft break", "sits beneath a soft break")
    assert not verdict.ok and "start of a line" in verdict.reason


def test_suggestions_share_the_comment_budget():
    sub = _sub(budget=1)
    assert sub.accept_suggestion("forty percent overall", "forty percent of openers").ok
    assert not sub.accept("every resident", "One more.").ok


# --- the brief carries the contract in both renderings -----------------------


def test_the_brief_carries_the_suggestion_section_only_when_on():
    def names(cfg):
        parts = brief.sections(
            "Doc", DOC, None, cfg, "contract", "handoff", budget=3,
            suggestions=reviewer.SUGGESTION_CONTRACT,
        )
        return [s.name for s in parts]

    assert "suggestions" in names(_cfg(suggestions=True))
    assert "suggestions" not in names(config_mod.Config())


def test_both_renderers_carry_the_suggestion_contract():
    parts = brief.sections(
        "Doc", DOC, None, _cfg(), "contract", "handoff", budget=3,
        suggestions=reviewer.SUGGESTION_CONTRACT,
    )
    system, _ = brief.as_blocks(parts)
    assert "Suggesting edits" in system
    assert "Suggesting edits" in brief.as_text(parts, None)


# --- agent mode end to end (dry run) -----------------------------------------


def test_post_batch_dry_run_accepts_a_mixed_batch(monkeypatch):
    from marginal import run as runmod

    tab = {"id": "t.0", "text": DOC, "paragraphs": [], "revision_id": "rev-1"}
    monkeypatch.setattr(
        runmod, "read_document", lambda *a, **k: {"title": "D", "tabs": [tab]}
    )
    monkeypatch.setattr(runmod.post_mod, "api_tab_refresher", lambda *a, **k: None)
    run, pairs, rejected = runmod.post_batch(
        "doc1", "tok", _cfg(),
        [
            {"quote": "every resident of the district", "comment": "Everyone?"},
            {"quote": "forty percent overall", "replacement": "forty percent of openers"},
        ],
        dry_run=True,
    )
    assert run is None
    assert pairs == [("every resident of the district", "Everyone?")]
    assert any("suggestions: 1 accepted (dry run" in line for line in rejected)
    # The replacement is applied verbatim, so the dry-run preview shows it whole.
    assert any("→ forty percent of openers" in line for line in rejected)


# --- the report and its exit code --------------------------------------------


def test_a_suggestion_only_run_reports_by_its_results(capsys):
    # `pairs` counts comments, so a run that only suggested used to be judged
    # against zero — a failed suggestion would have exited 0.
    from marginal import cli
    from marginal.post import Result, Run

    run = Run(doc_id="d", tab_id="t", strategy="paragraph")
    run.results = [Result(quote="q", body="", ok=True, kind="suggestion",
                          replacement="better words")]
    assert cli._report(run, [], []) == 0
    assert "→ better words" in capsys.readouterr().out

    run.results[0].ok = False
    run.results[0].error = "typed suggestion could not be read back"
    assert cli._report(run, [], []) == 1


def test_run_notes_reach_the_report(capsys):
    from marginal import cli
    from marginal.post import Run

    run = Run(doc_id="d", tab_id="t", strategy="paragraph",
              notes=["the editor could not be switched back to editing mode"])
    cli._report(run, [], [])
    assert "switched back" in capsys.readouterr().out


# --- the ledger --------------------------------------------------------------


def test_a_recorded_suggestion_does_not_join_the_unpostable_comments(tmp_path, monkeypatch):
    path = tmp_path / "ledger.jsonl"
    monkeypatch.setattr(ledger, "LEDGER", path)
    ledger.record("doc1", "c-1", "q", "b", "m", kind="comment", path=path)
    ledger.record("doc1", None, "quote", "replacement", "m", kind="suggestion", path=path)
    assert ledger.our_comment_ids("doc1", path) == {"c-1"}
    kinds = [json.loads(line)["kind"] for line in path.read_text().splitlines()]
    assert kinds == ["comment", "suggestion"]


def test_the_submitter_records_a_suggestion_with_its_replacement(monkeypatch):
    rows = []
    monkeypatch.setattr(
        ledger, "record",
        lambda doc, cid, quote, body, label, kind="comment", **kw: rows.append(
            {"cid": cid, "quote": quote, "body": body, "kind": kind}
        ),
    )
    from marginal.post import Result

    sub = _sub()
    sub.record("doc1", Result(
        quote="forty percent overall", body="", ok=True,
        kind="suggestion", replacement="forty percent of openers",
    ))
    assert rows == [{
        "cid": None, "quote": "forty percent overall",
        "body": "forty percent of openers", "kind": "suggestion",
    }]
