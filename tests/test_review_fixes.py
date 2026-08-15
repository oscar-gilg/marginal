"""Regressions for defects found reviewing the least-tested paths."""

import xml.etree.ElementTree as ET

from marginal import attribution
from marginal import config as config_mod
from marginal.browser_source import W, _live_text


def test_a_human_bold_opening_is_not_mistaken_for_ours():
    # Otherwise `respond` skips a human reply believing we wrote it.
    for body in ("**Note**\nthis is wrong", "**Important**\nsee below", "**TODO**\nfix"):
        assert not attribution.is_ours(body)


def test_model_shaped_labels_are_recognised():
    for model in ("claude-fable-5", "gpt-6-mini", "opus-5", "haiku-4-5"):
        assert attribution.is_ours(attribution.apply("point", model))


def test_header_beginning_with_the_placeholder_still_owns_its_comments():
    header = "{model} says:"
    body = attribution.apply("point", "claude-opus-5", header=header)
    assert body.startswith("opus-5 says:")
    assert attribution.is_ours(body, header=header)
    assert not attribution.is_ours("a human wrote this", header=header)


def test_unknown_config_key_names_itself(tmp_path):
    p = tmp_path / "bad.toml"
    p.write_text('modle = "typo"\n')
    try:
        config_mod.load(p)
    except ValueError as e:
        assert "modle" in str(e)
    else:
        raise AssertionError("an unknown setting should be rejected by name")


def test_config_expands_a_home_relative_credential_path(tmp_path):
    p = tmp_path / "cfg.toml"
    p.write_text('credentials = "~/creds/bot.json"\n')
    assert "~" not in str(config_mod.load(p).credentials)


def test_comment_body_keeps_its_line_breaks():
    """The docx stores each line of a comment as its own `w:p`.

    Concatenating runs dropped the newline, so every comment carrying an attribution
    header failed body matching during verification — while posting perfectly well.
    """
    from marginal.browser_source import _comment_text

    xml = (
        f'<w:comment xmlns:w="{W[1:-1]}">'
        f"<w:p><w:r><w:t>**fable-5**</w:t></w:r></w:p>"
        f"<w:p><w:r><w:t>The body of the comment.</w:t></w:r></w:p>"
        f"</w:comment>"
    )
    assert _comment_text(ET.fromstring(xml)) == "**fable-5**\nThe body of the comment."


def test_docx_text_skips_suggested_deletions():
    # Counting struck-through text would shift every offset after a suggestion.
    xml = (
        f'<w:p xmlns:w="{W[1:-1]}">'
        f"<w:r><w:t>kept </w:t></w:r>"
        f"<w:del><w:r><w:t>deleted </w:t></w:r></w:del>"
        f"<w:r><w:t>also kept</w:t></w:r>"
        f"</w:p>"
    )
    assert _live_text(ET.fromstring(xml)) == "kept also kept"


def test_browser_source_agent_mode_needs_no_google_credentials(monkeypatch, capsys):
    # `source = "browser"` exists to run with no Google credentials at all, but the
    # CLI minted a token before dispatch, so the credential-free path failed first.
    from marginal import cli, gdocs

    def refuse(*a, **k):
        raise AssertionError("minted a Google token on the credential-free path")

    monkeypatch.setattr(gdocs, "access_token", refuse)
    monkeypatch.setattr(cli, "context", lambda *a, **k: "the context")
    cfg = config_mod.Config(source="browser")
    monkeypatch.setattr(cli.config_mod, "load", lambda *a, **k: cfg)

    assert cli.main(["context", "1" * 25]) == 0
    assert "the context" in capsys.readouterr().out


def test_report_prints_comment_lengths(capsys):
    from marginal import cli

    cli._report(None, [("q", "one two three"), ("q2", "word " * 90)], [], ceiling=80)
    out = capsys.readouterr().out
    assert "[3, 90]" in out
    assert "1 over the 80-word ceiling" in out


def test_the_ceiling_reported_follows_the_configured_band(capsys):
    # The band is a setting; a report that kept quoting 80 would contradict the
    # prompt the critic was actually given.
    from marginal import cli

    cli._report(None, [("q", "word " * 30)], [], ceiling=25)
    assert "1 over the 25-word ceiling" in capsys.readouterr().out


def test_commenter_effort_is_validated_and_reaches_the_model(monkeypatch, tmp_path):
    """A typo in `effort` must fail at load, and a good one must reach `exchange`.

    Silently dropping it would look exactly like a run at the provider default: the
    same comments, the same exit code, no error anywhere.
    """
    import pytest

    from marginal import model as model_mod
    from marginal import reviewer

    p = tmp_path / "cfg.toml"
    p.write_text('effort = "hihg"\n')
    with pytest.raises(ValueError, match="effort"):
        config_mod.load(p)

    p.write_text('effort = "high"\n')
    cfg = config_mod.load(p)
    assert cfg.effort == "high"

    seen = {}

    def fake_exchange(system, messages, **kw):
        seen.update(kw)
        return {"content": [], "stop_reason": "end_turn"}

    monkeypatch.setattr(reviewer.model, "exchange", fake_exchange)
    monkeypatch.setattr(model_mod, "exchange", fake_exchange)

    class Submitter:
        def note(self, *a, **k):
            pass

        def submit(self, *a, **k):
            pass

    list(reviewer.propose_stream("Doc", "Some text.\n", 1, cfg, Submitter()))
    assert seen.get("effort") == "high"


DOC_TEXT = (
    "The survey will be emailed to every resident of the district.\n"
    "Response rates in the pilot reached forty percent overall.\n"
)
QUOTE = "The survey will be emailed to every resident of the district."


class _FakePage:
    def close(self):
        pass


def _batch_scaffold(monkeypatch):
    """Everything `post_batch` touches except the editing pass and the browser."""
    from marginal import ledger
    from marginal import run as runmod
    from marginal.post import Result, Run

    tab = {"id": "t.0", "text": DOC_TEXT, "paragraphs": []}
    monkeypatch.setattr(runmod, "read_document", lambda *a, **k: {"title": "D", "tabs": [tab]})
    monkeypatch.setattr(ledger, "record", lambda *a, **k: None)
    monkeypatch.setattr(runmod, "open_doc", lambda *a, **k: _FakePage())
    posted: list[tuple[str, str]] = []

    def fake_post_many(
        page, doc_id, tab, headed, token, strategy, read_comments=None, **_kwargs
    ):
        posted.extend(headed)
        run = Run(doc_id=doc_id, tab_id=tab["id"], strategy=strategy)
        run.results = [Result(quote=q, body=b, ok=True) for q, b in headed]
        return run

    monkeypatch.setattr(runmod, "post_many", fake_post_many)
    return posted


def test_agent_mode_posts_are_edited_by_the_code_not_by_a_prompt(monkeypatch):
    # The defect: agent mode's editing pass lived in the `submit-brief` prompt, so
    # anything reaching `post-batch` without having read that prompt posted the raw
    # body — no error, no note, and no symptom but a comment over the word band.
    from marginal import critic
    from marginal import run as runmod

    posted = _batch_scaffold(monkeypatch)
    monkeypatch.setattr(
        critic, "tighten", lambda body, quote, cfg, usage=None: ("tightened", "a clause")
    )
    _run, _pairs, notes = runmod.post_batch(
        "d", "tok", config_mod.Config(header=""),
        [{"quote": QUOTE, "comment": "a long unedited body"}],
        tab_id=None,
    )
    assert posted and posted[0][1] == "tightened", posted
    assert any("tightened" in n for n in notes), notes


def test_a_critic_outage_keeps_the_comment_on_every_schedule(monkeypatch):
    # The same failure used to have three answers, picked by a setting that is meant
    # to change only what overlaps what: `_post` posted the comment unedited, `_async`
    # recorded a failed result and dropped it, and `_sync` had no handler and took the
    # run down. `Submitter.critique` answers it once now.
    from marginal import critic
    from marginal import run as runmod
    from marginal.submission import Submitter

    def boom(body, quote, cfg, usage=None):
        raise RuntimeError("529 from the editing model")

    monkeypatch.setattr(critic, "tighten", boom)
    cfg = config_mod.Config(header="", critic=True, critic_workers=2)

    for schedule in ("sync", "async"):
        results: list = []
        pairs: list = []
        notes: list = []
        sub = Submitter("t", cfg, "m", notes=notes)
        stages = runmod._sync if schedule == "sync" else runmod._async
        stages(
            {"id": None, "text": "t", "paragraphs": []},
            iter([("q1", "d1"), ("q2", "d2")]),
            None,
            results,
            pairs,
            sub,
        )
        assert [b for _q, b in pairs] == ["d1", "d2"], (schedule, pairs)
        assert results == [], (schedule, results)
        assert sum("editing stage failed" in n for n in notes) == 2, (schedule, notes)


def test_a_critic_outage_still_posts_the_comment(monkeypatch):
    # `critic.tighten` swallows its own failures; moving the call must not turn an
    # editing outage into a lost comment.
    from marginal import critic
    from marginal import run as runmod

    posted = _batch_scaffold(monkeypatch)

    def boom(body, quote, cfg, usage=None):
        raise RuntimeError("529")

    monkeypatch.setattr(critic, "tighten", boom)
    runmod.post_batch(
        "d", "tok", config_mod.Config(header=""),
        [{"quote": QUOTE, "comment": "the body"}],
        tab_id=None,
    )
    assert posted and posted[0][1] == "the body"


def test_a_dry_run_says_its_preview_is_pre_edit(monkeypatch):
    # A dry run never reaches the editing pass, so the wording it prints is not the
    # wording that would post. Stated rather than left to be discovered.
    from marginal import run as runmod

    _batch_scaffold(monkeypatch)
    _run, pairs, notes = runmod.post_batch(
        "d", "tok", config_mod.Config(header=""),
        [{"quote": QUOTE, "comment": "the body"}],
        tab_id=None, dry_run=True,
    )
    assert pairs and any("before the editing pass" in n for n in notes), notes


def test_config_view_names_the_file_each_setting_came_from(tmp_path, capsys, monkeypatch):
    # Two config files disagreeing is the ordinary way a run does something you did
    # not ask for, and the value alone does not say which one won.
    from marginal import cli
    from marginal import config as config_mod_

    user = tmp_path / "user.toml"
    user.write_text('max_words = 55\nmodel = "claude-opus-5"\n')
    local = tmp_path / "marginal.toml"
    local.write_text("max_words = 30\n")
    monkeypatch.setattr(config_mod_, "USER_CONFIG", user)
    monkeypatch.setattr(config_mod_, "LOCAL_CONFIG", local)

    cli.main(["config", "--critic-model", "m"])
    out = capsys.readouterr().out
    assert "max_words            30  ← " + str(local) in out, out
    assert "model                claude-opus-5  ← " + str(user) in out, out
    assert "critic_model         m  ← flag" in out, out


def test_config_view_names_every_setting(capsys):
    # The grouping is written by hand; the list of settings must not be. Eight were
    # missing, so `config` answered "what would this run use" without mentioning the
    # setting that decides whether the anchor ladder has a model rung.
    from dataclasses import fields

    from marginal import cli

    cli.main(["config"])
    out = capsys.readouterr().out
    missing = [f.name for f in fields(config_mod.Config) if f.name not in out]
    assert not missing, f"settings a run uses but `config` does not print: {missing}"


def test_config_view_is_plain_text_when_it_is_not_a_terminal(capsys):
    # Colour belongs to a terminal; a redirect or a pipe must not collect escapes.
    from marginal import cli

    cli.main(["config"])
    assert "\033[" not in capsys.readouterr().out


# --- respond: one rendering of the document per tab ------------------------
#
# The document block is built once and handed to every thread. Both ways that can go
# wrong are silent — a reply answered against another thread's transcript, or against
# a tab the comment is not in — so each has a test.


def _respond_run(monkeypatch, tabs, comments):
    """Run `run.respond` over `comments` with no network. Returns (sent, fetches)."""
    from marginal import figures, gdocs, ledger, model as model_mod, run as run_mod

    sent: list[list[dict]] = []
    fetches: list[int] = []

    def fake_exchange(system, messages, **kw):
        sent.append(messages[0]["content"])
        return {"content": [{"type": "text", "text": "reply"}], "stop_reason": "end_turn"}

    def fake_fetch(figs, limit, max_bytes):
        fetches.append(len(figs))
        return [{"at": 0, "alt": "", "media_type": "image/png", "data": "AAAA"}]

    monkeypatch.setattr(model_mod, "exchange", fake_exchange)
    monkeypatch.setattr(figures, "fetch", fake_fetch)
    monkeypatch.setattr(figures, "blocks", lambda imgs: [
        {"type": "image", "source": {"type": "base64", "media_type": i["media_type"],
                                     "data": i["data"]}} for i in imgs
    ])
    monkeypatch.setattr(run_mod, "read_document", lambda *a, **k: {"title": "D", "tabs": tabs})
    monkeypatch.setattr(gdocs, "list_comments", lambda *a, **k: comments)
    monkeypatch.setattr(ledger, "rows", lambda *a, **k: [])
    monkeypatch.setattr(ledger, "record", lambda *a, **k: None)
    monkeypatch.setattr(gdocs, "create_reply", lambda *a, **k: {"id": "r"})

    out, _usage = run_mod.respond("doc", "tok", config_mod.Config(), dry_run=True)
    assert len(out) == len(sent) == len(comments)
    return sent, fetches


def _thread(cid, quoted, reply):
    return {
        "id": cid,
        "content": "**opus-5**\n\nour point",
        "quotedFileContent": {"value": quoted},
        "replies": [{"content": reply, "author": {"displayName": "A"}}],
    }


def test_answering_two_threads_downloads_the_figures_once(monkeypatch):
    # `reviewer.respond` used to build its own document block per call, so every
    # thread re-downloaded every figure to send the same bytes. Nothing failed and
    # nothing was mis-answered; the run just paid for it again each time.
    tab = {"id": "t.0", "text": "alpha line\nbeta line\n", "paragraphs": [],
           "figures": [{"uri": "https://x/1.png", "at": 0}]}
    sent, fetches = _respond_run(
        monkeypatch, [tab],
        [_thread("c1", "alpha line", "why?"), _thread("c2", "beta line", "how?")],
    )
    assert len(fetches) == 1, f"figures fetched {len(fetches)} times for one document"
    # And the cached prefix really is shared: identical bytes up to the thread block.
    assert sent[0][:-1] == sent[1][:-1]


def test_a_second_thread_does_not_carry_the_first_ones_transcript(monkeypatch):
    # The blocks are reused across threads, so appending the thread to them would
    # send thread two the document plus thread one's conversation — a reply answering
    # a question nobody asked it, with no error anywhere.
    tab = {"id": "t.0", "text": "alpha line\nbeta line\n", "paragraphs": [], "figures": []}
    sent, _ = _respond_run(
        monkeypatch, [tab],
        [_thread("c1", "alpha line", "first question"),
         _thread("c2", "beta line", "second question")],
    )
    second = "".join(b.get("text", "") for b in sent[1])
    assert "second question" in second
    assert "first question" not in second, "thread one's transcript leaked into thread two"
    assert len(sent[0]) == len(sent[1]), "the two requests are not the same shape"


def test_each_tab_is_rendered_for_the_threads_anchored_in_it(monkeypatch):
    # One document block for the whole run would answer a comment in the second tab
    # against the first tab's text — confidently, and about the wrong document.
    tabs = [
        {"id": "t.0", "text": "alpha only\n", "paragraphs": [], "figures": []},
        {"id": "t.1", "text": "beta only\n", "paragraphs": [], "figures": []},
    ]
    sent, _ = _respond_run(
        monkeypatch, tabs,
        [_thread("c1", "alpha only", "q"), _thread("c2", "beta only", "q")],
    )
    first = "".join(b.get("text", "") for b in sent[0])
    second = "".join(b.get("text", "") for b in sent[1])
    assert "alpha only" in first and "beta only" not in first
    assert "beta only" in second and "alpha only" not in second
