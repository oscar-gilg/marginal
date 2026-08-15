"""Where API mode and agent mode must behave the same.

The two modes are meant to differ in who does the thinking and nothing else. Every
test here pins a place where they had quietly drifted apart, so a comment would
have depended on which mode produced it — which is exactly what makes cross-mode
comparison worthless.
"""

import base64
import json

from marginal import config as config_mod
from marginal import brief, figures, reviewer, run, submission
from marginal import model as model_mod

DOC = (
    "The survey will be emailed to every resident of the district.\n"
    "Response rates in the pilot reached forty percent overall.\n"
    "We therefore expect the full rollout to be representative.\n"
)


def _cfg(**kw):
    return config_mod.Config(**kw)


def _turn(*pairs):
    return {
        "content": [
            {
                "type": "tool_use",
                "id": f"c{i}",
                "name": "submit_comment",
                "input": {"quote": q, "comment": c},
            }
            for i, (q, c) in enumerate(pairs)
        ],
        "stop_reason": "tool_use",
    }


DONE = {"content": [{"type": "text", "text": "Done."}], "stop_reason": "end_turn"}


def _submits(monkeypatch, turns, seen=None):
    calls = iter(turns)

    def fake(system, messages, **kw):
        if seen is not None:
            seen.append({"system": system, "messages": [dict(m) for m in messages], "kw": kw})
        return next(calls, DONE)

    monkeypatch.setattr(model_mod, "exchange", fake)


def _sub(cfg=None, budget=5, notes=None, reconcile=None):
    cfg = cfg or _cfg()
    return submission.Submitter(
        DOC, cfg, cfg.model, budget=budget, notes=notes, reconcile=reconcile
    )


def test_an_unplaceable_quote_costs_a_comment_not_the_run(monkeypatch):
    # The old loop returned on retry exhaustion, abandoning the remaining budget
    # over one bad quotation. Agent mode had already been fixed for exactly this
    # shape of terminal failure; API mode still had it.
    _submits(
        monkeypatch,
        [
            _turn(("a sentence that is nowhere in this document", "x")),
            _turn(("reached forty percent overall", "Of whom?")),
            DONE,
        ],
    )
    notes: list[str] = []
    sub = _sub(notes=notes)
    out = list(reviewer.propose_stream("t", DOC, 3, _cfg(), sub))
    assert [q for q, _ in out] == ["reached forty percent overall"]
    assert any("rejected a submission" in n for n in notes)


def test_enough_consecutive_drops_still_stop_the_run(monkeypatch):
    # Survivable is not the same as unbounded: a commenter that can place nothing
    # should not spend the whole budget discovering that.
    _submits(monkeypatch, [_turn(("nowhere in this document", "x"))] * 30)
    notes: list[str] = []
    sub = _sub(notes=notes)
    assert list(reviewer.propose_stream("t", DOC, 10, _cfg(), sub)) == []
    assert any("stopped after" in n for n in notes)


def test_the_commenter_is_shown_the_threads_already_on_the_document(monkeypatch):
    # Agent mode's brief always carried these. API mode's did not, so the same
    # prompt on the same document repeated a point in one mode and declined to in
    # the other.
    seen: list[dict] = []
    _submits(monkeypatch, [DONE, DONE], seen)
    prior = "# Comments already on this document\n\n  on 'forty percent'\n    Of whom?"
    list(reviewer.propose_stream("t", DOC, 2, _cfg(), _sub(), prior=prior))
    sent = json.dumps(seen[0]["messages"][0]["content"])
    assert "Comments already on this document" in sent


def test_prior_threads_sit_inside_the_cached_prefix(monkeypatch):
    # They are fixed for the whole run, so they belong before the breakpoint. If
    # they landed after it, every turn would re-read them at full price.
    seen: list[dict] = []
    _submits(monkeypatch, [DONE, DONE], seen)
    list(
        reviewer.propose_stream(
            "t", DOC, 2, _cfg(), _sub(), prior="# Comments already on this document"
        )
    )
    blocks = seen[0]["messages"][0]["content"]
    marked = [i for i, b in enumerate(blocks) if b.get("cache_control")]
    assert marked, "no breakpoint on the document turn"
    prior_at = next(
        i for i, b in enumerate(blocks) if "Comments already" in (b.get("text") or "")
    )
    assert prior_at <= marked[0]


def test_the_model_fallback_is_reached_in_both_modes(monkeypatch):
    # The ladder's last rung used to exist only in agent mode. It now lives on the
    # submitter, which is the object both modes go through — so one setting turns it
    # on for both and there is no second implementation to drift.
    asked: list[str] = []

    def reconcile(text, quote):
        asked.append(quote)
        return "reached forty percent overall"

    _submits(monkeypatch, [_turn(("nowhere in this document at all", "x")), DONE])
    sub = _sub(reconcile=reconcile)
    out = list(reviewer.propose_stream("t", DOC, 1, _cfg(), sub))
    assert [q for q, _ in out] == ["reached forty percent overall"]
    assert sub.rungs == ["model"] and len(asked) == 1

    # Agent mode, same submitter class, same setting, same rung.
    agent_sub = _sub(reconcile=reconcile)
    pairs, _ = reviewer.vet(
        [{"quote": "nowhere in this document at all", "comment": "x"}], agent_sub
    )
    assert pairs and pairs[0][0] == "reached forty percent overall"
    assert agent_sub.rungs == ["model"]


def test_both_modes_stamp_the_same_header():
    # A run's comments are data about the model that wrote them; a header that
    # differs by mode weakens exactly the artifact being collected.
    cfg = _cfg(header="**{model}**")
    assert _sub(cfg).stamp("body") == submission.Submitter(
        DOC, cfg, cfg.model, budget=5
    ).stamp("body")


def test_one_contract_serves_both_modes():
    # There were two, and they had drifted: one made the commenter count characters
    # and re-anchor its own rejections, the other asked for none of it.
    assert reviewer.AGENT_CONTRACT is reviewer.COMMENTER_CONTRACT
    assert "do not count characters" in reviewer.COMMENTER_CONTRACT


def test_figures_reach_the_agent_as_files(tmp_path):
    # API mode sends the images; agent mode's brief is a string and sent nothing,
    # so its commenter was told a figure existed and shown no figure.
    png = base64.b64encode(b"\x89PNG\r\n\x1a\nfake").decode()
    saved = figures.save(
        [{"media_type": "image/png", "data": png, "alt": "throughput"}], tmp_path / "d"
    )
    assert len(saved) == 1
    assert saved[0]["path"].endswith("figure-1.png")
    assert open(saved[0]["path"], "rb").read().startswith(b"\x89PNG")
    assert saved[0]["alt"] == "throughput"


def _parts(cfg=None, tab=None, prior="", budget=3):
    cfg = cfg or _cfg()
    return brief.sections(
        "Doc", DOC, tab, cfg, reviewer.COMMENTER_CONTRACT, "HANDOFF",
        budget=budget, prior=prior,
    )


def _fake_figures(monkeypatch, n=2):
    monkeypatch.setattr(
        brief.figures,
        "fetch",
        lambda figs, limit, max_bytes: [
            {"media_type": "image/png", "data": base64.b64encode(b"\x89PNGx").decode(),
             "alt": f"alt{i}"}
            for i in range(n)
        ],
    )
    return {"id": "t.1", "text": DOC,
            "figures": [{"uri": "http://x/1.png", "at": 10 + i} for i in range(n)]}


def test_neither_renderer_may_drop_a_section(monkeypatch, tmp_path):
    # The conformance check the seam exists for. Every ingredient the commenter is
    # given must survive both renderings — agent mode silently went without the
    # figures, then the budget, then the editing model's name, each the same bug.
    tab = _fake_figures(monkeypatch)
    parts = _parts(tab=tab, prior="PRIOR-THREADS-MARKER")
    system, blocks = brief.as_blocks(parts)
    as_blocks_text = system + json.dumps(blocks)
    as_text = brief.as_text(parts, tmp_path)
    for section in parts:
        probe = section.text.strip().splitlines()[0]
        assert probe in as_blocks_text, f"API mode dropped {section.name!r}"
        assert probe in as_text, f"agent mode dropped {section.name!r}"


def test_every_figure_reaches_both_commenters(monkeypatch, tmp_path):
    # Same figures, same numbering, same captions. The only difference is that one
    # destination takes bytes and the other takes a path, which is a fact about
    # HTTPS requests versus terminals and not about reviewing.
    tab = _fake_figures(monkeypatch, n=3)
    parts = _parts(tab=tab)
    # The figures section alone. Scanning the whole brief would also match the
    # `[figure N]` markers the document itself carries, which is how a hand check of
    # this counted nineteen figures on a document that has eight.
    figs = next(s for s in parts if s.name == "figures")
    _system, blocks = brief.as_blocks([figs])
    images = [b for b in blocks if b.get("type") == "image"]
    assert len(images) == 3, "API mode must carry every figure as an image"

    text = brief.as_text([figs], tmp_path)
    rows = [ln.strip() for ln in text.splitlines() if ln.strip().startswith("[figure ")]
    paths = [r.rsplit("  ", 1)[1].strip() for r in rows]
    assert len(paths) == 3, "agent mode must carry every figure as a file"
    for path in paths:
        assert open(path, "rb").read().startswith(b"\x89PNG")

    # And they are called the same thing in both.
    captions = [f"[figure {i}] (alt{i - 1})" for i in (1, 2, 3)]
    for c in captions:
        assert c in json.dumps(blocks), f"API mode renamed {c}"
        assert c in text, f"agent mode renamed {c}"
    # And they are the same bytes, not merely the same count.
    for i, path in enumerate(paths):
        assert open(path, "rb").read() == base64.b64decode(figs.images[i]["data"])


def test_a_figure_that_will_not_download_costs_only_itself(monkeypatch, tmp_path):
    monkeypatch.setattr(
        brief.figures, "fetch", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no network"))
    )
    tab = {"id": "t.1", "text": DOC, "figures": [{"uri": "http://example/x.png", "at": 5}]}
    parts = _parts(tab=tab)
    assert [s.name for s in parts if s.name == "figures"] == []
    assert "document" in [s.name for s in parts], "the review goes on without the figure"


def test_both_modes_read_the_sections_in_one_order():
    # Not cosmetic: the order used to differ, which is how it stayed unnoticed that
    # they held different things.
    assert [s.name for s in _parts(prior="P")] == [
        "instructions", "contract", "handoff", "ask", "prior", "document",
    ]


def test_the_whole_brief_is_one_cached_prefix(monkeypatch):
    # Every section is fixed for the run, so the breakpoint belongs on the last
    # block. A marker in the middle would leave the document paying full price.
    tab = _fake_figures(monkeypatch)
    _system, blocks = brief.as_blocks(_parts(tab=tab, prior="P"))
    marked = [i for i, b in enumerate(blocks) if "cache_control" in b]
    assert marked == [len(blocks) - 1]


def test_two_posters_on_one_document_do_not_overlap(monkeypatch, tmp_path):
    # API mode serialises the editor through one poster thread. Agent mode's brief
    # tells the commenter to fire off a subagent per comment and carry on, so several
    # post-batch processes can reach the editor at once — and a Docs tab has one
    # selection state. Serialising in the commenter would put back the clerical work
    # the handoff exists to remove, so the lock lives below both of them.
    import threading
    import time

    monkeypatch.setattr(run, "LOCK_DIR", tmp_path)
    overlapping, inside = [], []

    def hold():
        with run.browser_lock("doc1"):
            inside.append(1)
            overlapping.append(len(inside))
            time.sleep(0.05)
            inside.pop()

    threads = [threading.Thread(target=hold) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert overlapping == [1, 1, 1, 1], f"posters overlapped in the editor: {overlapping}"


def test_different_documents_do_not_wait_for_each_other(monkeypatch, tmp_path):
    monkeypatch.setattr(run, "LOCK_DIR", tmp_path)
    with run.browser_lock("doc1"), run.browser_lock("doc2"):
        pass


# --- the two modes take the same settings ------------------------------------


def _flags(cmd: str) -> set[str]:
    import argparse

    from marginal import cli

    sub = [
        a for a in cli._parser()._actions if isinstance(a, argparse._SubParsersAction)
    ][0]
    return {opt for action in sub.choices[cmd]._actions for opt in action.option_strings}


def test_every_commenting_command_takes_the_same_settings():
    """A setting one mode can be tuned with and the other cannot is a difference.

    `review` owned the critic flags and `post-batch` owned none of them, so an
    agent-mode run took the configured defaults while an API-mode run took what was
    typed — and the comments then differed in length for a reason that had nothing
    to do with which mode wrote them.
    """
    shared = _flags("review") & _flags("context")
    assert "--max-words" in shared and "--critic-model" in shared and "--mode" in shared
    for cmd in ("comment", "context", "submit-brief", "post-batch"):
        missing = _flags("review") - _flags(cmd)
        # `post-batch` and `comment` also open the editor, so they carry the placing
        # flags too; `context` and `submit-brief` never do.
        allowed = {"--port", "--strategy", "--dry-run"} if cmd in ("context", "submit-brief") else set()
        assert missing <= allowed, f"{cmd} cannot be given: {sorted(missing - allowed)}"


def test_a_flag_reaches_the_same_setting_in_either_mode(monkeypatch):
    from marginal import cli

    seen = []
    monkeypatch.setattr(cli.config_mod, "load", lambda *a, **k: seen.append(k) or _cfg())
    for argv in (
        ["review", "1" * 25, "--max-words", "40", "--critic-model", "m"],
        ["post-batch", "1" * 25, "--from", "-", "--max-words", "40", "--critic-model", "m"],
    ):
        try:
            cli._main(argv)
        except (SystemExit, Exception):
            pass  # the run itself needs Google and a browser; only the parse matters
    assert len(seen) == 2 and seen[0]["max_words"] == seen[1]["max_words"] == 40
    assert seen[0]["critic_model"] == seen[1]["critic_model"] == "m"


def test_comment_follows_the_configured_mode(monkeypatch):
    """`comment` is whichever command the config names, and nothing else changes."""
    from marginal import cli, gdocs

    ran = []
    monkeypatch.setattr(gdocs, "access_token", lambda *a, **k: "tok")
    monkeypatch.setattr(cli, "review", lambda *a, **k: ran.append("review") or (None, [], []))
    monkeypatch.setattr(cli, "context", lambda *a, **k: ran.append("context") or "brief")

    for mode, expected in (("api", "review"), ("agent", "context")):
        monkeypatch.setattr(cli.config_mod, "load", lambda *a, _m=mode, **k: _cfg(mode=_m))
        cli._main(["comment", "1" * 25])
        assert ran[-1] == expected, (mode, ran)


def test_an_explicit_command_beats_the_configured_mode_and_says_so(monkeypatch, capsys):
    # Comparing the modes on one document means running both against one config, so
    # naming the command wins — but a run must never be silently attributed to the
    # mode the config names.
    from marginal import cli, gdocs

    monkeypatch.setattr(gdocs, "access_token", lambda *a, **k: "tok")
    monkeypatch.setattr(cli.config_mod, "load", lambda *a, **k: _cfg(mode="agent"))
    monkeypatch.setattr(cli, "review", lambda *a, **k: (None, [], []))
    cli._main(["review", "1" * 25])
    assert "running api mode" in capsys.readouterr().err


def test_the_browser_source_frees_both_modes_from_google(monkeypatch):
    # `source = "browser"` exists so a run needs no Google credentials at all. The
    # exemption named the two agent-mode commands, so API mode demanded a token it
    # never used — a difference between the modes with no reason behind it.
    from marginal import cli, gdocs

    def refuse(*a, **k):
        raise AssertionError("minted a Google token on the credential-free path")

    monkeypatch.setattr(gdocs, "access_token", refuse)
    monkeypatch.setattr(cli.config_mod, "load", lambda *a, **k: _cfg(source="browser"))
    monkeypatch.setattr(cli, "review", lambda *a, **k: (None, [], []))
    monkeypatch.setattr(cli, "context", lambda *a, **k: "brief")
    for argv in (["review", "1" * 25], ["context", "1" * 25], ["comment", "1" * 25]):
        cli._main(argv)


def test_the_responder_reads_the_document(monkeypatch):
    # A reply used to see only the ~60 characters its comment was anchored to, so an
    # author's "we address that in section 3" could be conceded to or restated, but
    # never checked. The responder reads what the commenter read.
    from marginal import reviewer

    seen = {}

    def fake_exchange(system, messages, **kw):
        seen["system"] = system
        seen["blocks"] = messages[0]["content"]
        seen["cache"] = kw.get("cache")
        return {"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn"}

    monkeypatch.setattr(model_mod, "exchange", fake_exchange)
    tab = {"id": "t.0", "text": DOC, "paragraphs": []}
    reviewer.respond("emailed to every resident", [("Author", "we say why below")],
                     "D", _cfg(), tab)
    body = "".join(b.get("text", "") for b in seen["blocks"])
    assert "Response rates in the pilot" in body, "the document never reached the responder"
    assert "we say why below" in body


def test_the_document_carries_the_cache_breakpoint_not_the_thread(monkeypatch):
    """The breakpoint must sit on the part that repeats, not the part that varies.

    Appending the thread before rendering would move it onto the one block that
    changes per thread. Nothing errors, the replies are identical, and every thread
    pays for the whole document again — the failure this test exists to catch is a
    bill, not a traceback.
    """
    from marginal import reviewer

    seen = {}

    def fake_exchange(system, messages, **kw):
        seen["blocks"] = messages[0]["content"]
        seen["cache"] = kw.get("cache")
        return {"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn"}

    monkeypatch.setattr(model_mod, "exchange", fake_exchange)
    tab = {"id": "t.0", "text": DOC, "paragraphs": []}
    reviewer.respond("emailed to every resident", [("Author", "no")], "D", _cfg(), tab)

    blocks = seen["blocks"]
    marked = [i for i, b in enumerate(blocks) if "cache_control" in b]
    assert marked, "no cache breakpoint: every reply would resend the document"
    assert marked == [len(blocks) - 2], f"breakpoint is not on the document: {marked}"
    assert "the thread so far" in blocks[-1]["text"].lower()
    assert seen["cache"] is True, "the system prompt is not a breakpoint"


def test_two_threads_send_the_same_document_bytes(monkeypatch):
    # The cached prefix is a byte comparison. Two threads whose document block
    # differs by a character share nothing, and the symptom is only the price.
    from marginal import reviewer

    sent = []

    def fake_exchange(system, messages, **kw):
        sent.append(messages[0]["content"][:-1])
        return {"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn"}

    monkeypatch.setattr(model_mod, "exchange", fake_exchange)
    tab = {"id": "t.0", "text": DOC, "paragraphs": []}
    for quote, reply in (("emailed to every resident", "no"), ("forty percent", "yes")):
        reviewer.respond(quote, [("Author", reply)], "D", _cfg(), tab)
    assert sent[0] == sent[1], "the cached prefix differs between two threads"


def test_a_comment_in_an_unidentifiable_tab_answers_without_one(monkeypatch):
    # Ambiguity resolves to absent here as everywhere else: a quote in two tabs
    # identifies neither, and a confidently wrong document is worse than none.
    doc = {
        "title": "D",
        "tabs": [
            {"id": "t.0", "text": "shared sentence\nfirst only\n"},
            {"id": "t.1", "text": "shared sentence\nsecond only\n"},
        ],
    }
    assert run._tab_of(doc, "shared sentence") is None
    assert run._tab_of(doc, "second only")["id"] == "t.1"
    assert run._tab_of({"title": "D", "tabs": [{"id": None, "text": "x"}]}, "")["id"] is None


def test_respond_reports_what_the_cache_did(monkeypatch, capsys):
    # The only symptom of a dead breakpoint is the price, so the run has to say.
    from marginal import cli, gdocs

    usage = model_mod.Usage()
    usage.calls, usage.input, usage.cache_read = 2, 10913, 5388
    monkeypatch.setattr(gdocs, "access_token", lambda *a, **k: "tok")
    monkeypatch.setattr(cli.config_mod, "load", lambda *a, **k: _cfg())
    monkeypatch.setattr(
        cli, "respond", lambda *a, **k: ([{"on": "q", "reply": "r"}], usage)
    )
    cli._main(["respond", "1" * 25])
    assert "cache 5388 read" in capsys.readouterr().out


def test_an_unknown_mode_is_refused_at_load(tmp_path):
    import pytest

    p = tmp_path / "cfg.toml"
    p.write_text('mode = "agentic"\n')
    with pytest.raises(ValueError, match="mode"):
        config_mod.load(p)
