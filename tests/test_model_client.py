"""The provider-neutral client: caching, effort and usage on both transports.

Shapes adopted from an earlier model client of mine, which had already worked out
that OpenRouter passes Anthropic `cache_control` through for Anthropic-backed
models — so the fallback route caches too, rather than silently paying full price.
"""

import json

from marginal import model


def _reply(text):
    """A transport reply in the block shape `exchange` returns."""
    return {"content": [{"type": "text", "text": text}], "stop_reason": "end_turn"}


def _capture(monkeypatch):
    """Record the request body instead of sending it."""
    sent = {}

    def fake_post(url, body, headers, timeout=600):
        sent["url"], sent["body"] = url, body
        if "openrouter" in url:
            return {"choices": [{"message": {"content": "{}"}}],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 5}}
        return {"content": [{"type": "text", "text": "{}"}],
                "usage": {"input_tokens": 100, "output_tokens": 5}}

    monkeypatch.setattr(model, "_post", fake_post)
    monkeypatch.setenv("OPENROUTER_API_KEY", "x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "y")
    return sent


MSGS = [{"role": "user", "content": [
    {"type": "text", "text": "doc", "cache_control": {"type": "ephemeral"}},
    {"type": "text", "text": "ask"},
]}]


def test_openrouter_preserves_cache_breakpoints(monkeypatch):
    # Flattening the blocks here is what made caching a no-op on the only route
    # that works when the Anthropic key has no credit.
    sent = _capture(monkeypatch)
    model.converse("sys", MSGS, model="claude-opus-5", provider="openrouter", cache=True)
    content = sent["body"]["messages"][-1]["content"]
    assert isinstance(content, list), "content blocks must survive the OpenRouter route"
    assert content[0]["cache_control"] == {"type": "ephemeral"}
    assert sent["body"]["cache_control"] == {"type": "ephemeral"}


def test_openrouter_uses_its_own_spelling_of_effort(monkeypatch):
    sent = _capture(monkeypatch)
    model.converse("sys", MSGS, model="claude-opus-5", provider="openrouter", effort="medium")
    assert sent["body"]["reasoning"] == {"effort": "medium"}
    assert "output_config" not in sent["body"], "output_config is Anthropic-native"


def test_anthropic_uses_output_config_for_effort(monkeypatch):
    sent = _capture(monkeypatch)
    model.converse("sys", MSGS, model="claude-opus-5", provider="anthropic",
                   effort="medium", cache=True)
    assert sent["body"]["output_config"] == {"effort": "medium"}
    assert "reasoning" not in sent["body"], "reasoning is OpenRouter's spelling"
    assert sent["body"]["system"][0]["cache_control"] == {"type": "ephemeral"}


def test_blocks_are_flattened_for_non_anthropic_models(monkeypatch):
    # A non-Anthropic model would reject cache_control blocks outright.
    sent = _capture(monkeypatch)
    model.converse("sys", MSGS, model="llama-3", provider="openrouter", cache=True)
    assert isinstance(sent["body"]["messages"][-1]["content"], str)
    assert "cache_control" not in sent["body"]


def test_both_transports_report_the_same_totals():
    # Anthropic's input_tokens excludes cached tokens; OpenRouter's prompt_tokens
    # includes them. Adding them the same way double-counts the prefix and hides
    # a working cache behind a 0% hit rate.
    a = model.Usage()
    a.add({"input_tokens": 100, "cache_read_input_tokens": 900, "output_tokens": 50})
    o = model.Usage()
    o.add({"prompt_tokens": 1000, "completion_tokens": 50,
           "prompt_tokens_details": {"cached_tokens": 900}})
    assert (a.input, a.cache_read, a.output) == (100, 900, 50)
    assert (o.input, o.cache_read, o.output) == (100, 900, 50)
    assert "90%" in a.line() and "90%" in o.line()


def test_openrouter_cache_write_tokens_are_counted():
    u = model.Usage()
    u.add({"prompt_tokens": 1000, "prompt_tokens_details": {"cache_write_tokens": 400}})
    assert u.cache_write == 400
    assert u.input == 600, "the written prefix must not also be billed as fresh input"


def test_a_permanently_dead_provider_is_tried_once(monkeypatch):
    # An unfunded key answers fast but never succeeds; retrying it before every
    # call cost a doomed round trip per model call for the whole run.
    monkeypatch.setattr(model, "_DEAD", {}, raising=False)
    tries = {"anthropic": 0, "openrouter": 0}

    def anthropic(*a, **k):
        tries["anthropic"] += 1
        raise model.ModelError("HTTP 400 from anthropic: credit balance is too low")

    def openrouter(*a, **k):
        tries["openrouter"] += 1
        return _reply("ok")

    monkeypatch.setattr(model, "_anthropic", anthropic)
    monkeypatch.setattr(model, "_openrouter", openrouter)
    for _ in range(5):
        assert model.converse("s", MSGS, provider="auto") == "ok"
    assert tries == {"anthropic": 1, "openrouter": 5}
    # Keyed by (provider, model): a route can be dead for one model and fine for
    # another, so disabling it wholesale would be too broad.
    assert ("anthropic", model.DEFAULT_MODEL) in model.dead_providers()


def test_a_busy_provider_is_retried(monkeypatch):
    # 429 and 5xx mean try again later, not give up for the process.
    monkeypatch.setattr(model, "_DEAD", {}, raising=False)
    tries = {"n": 0}

    def anthropic(*a, **k):
        tries["n"] += 1
        raise model.ModelError("HTTP 529 from anthropic: overloaded")

    monkeypatch.setattr(model, "_anthropic", anthropic)
    monkeypatch.setattr(model, "_openrouter", lambda *a, **k: _reply("ok"))
    for _ in range(3):
        model.converse("s", MSGS, provider="auto")
    assert tries["n"] == 3, "a transient failure must not disable the route"
    assert model.dead_providers() == {}
