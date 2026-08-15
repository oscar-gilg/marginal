"""Letting the commenter look things up, on both routes and in both modes.

Web search is the first capability where the two providers do not merely spell the
same request differently. Anthropic takes a tool definition and runs the search
itself; OpenRouter takes a `web` plugin and splices results in before the model
sees a tool. Passing the Anthropic definition through the OpenAI tool conversion
declares a function this process is expected to execute — the model calls it,
nothing answers, and the run hangs with no error. That is the failure these tests
exist to keep out, and it would land on the route that actually runs here.

The rest pin the two things a server tool changes about the loop: a turn can now
pause mid-search, and a turn can now end in a block type a cache breakpoint may
not be attached to.
"""

import json

from marginal import brief
from marginal import config as config_mod
from marginal import cli as run_cli
from marginal import model, reviewer, run, submission

DOC = (
    "The survey will be emailed to every resident of the district.\n"
    "Response rates in the pilot reached forty percent overall.\n"
    "We therefore expect the full rollout to be representative.\n"
)


def _cfg(**kw):
    return config_mod.Config(**kw)


def _sub(cfg=None, budget=5, notes=None):
    cfg = cfg or _cfg()
    return submission.Submitter(DOC, cfg, cfg.model, budget=budget, notes=notes)


def _turn(*pairs):
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


PAUSED = {
    "content": [
        {"type": "server_tool_use", "id": "srv1", "name": "web_search",
         "input": {"query": "pilot response rates"}},
        {"type": "web_search_tool_result", "tool_use_id": "srv1", "content": []},
    ],
    "stop_reason": "pause_turn",
}

DONE = {"content": [{"type": "text", "text": "Nothing further."}], "stop_reason": "end_turn"}


def _submits(monkeypatch, turns, seen=None):
    calls = iter(turns)

    def fake(system, messages, **kw):
        if seen is not None:
            seen.append({"system": system, "messages": [dict(m) for m in messages], "kw": kw})
        return next(calls, DONE)

    monkeypatch.setattr(model, "exchange", fake)


def _stream(turns, monkeypatch, cfg=None, budget=5, seen=None, sub=None):
    _submits(monkeypatch, turns, seen)
    cfg = cfg or _cfg()
    sub = sub or _sub(cfg, budget)
    return list(reviewer.propose_stream("Doc", DOC, budget, cfg, sub))


def _capture(monkeypatch):
    """Record the request body instead of sending it.

    Patched at `_post`, which is where the two providers' shapes are finally
    decided; stubbing `_openrouter` would prove nothing about the conversion.
    """
    sent = {}

    def fake_post(url, body, headers, timeout=600):
        sent["url"], sent["body"] = url, body
        if "openrouter" in url:
            return {"choices": [{"message": {"content": "ok"}}], "usage": {}}
        return {"content": [{"type": "text", "text": "ok"}], "usage": {}}

    monkeypatch.setattr(model, "_post", fake_post)
    monkeypatch.setenv("OPENROUTER_API_KEY", "x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "y")
    return sent


MSGS = [{"role": "user", "content": [{"type": "text", "text": "ask"}]}]


# --- the two routes ----------------------------------------------------------


def test_the_direct_route_passes_the_server_tool_through_untouched(monkeypatch):
    sent = _capture(monkeypatch)
    tools = [reviewer.SUBMIT_TOOL, model.web_search_tool(3)]
    model.exchange("sys", MSGS, provider="anthropic", tools=tools)
    assert sent["body"]["tools"] == tools, "Anthropic runs the tool; it must arrive as written"


def test_openrouter_never_declares_web_search_as_a_function(monkeypatch):
    # The defect this whole seam exists for. Declared as a function, the model calls
    # `web_search`, nothing in this process answers it, and the commenter waits for
    # a tool result that is never coming — no exception, no note, just a stalled run.
    sent = _capture(monkeypatch)
    model.exchange(
        "sys", MSGS, provider="openrouter",
        tools=[reviewer.SUBMIT_TOOL, model.web_search_tool(3)],
    )
    names = [t["function"]["name"] for t in sent["body"].get("tools", [])]
    assert names == ["submit_comment"], "only tools this process executes may be declared"
    assert sent["body"]["plugins"] == [{"id": "web"}], "search must reach OpenRouter's own path"


def test_a_request_carrying_only_a_server_tool_declares_no_functions(monkeypatch):
    # An empty `tools` array is not the same as no tools: some providers read it as
    # "you may call nothing", and it is not what the caller asked for either way.
    sent = _capture(monkeypatch)
    model.exchange("sys", MSGS, provider="openrouter", tools=[model.web_search_tool(2)])
    assert "tools" not in sent["body"]
    assert sent["body"]["plugins"] == [{"id": "web"}]


def test_no_search_asked_for_means_no_plugin(monkeypatch):
    sent = _capture(monkeypatch)
    model.exchange("sys", MSGS, provider="openrouter", tools=[reviewer.SUBMIT_TOOL])
    assert "plugins" not in sent["body"]


def test_a_searched_turn_is_not_dropped_on_the_way_to_openrouter(monkeypatch):
    """A mid-conversation fallback must not delete an assistant turn.

    `provider="auto"` retries on OpenRouter after any Anthropic failure, so a
    conversation can carry Anthropic-only blocks into a request built in the OpenAI
    shape. Those blocks have no counterpart there; dropping the whole turn silently
    is what must not happen, because the tool_result that follows would then answer
    a call the request no longer contains.
    """
    sent = _capture(monkeypatch)
    history = [
        {"role": "user", "content": [{"type": "text", "text": "ask"}]},
        {"role": "assistant", "content": PAUSED["content"]},
    ]
    model.exchange("sys", history, provider="openrouter")
    roles = [m["role"] for m in sent["body"]["messages"]]
    assert roles == ["system", "user", "assistant"], "the searched turn must survive"


def test_a_turn_of_only_tool_results_is_still_emitted_once(monkeypatch):
    # The guard above tests "nothing renderable in this turn", and a user turn of
    # nothing but tool_results looks exactly like that — its blocks are emitted as
    # `role: tool` messages before the check ever runs. Without counting them the
    # guard appends a second, invented user message after every submission.
    sent = _capture(monkeypatch)
    history = [
        {"role": "user", "content": [{"type": "text", "text": "ask"}]},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "c1", "name": "submit_comment", "input": {}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "c1", "content": "Placed."},
        ]},
    ]
    model.exchange("sys", history, provider="openrouter")
    roles = [m["role"] for m in sent["body"]["messages"]]
    assert roles == ["system", "user", "assistant", "tool"]


# --- the loop ----------------------------------------------------------------


def test_a_paused_turn_is_resumed_rather_than_nudged(monkeypatch):
    # A pause carries no submission, which looks exactly like a commenter with
    # nothing to say. Nudging it interrupts a search still in flight; returning
    # ends a run that had not started.
    seen = []
    pairs = _stream(
        [PAUSED, _turn(("Response rates in the pilot reached forty percent", "Which pilot?"))],
        monkeypatch, cfg=_cfg(web_search=True), seen=seen,
    )
    assert [p[1] for p in pairs] == ["Which pilot?"]
    resumed = seen[1]["messages"]
    assert resumed[-1]["role"] == "assistant", "resuming sends the paused turn back as-is"
    assert not any(
        "submit_comment tool" in json.dumps(m) for m in resumed[1:]
    ), "a paused turn must not be nudged"


def test_endless_pausing_stops_the_run_and_says_so(monkeypatch):
    notes = []
    sub = _sub(_cfg(web_search=True), notes=notes)
    pairs = _stream([PAUSED] * 20, monkeypatch, cfg=_cfg(web_search=True), sub=sub, seen=None)
    assert pairs == []
    assert any("paused" in n for n in notes), "a run that gave up must report why"


def test_the_cache_breakpoint_skips_a_block_that_cannot_carry_one(monkeypatch):
    # The breakpoint used to go on the last block outright. After a search the last
    # block is a `web_search_tool_result`, which the API rejects a breakpoint on —
    # and a rejected request costs the whole run, not one comment.
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "doc"}]},
        {"role": "assistant", "content": [dict(b) for b in PAUSED["content"]]},
    ]
    reviewer._rolling_cache(messages, fixed=0)
    marked = [b for m in messages for b in m["content"] if "cache_control" in b]
    assert all(b.get("type") in reviewer._CACHEABLE for b in marked)


# --- the two modes -----------------------------------------------------------


def _parts(cfg):
    return brief.sections(
        "Doc", DOC, None, cfg, reviewer.COMMENTER_CONTRACT, "HANDOFF", budget=3,
    )


def test_both_modes_are_told_the_same_thing_about_searching(tmp_path):
    # The reason this is a `Section` and not a string built next to the tool: an
    # agent could always search because nothing stopped it, so the capability was
    # already unevenly available. Whether to search is a judgement about reviewing
    # and has to reach both commenters or the modes diverge again.
    parts = _parts(_cfg(web_search=True))
    research = next(s for s in parts if s.name == "research")
    probe = research.text.strip().splitlines()[0]
    system, blocks = brief.as_blocks(parts)
    assert probe in system + json.dumps(blocks), "API mode dropped the research section"
    assert probe in brief.as_text(parts, tmp_path), "agent mode dropped the research section"


def test_a_commenter_that_cannot_search_is_not_told_it_can(tmp_path):
    parts = _parts(_cfg())
    assert [s for s in parts if s.name == "research"] == []


def test_only_the_configured_run_is_given_the_search_tool(monkeypatch):
    on, off = [], []
    _stream([DONE], monkeypatch, cfg=_cfg(web_search=True), seen=on)
    _stream([DONE], monkeypatch, cfg=_cfg(), seen=off)

    def types(seen):
        return [t.get("type") for t in seen[0]["kw"]["tools"]]

    assert model.WEB_SEARCH_TYPE in types(on)
    assert types(off) == [None], "search off means the one submission tool and nothing else"


def test_both_subcommands_carry_the_flag_into_the_config(monkeypatch):
    # A flag that argparse accepts and nothing forwards is the quietest way for a
    # setting to do nothing: `--web-search` would parse, print no error, and the run
    # would review from the document alone.
    seen = {}

    class Stop(Exception):
        pass

    def fake_load(explicit=None, **overrides):
        seen.update(overrides)
        raise Stop

    monkeypatch.setattr(run_cli.config_mod, "load", fake_load)
    for argv in (["review", "d" * 30, "--web-search"], ["context", "d" * 30, "--web-search"]):
        seen.clear()
        try:
            run_cli.main(argv)
        except Stop:
            pass
        assert seen["web_search"] is True, f"{argv[0]} dropped the flag"

    seen.clear()
    try:
        run_cli.main(["review", "d" * 30, "--no-web-search", "--web-search-max-uses", "2"])
    except Stop:
        pass
    assert seen["web_search"] is False
    assert seen["web_search_max_uses"] == 2


# A test that the `batch` schedule reported it could not search lived here. `batch`
# has since been deleted — it decided the whole set in one call and had drifted from
# the pipeline it was meant to be a baseline for — and with it the one schedule where
# the setting could not apply. Every remaining schedule runs a tool loop, so there is
# no longer a case where searching is configured and silently impossible.
