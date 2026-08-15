"""Revision-aware posting regressions.

The idea of gating a write on the document's revision is borrowed from
LucaDeLeo/gdoc; what is asserted here is this package's own behaviour.
"""

import pytest

from marginal import gdocs, post


def _tab(text, revision="rev-1", tab_id="t.0"):
    return {
        "id": tab_id,
        "title": "Tab",
        "text": text,
        "paragraphs": [{"start": 0, "end": len(text), "style": "NORMAL_TEXT"}],
        "revision_id": revision,
    }


def test_read_doc_retains_the_revision_beside_each_coordinate_stream(monkeypatch):
    monkeypatch.setattr(
        gdocs,
        "get_tabs",
        lambda *_: {"title": "Doc", "revisionId": "rev-9", "tabs": [{"raw": True}]},
    )
    monkeypatch.setattr(gdocs, "tab_text", lambda _raw: {"id": "t.0", "text": "text"})
    doc = gdocs.read_doc("doc", "token")
    assert doc["revision_id"] == "rev-9"
    assert doc["tabs"][0]["revision_id"] == "rev-9"


def test_revision_probe_requests_only_revision_id(monkeypatch):
    seen = {}

    def call(url, token):
        seen.update(url=url, token=token)
        return {"revisionId": "rev-2"}

    monkeypatch.setattr(gdocs, "_call", call)
    assert gdocs.get_revision_id("doc", "token") == "rev-2"
    assert "fields=revisionId" in seen["url"]


def test_api_read_without_a_revision_fails_loudly(monkeypatch):
    monkeypatch.setattr(gdocs, "get_tabs", lambda *_: {"title": "Doc", "tabs": []})
    with pytest.raises(RuntimeError, match="revisionId"):
        gdocs.read_doc("doc", "token")


def test_unchanged_revision_reuses_the_exact_tab(monkeypatch):
    original = _tab("a unique quote")
    monkeypatch.setattr(gdocs, "get_revision_id", lambda *_: "rev-1")
    monkeypatch.setattr(gdocs, "read_doc", lambda *_: pytest.fail("unnecessary reread"))
    assert post.api_tab_refresher("doc", "token", original)() is original


def test_changed_revision_moves_a_unique_quote_before_selection(monkeypatch):
    original = _tab("a unique quote")
    fresh = _tab("new prefix; a unique quote", revision="rev-2")
    monkeypatch.setattr(gdocs, "get_revision_id", lambda *_: "rev-2")
    monkeypatch.setattr(
        gdocs,
        "read_doc",
        lambda *_: {"revision_id": "rev-2", "tabs": [fresh]},
    )
    selected = {}

    def select(_page, span, _paragraphs, _strategy, text):
        selected.update(start=span.start, text=text)
        return True, 1, span.quote

    monkeypatch.setattr(post, "select_span", select)
    monkeypatch.setattr(post, "post_comment", lambda *_: None)
    result = post.post_one(
        object(),
        original,
        "a unique quote",
        "body",
        "paragraph",
        fresh_tab=post.api_tab_refresher("doc", "token", original),
    )
    assert result.ok is True
    assert selected == {"start": len("new prefix; "), "text": fresh["text"]}


def test_changed_revision_rejects_a_newly_ambiguous_quote_without_posting(monkeypatch):
    original = _tab("a unique quote")
    fresh = _tab("a unique quote\nand again: a unique quote", revision="rev-2")
    monkeypatch.setattr(gdocs, "get_revision_id", lambda *_: "rev-2")
    monkeypatch.setattr(
        gdocs,
        "read_doc",
        lambda *_: {"revision_id": "rev-2", "tabs": [fresh]},
    )
    monkeypatch.setattr(post, "select_span", lambda *_a, **_k: pytest.fail("selected stale text"))
    monkeypatch.setattr(post, "post_comment", lambda *_: pytest.fail("posted stale text"))
    result = post.post_one(
        object(),
        original,
        "a unique quote",
        "body",
        "paragraph",
        fresh_tab=post.api_tab_refresher("doc", "token", original),
    )
    assert result.ok is False and "SpanError" in result.error


def test_changed_revision_rejects_a_deleted_quote_without_posting(monkeypatch):
    original = _tab("a unique quote")
    fresh = _tab("the passage was removed", revision="rev-2")
    monkeypatch.setattr(gdocs, "get_revision_id", lambda *_: "rev-2")
    monkeypatch.setattr(
        gdocs,
        "read_doc",
        lambda *_: {"revision_id": "rev-2", "tabs": [fresh]},
    )
    monkeypatch.setattr(post, "select_span", lambda *_a, **_k: pytest.fail("selected stale text"))
    monkeypatch.setattr(post, "post_comment", lambda *_: pytest.fail("posted stale text"))
    result = post.post_one(
        object(),
        original,
        "a unique quote",
        "body",
        "paragraph",
        fresh_tab=post.api_tab_refresher("doc", "token", original),
    )
    assert result.ok is False and "SpanError" in result.error


def test_revision_probe_failure_never_falls_back_to_stale_coordinates(monkeypatch):
    original = _tab("a unique quote")
    monkeypatch.setattr(
        gdocs,
        "get_revision_id",
        lambda *_: (_ for _ in ()).throw(OSError("network down")),
    )
    monkeypatch.setattr(post, "select_span", lambda *_a, **_k: pytest.fail("selected stale text"))
    result = post.post_one(
        object(),
        original,
        "a unique quote",
        "body",
        "paragraph",
        fresh_tab=post.api_tab_refresher("doc", "token", original),
    )
    assert result.ok is False and "network down" in result.error


def test_selection_retry_rechecks_revision_and_quote(monkeypatch):
    original = _tab("a unique quote")
    deleted = _tab("the passage was removed", revision="rev-2")
    calls = []

    def fresh():
        calls.append(1)
        return original if len(calls) == 1 else deleted

    monkeypatch.setattr(post, "select_span", lambda *_a, **_k: (False, 1, "wrong"))
    monkeypatch.setattr(post, "post_comment", lambda *_: pytest.fail("posted stale text"))
    monkeypatch.setattr(post.time, "sleep", lambda *_: None)
    result = post.post_one(
        object(),
        original,
        "a unique quote",
        "body",
        "paragraph",
        attempts=2,
        fresh_tab=fresh,
    )
    assert len(calls) == 2
    assert result.ok is False and "SpanError" in result.error


def test_disappearing_tab_is_refused(monkeypatch):
    original = _tab("a unique quote")
    monkeypatch.setattr(gdocs, "get_revision_id", lambda *_: "rev-2")
    monkeypatch.setattr(
        gdocs,
        "read_doc",
        lambda *_: {"revision_id": "rev-2", "tabs": [_tab("other", "rev-2", "t.1")]},
    )
    with pytest.raises(LookupError, match="disappeared"):
        post.api_tab_refresher("doc", "token", original)()


def test_post_many_builds_the_guard_for_direct_api_callers(monkeypatch):
    original = _tab("a unique quote")
    seen = {}
    monkeypatch.setattr(post, "snapshot", lambda *_: set())
    monkeypatch.setattr(post, "verify", lambda run, *_: run)

    def one(_page, _tab, quote, body, strategy, **kwargs):
        seen.update(kwargs)
        return post.Result(quote=quote, body=body, ok=False)

    monkeypatch.setattr(post, "post_one", one)
    post.post_many(
        object(),
        "doc",
        original,
        [("a unique quote", "body")],
        token="token",
        read_comments=lambda: [],
    )
    assert callable(seen["fresh_tab"])
