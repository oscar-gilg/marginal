"""Regressions for the defects an external audit found.

Each of these is a failure that reported success: a wrong anchor that verified as
exact, a fallback that never fired, a run that dropped every comment and exited 0.
That is the class this tool can least afford, which is why they are pinned here
rather than left to the paths that happened to be exercised.
"""

import json
import urllib.error

import pytest

from marginal import anchors, config as config_mod, model, post
from marginal.docs_ui import Span, SpanError
from marginal.post import Result, Run


# --- anchors ---------------------------------------------------------------


def test_a_fuzzy_tie_is_ambiguous_rather_than_first_wins():
    # Two paragraphs the quote resembles equally do not identify one place. Picking
    # the earlier one silently anchors the comment to the wrong paragraph, and the
    # result still verifies as exact because the anchor matches what was selected.
    text = (
        "The pilot reached forty percent in cohort A overall\n"
        "The pilot reached forty percent in cohort B overall\n"
    )
    assert anchors._fuzzy(text, "The pilot reached forty percent in cohort C overall") is None


def test_an_unambiguous_near_match_still_resolves():
    text = "The pilot reached forty percent in cohort A overall\nSomething else entirely\n"
    span = anchors._fuzzy(text, "The pilot reached forty percent in cohort A overal")
    assert span is not None and "cohort A" in span.quote


def test_resolve_reports_a_tie_instead_of_guessing():
    text = (
        "The pilot reached forty percent in cohort A overall\n"
        "The pilot reached forty percent in cohort B overall\n"
    )
    with pytest.raises(SpanError):
        anchors.resolve(text, "The pilot reached forty percent in cohort C overall")


# --- model client ----------------------------------------------------------


def _reply(text: str) -> dict:
    """A provider reply in the shape the transport functions return."""
    return {"content": [{"type": "text", "text": text}], "stop_reason": "end_turn"}


def _as_text(reply) -> str:
    """The reply as text, whether `converse` hands back blocks or a string.

    These tests are about provider selection, not about the reply contract, and
    should not fail the day that contract changes underneath them.
    """
    if isinstance(reply, str):
        return reply
    return "".join(
        b.get("text", "") for b in reply.get("content", []) if b.get("type") == "text"
    )


def test_a_transport_failure_falls_back_to_the_other_provider(monkeypatch):
    # Reaching Anthropic can fail without an HTTP status — DNS, TLS, a read timeout.
    # Those used to escape as URLError, which `provider="auto"` does not catch, so a
    # network hiccup aborted the run with a healthy OpenRouter sitting right there.
    # Patched at the socket, not at `_anthropic`: the conversion happens inside
    # `_post`, so a test that replaces the whole provider proves nothing about it.
    monkeypatch.setattr(model, "_DEAD", {}, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(model, "load_env", lambda *a, **k: None)

    def boom(*a, **k):
        raise urllib.error.URLError("nodename nor servname provided")

    monkeypatch.setattr(model.urllib.request, "urlopen", boom)
    monkeypatch.setattr(model, "_openrouter", lambda *a, **k: _reply("ok"))
    got = model.converse("s", [{"role": "user", "content": "hi"}], provider="auto")
    assert _as_text(got) == "ok"
    # And a transport failure is not a permanent verdict on the provider.
    assert model.dead_providers() == {}


def test_post_turns_transport_errors_into_model_errors(monkeypatch):
    def boom(*a, **k):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(model.urllib.request, "urlopen", boom)
    with pytest.raises(model.ModelError):
        model._post("https://example.invalid", {}, {})


def test_a_bad_model_id_does_not_condemn_the_whole_provider(monkeypatch):
    # A 404 for a mistyped critic model used to mark Anthropic dead for the process,
    # taking the commenter calls — which were working — down with it.
    monkeypatch.setattr(model, "_DEAD", {}, raising=False)
    seen = []

    def anthropic(system, messages, m, *a, **k):
        seen.append(m)
        if m == "claude-typo-9":
            raise model.ModelError("HTTP 404 from anthropic: model not found")
        return _reply("ok")

    monkeypatch.setattr(model, "_anthropic", anthropic)
    monkeypatch.setattr(model, "_openrouter", lambda *a, **k: _reply("fallback"))
    monkeypatch.setattr(model, "load_env", lambda *a, **k: None)
    msgs = [{"role": "user", "content": "hi"}]
    assert _as_text(model.converse("s", msgs, model="claude-typo-9", provider="auto")) == "fallback"
    assert _as_text(model.converse("s", msgs, model="claude-fable-5", provider="auto")) == "ok"
    assert seen == ["claude-typo-9", "claude-fable-5"]


# --- verification ----------------------------------------------------------


def _comment(cid, body, anchored):
    return {"id": cid, "body": body, "anchored": anchored, "author": "bot"}


def test_verification_waits_for_our_comment_not_for_a_count(monkeypatch):
    # A collaborator commenting mid-run used to satisfy the wait by arriving first:
    # one new comment, one expected, so polling stopped and our own comment — still
    # propagating — was reported as never posted.
    monkeypatch.setattr(post.time, "sleep", lambda *_: None)
    reads = [
        [_comment("x", "someone else entirely", "their passage")],
        [
            _comment("x", "someone else entirely", "their passage"),
            _comment("mine", "our comment", "our passage"),
        ],
    ]

    def reader():
        return reads[min(len(seen), len(reads) - 1)] if (seen.append(1) or True) else []

    seen: list = []
    run = Run(doc_id="d", tab_id=None, strategy="paragraph")
    run.results = [Result(quote="our passage", body="our comment", ok=True)]
    post.verify(run, set(), reader)
    assert run.results[0].ok is True
    assert run.results[0].comment_id == "mine"


# --- config ----------------------------------------------------------------


def test_a_named_config_that_is_missing_is_an_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        config_mod.load(tmp_path / "nope.toml")


def test_a_misspelled_schedule_is_refused(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('schedule = "synch"\n')
    with pytest.raises(ValueError, match="schedule"):
        config_mod.load(p)


def test_a_misspelled_source_is_refused(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('source = "brower"\n')
    with pytest.raises(ValueError, match="source"):
        config_mod.load(p)


def test_zero_critic_workers_is_refused(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("critic_workers = 0\n")
    with pytest.raises(ValueError, match="critic_workers"):
        config_mod.load(p)


def test_the_shipped_example_config_loads(tmp_path):
    # The file users copy must satisfy the validator it now goes through.
    src = config_mod.REPO / "marginal.example.toml"
    if src is None or not src.exists():
        pytest.skip("not a source checkout")
    body = "\n".join(
        line for line in src.read_text().splitlines() if not line.strip().startswith("#")
    )
    p = tmp_path / "c.toml"
    p.write_text(body)
    assert config_mod.load(p) is not None


# --- pipeline --------------------------------------------------------------


def test_a_failed_edit_is_a_failed_comment_not_a_missing_one(monkeypatch, tmp_path):
    # The async schedule turned an exception in the editing stage into an omitted
    # comment. A run that lost every one of them then reported zero comments and
    # exited 0, which reads as "nothing worth saying" rather than "the run broke".
    from marginal import run as run_mod
    from marginal.submission import Submitter

    cfg = config_mod.Config(critic=False, critic_workers=2)
    results: list = []
    pairs: list = []
    notes: list = []
    sub = Submitter("t", cfg, "m", notes=notes)

    def explode(quote, draft, *a, **k):
        raise KeyError("unknown")

    monkeypatch.setattr(run_mod, "_edit", explode)
    run_mod._async(
        {"id": None, "text": "t", "paragraphs": []},
        iter([("q1", "d1"), ("q2", "d2")]),
        None,
        results,
        pairs,
        sub,
    )
    assert pairs == []
    assert len(results) == 2
    assert all(r.ok is False and "editing stage failed" in r.error for r in results)


def test_the_exit_code_counts_failed_results(capsys):
    from marginal import cli

    run = Run(doc_id="d", tab_id=None, strategy="paragraph")
    run.results = [Result(quote="q", body="", ok=False, error="editing stage failed: KeyError")]
    assert cli._report(run, [], []) == 1


def test_a_clean_run_still_exits_zero(capsys):
    from marginal import cli

    run = Run(doc_id="d", tab_id=None, strategy="paragraph")
    run.results = [Result(quote="q", body="b", ok=True)]
    assert cli._report(run, [("q", "b")], []) == 0


# --- round two: what the critique of the first round found -----------------


def test_an_intraword_underscore_is_not_markdown():
    # `foo_bar` is an identifier, not italics. Dropping the underscore produced
    # `foobar`, which could match an unrelated passage and verify as exact.
    assert anchors.to_plain("**foo_bar** matters") == "foo_bar matters"
    assert anchors.to_plain("_emphasis_ matters") == "emphasis matters"
    assert anchors.to_plain("`code` matters") == "code matters"
    assert anchors.to_plain("snake_case_name") == "snake_case_name"


def test_stripping_does_not_redirect_an_anchor():
    text = "The threshold foo_bar governs the cutoff\nUnrelated foobar appears here\n"
    span, _ = anchors.resolve(text, "The threshold **foo_bar** governs the cutoff")
    assert span.quote.startswith("The threshold foo_bar")


def test_a_near_tie_is_ambiguous_not_just_an_exact_tie():
    # 0.98 against 0.97 is still a coin toss between two passages; the ratios do not
    # have to collide for the choice to be arbitrary.
    text = (
        "Response rates in the pilot reached forty percent in cohort A\n"
        "Response rates in the pilot reached forty percent in cohort B\n"
    )
    assert anchors._fuzzy(text, "Response rates in the pilot reached forty pecent in cohort") is None


def test_submission_failures_are_not_retried(monkeypatch):
    # Cmd+Enter can reach Docs while the CDP response is lost. Retrying posts a
    # second copy that verification then claims as the first — success reported, a
    # duplicate left live in the document.
    calls = {"select": 0, "post": 0}

    def select(*a, **k):
        calls["select"] += 1
        return True, 1, "quoted span here"

    def post_comment(*a, **k):
        calls["post"] += 1
        raise RuntimeError("websocket closed")

    monkeypatch.setattr(post, "select_span", select)
    monkeypatch.setattr(post, "post_comment", post_comment)
    monkeypatch.setattr(post, "resolve_quote", lambda text, q: Span(0, len(q), q))
    tab = {"text": "quoted span here and more", "paragraphs": []}
    r = post.post_one(object(), tab, "quoted span here", "body", "paragraph")
    assert r.ok is False
    assert calls["post"] == 1  # not retried
    assert "may have reached the document" in r.error


def test_selection_failures_are_still_retried(monkeypatch):
    calls = {"select": 0}

    def select(*a, **k):
        calls["select"] += 1
        return (calls["select"] == 3), 1, "quoted span here"

    monkeypatch.setattr(post, "select_span", select)
    monkeypatch.setattr(post, "post_comment", lambda *a, **k: None)
    monkeypatch.setattr(post, "resolve_quote", lambda text, q: Span(0, len(q), q))
    monkeypatch.setattr(post.time, "sleep", lambda *_: None)
    tab = {"text": "quoted span here and more", "paragraphs": []}
    r = post.post_one(object(), tab, "quoted span here", "body", "paragraph")
    assert r.ok is True and calls["select"] == 3


def test_identical_bodies_keep_their_own_anchors(monkeypatch):
    # Two comments sharing a body but anchored differently were paired by API return
    # order, so both were reported as mismatched though both landed correctly.
    monkeypatch.setattr(post.time, "sleep", lambda *_: None)
    live = [
        _comment("second", "same body", "passage B"),
        _comment("first", "same body", "passage A"),
    ]
    run = Run(doc_id="d", tab_id=None, strategy="paragraph")
    run.results = [
        Result(quote="passage A", body="same body", ok=True),
        Result(quote="passage B", body="same body", ok=True),
    ]
    post.verify(run, set(), lambda: live)
    assert [r.comment_id for r in run.results] == ["first", "second"]
    assert all(r.ok for r in run.results)


def test_a_duplicate_of_an_existing_body_is_still_seen_as_new(monkeypatch):
    # The browser source carries no ids, so every comment there keys as (None, body).
    # Commenting with a body the document already had made the new one look
    # pre-existing: filtered out, then reported as never posted.
    monkeypatch.setattr(post.time, "sleep", lambda *_: None)
    existing = {"id": None, "body": "same body", "anchored": "their passage"}
    mine = {"id": None, "body": "same body", "anchored": "my passage"}

    before = post.snapshot(lambda: [existing])
    run = Run(doc_id="d", tab_id=None, strategy="paragraph")
    run.results = [Result(quote="my passage", body="same body", ok=True)]
    post.verify(run, before, lambda: [existing, mine])
    assert run.results[0].ok is True
    assert run.results[0].anchored == "my passage"


def test_a_snapshot_from_an_older_caller_still_works(monkeypatch):
    # `run.py` passes `set()` for a dry run; `verify` should not care which it gets.
    monkeypatch.setattr(post.time, "sleep", lambda *_: None)
    run = Run(doc_id="d", tab_id=None, strategy="paragraph")
    run.results = [Result(quote="p", body="b", ok=True)]
    post.verify(run, set(), lambda: [{"id": "x", "body": "b", "anchored": "p"}])
    assert run.results[0].comment_id == "x"


def test_a_bad_tab_is_refused_rather_than_guessed():
    from marginal.run import _pick_tab

    doc = {"tabs": [{"id": "t.0", "text": "a"}, {"id": "t.1", "text": "b"}]}
    with pytest.raises(LookupError):
        _pick_tab(doc, "typo")
    assert _pick_tab(doc, "t.1")["id"] == "t.1"


def test_a_multi_tab_document_with_no_tab_named_is_refused():
    """`tabs[0]` was the default, and it reviewed a tab nobody asked for.

    Silently, too: every anchor placed in the wrong tab verifies byte-exact, so the
    run reported a clean success. One tab is still unambiguous and still resolves.
    """
    from marginal.run import _pick_tab

    many = {"tabs": [{"id": "t.0", "title": "First", "text": "a"},
                     {"id": "t.1", "title": "Second", "text": "b"}]}
    with pytest.raises(LookupError) as e:
        _pick_tab(many, None)
    assert "t.1" in str(e.value) and "Second" in str(e.value)
    assert _pick_tab({"tabs": [{"id": "t.0", "text": "a"}]}, None)["id"] == "t.0"
    # The browser source reports no ids, so a single unidentified tab still resolves.
    assert _pick_tab({"tabs": [{"id": None, "text": "a"}]}, "t.9")["id"] is None


def test_the_tab_in_a_pasted_url_is_not_dropped():
    """The tab id lives in the URL a person copies; the parse read only the doc id.

    The run then named no tab and fell through to the first one — a whole review
    posted on the wrong tab of a multi-tab document.
    """
    from marginal.gdocs import doc_id_from_url, tab_from_url

    url = "https://docs.google.com/document/d/1SYNTHETICDOCID/edit?tab=t.7fake0tabid"
    assert doc_id_from_url(url) == "1SYNTHETICDOCID"
    assert tab_from_url(url) == "t.7fake0tabid"
    # Some Docs URLs carry it after the fragment instead.
    assert tab_from_url("https://docs.google.com/document/d/abc/edit#tab=t.0") == "t.0"
    assert tab_from_url("https://docs.google.com/document/d/abc/edit") is None
    assert tab_from_url("1SYNTHETICDOCID") is None


def test_the_browser_export_is_scoped_to_one_tab(monkeypatch):
    """Unscoped, /export returns the whole document, not the tab.

    The tab dict still said it was one tab, so on a multi-tab document every offset
    past the first tab addressed the wrong characters — a wrong anchor that
    verifies. Checked against the API on a multi-tab document: the scoped export and
    `documents.get` agree exactly, where the unscoped export is the whole file.
    """
    from marginal import browser_source as bs

    urls = []

    def fake_download(page, url, timeout):
        urls.append(url)
        return url.encode()

    monkeypatch.setattr(bs, "_download", fake_download)
    bs._CACHE.clear()
    try:
        bs.export(None, "doc1", "docx", tab_id="t.5")
        assert urls[-1].endswith("export?format=docx&tab=t.5")
        # A second tab of the same document must not be served the first one's blob.
        bs.export(None, "doc1", "docx", tab_id="t.6")
        assert urls[-1].endswith("&tab=t.6")
        assert len(urls) == 2
        # And the cache still works within one tab.
        bs.export(None, "doc1", "docx", tab_id="t.6")
        assert len(urls) == 2
        # No tab named: unchanged, no stray `&tab=None`.
        bs.export(None, "doc1", "docx")
        assert urls[-1].endswith("export?format=docx")
    finally:
        bs._CACHE.clear()


def test_the_submit_brief_omits_the_tab_flag_rather_than_naming_none():
    # It interpolated the string "None", giving the subagent a command line that
    # names a tab no document has.
    from marginal.config import Config
    from marginal.run import submit_brief

    cfg = Config()
    assert "--tab None" not in submit_brief("doc1", "tok", cfg, None)
    assert "post-batch doc1 --from -" in submit_brief("doc1", "tok", cfg, None)
    assert "--tab t.7" in submit_brief("doc1", "tok", cfg, "t.7")


def test_an_impossible_length_band_is_refused_at_load(tmp_path):
    # It used to raise inside `critique_system`, where `critic.tighten` caught it and
    # kept the draft — a bad band silently disabled the editing pass.
    p = tmp_path / "c.toml"
    p.write_text("min_words = 90\nmax_words = 60\n")
    with pytest.raises(ValueError, match="length band"):
        config_mod.load(p)


def test_a_fractional_worker_count_is_refused(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("critic_workers = 1.5\n")
    with pytest.raises(ValueError, match="critic_workers"):
        config_mod.load(p)


def test_usage_and_json_stay_serialisable():
    # `dead_providers` is keyed by a tuple now; anything that reports it must not
    # assume a JSON-encodable mapping without converting.
    assert json.dumps({f"{p}:{m}": v for (p, m), v in model.dead_providers().items()}) is not None
