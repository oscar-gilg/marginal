"""Model access — two providers, one function.

Claude Fable 5 is the default: it writes good document comments, and it is what
earlier work on this corpus used.

Two providers because a direct Anthropic key is not always usable — an unfunded
or absent one fails every call — while OpenRouter routes to the same models.
Direct is preferred when it works: fewer hops, real `stop_reason` semantics. So
`provider="auto"` tries it and falls back.

The fallback is not a degraded route. Prompt caching and reasoning effort both
work through OpenRouter for Anthropic-backed models; only the spelling differs
(`cache_control` passes through either way, `reasoning.effort` against
`output_config.effort`). Shapes and the usage normalisation are adopted from
an earlier model client of mine, which had already established that a caller cannot
add the two providers' token counts the same way without double-counting.

Raw HTTPS rather than the SDK keeps the dependency list at one small wheel. If
this grows to need streaming, retries with jitter, or tool use, switch to the
`anthropic` SDK rather than growing this file.
"""

from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
API_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-fable-5"


@dataclass
class Usage:
    """Running token totals for a run. Optional, and cheap enough to always pass.

    Exists to make caching checkable. A cache breakpoint that silently does nothing
    — because the prefix moved, or the prompt is under the model's minimum — costs
    real money and reports no error, so the only way to know it works is to read
    `cache_read` back. Zero reads across a multi-turn run means it is not working.
    """

    calls: int = 0
    input: int = 0
    output: int = 0
    cache_write: int = 0
    cache_read: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, u: dict | None) -> None:
        """Fold one provider `usage` object in, whichever transport produced it.

        Normalisation carried over from that earlier client, including the
        subtraction that matters: Anthropic reports `input_tokens` *excluding*
        cached tokens, while OpenRouter's `prompt_tokens` is the total and carries
        the cached share in `prompt_tokens_details`. Adding them without
        subtracting double-counts the prefix and makes a working cache look like
        it read nothing.
        """
        if not u:
            return
        details = u.get("prompt_tokens_details") or {}
        write = int(
            u.get("cache_creation_input_tokens") or details.get("cache_write_tokens") or 0
        )
        read = int(u.get("cache_read_input_tokens") or details.get("cached_tokens") or 0)
        if "input_tokens" in u:
            fresh = int(u.get("input_tokens") or 0)
        else:
            fresh = max(0, int(u.get("prompt_tokens") or 0) - write - read)
        with self._lock:  # critic workers report concurrently
            self.calls += 1
            self.input += fresh
            self.output += int(u.get("output_tokens") or u.get("completion_tokens") or 0)
            self.cache_write += write
            self.cache_read += read

    def line(self) -> str:
        total = self.input + self.cache_write + self.cache_read
        hit = f"{self.cache_read / total:.0%}" if total else "n/a"
        return (
            f"{self.calls} calls, {total} in / {self.output} out, "
            f"cache {self.cache_read} read + {self.cache_write} written ({hit} of input)"
        )


class ModelError(RuntimeError):
    pass


# --- server-side tools -------------------------------------------------------
#
# A server tool is one the provider runs itself: the model asks, the provider
# answers, and nothing in this process executes anything. That distinction is what
# the two routes disagree about, so it is drawn here once rather than at each call
# site — see `_openrouter_plugins`.

# The dynamic-filtering variant, which needs Fable 5, Opus 4.6+ or Sonnet 4.6+.
# Every model this tool defaults to qualifies; an older one would need the basic
# `web_search_20250305` instead, which is why the string is a constant and not
# buried in a dict literal.
WEB_SEARCH_TYPE = "web_search_20260209"


def web_search_tool(max_uses: int) -> dict:
    """Anthropic's server-side web search, as a tool definition."""
    return {"type": WEB_SEARCH_TYPE, "name": "web_search", "max_uses": max_uses}


def is_server_tool(tool: dict) -> bool:
    """Whether the provider executes this tool rather than the caller.

    Keyed on the absence of an input schema rather than on a list of known type
    strings: a server tool is defined by its versioned `type` and needs no schema,
    because the caller never fills one in.
    """
    return "input_schema" not in tool


# Providers that answered with something no retry will fix — an unfunded key, a
# missing or rejected one. `provider="auto"` skips these for the rest of the
# process instead of paying a doomed round trip before every single call.
_DEAD: dict[tuple[str, str], str] = {}
_DEAD_LOCK = threading.Lock()
# 429 and 5xx are the provider being busy, not broken; retrying those is correct.
_PERMANENT = (" 400 ", " 401 ", " 403 ", " 404 ")


def _remember_dead(provider: str, model: str, error: Exception) -> None:
    # Keyed by model as well as provider: a 400 or 404 is as often "this model id is
    # wrong" as "this key is dead", and a mistyped `critic_model` used to condemn
    # Anthropic for the whole process — including the commenter calls that were
    # working moments earlier.
    if any(code in f" {error} " for code in _PERMANENT):
        with _DEAD_LOCK:
            _DEAD.setdefault((provider, model), str(error)[:200])


def dead_providers() -> dict[tuple[str, str], str]:
    """(provider, model) pairs skipped for the rest of this process, and why."""
    with _DEAD_LOCK:
        return dict(_DEAD)


class Refusal(ModelError):
    """The model's safety classifiers declined the request."""


def load_env(path: Path | None = None) -> None:
    """Read KEY=value lines from .env into the environment, without overriding it.

    The working directory first, then the repo when running from a checkout. It used
    to look only at `parents[2]`, which is the repo only before installation — so an
    installed copy silently never loaded a `.env` and looked like it had no keys.
    """
    from .config import source_checkout

    if path is None:
        root = source_checkout()
        candidates = [Path.cwd() / ".env"] + ([root / ".env"] if root else [])
        path = next((c for c in candidates if c.exists()), None)
        if path is None:
            return
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _post(url: str, body: dict, headers: dict, timeout: int = 600) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers={**headers, "content-type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        # Reading the error body can itself fail — a truncated response, bytes that
        # are not UTF-8 — and an exception raised in one handler is not caught by its
        # sibling, so that failure used to escape as something `auto` does not catch.
        try:
            detail = e.read().decode(errors="replace")[:400]
        except Exception:  # noqa: BLE001 — the status is the useful part regardless
            detail = "<error body unreadable>"
        raise ModelError(f"HTTP {e.code} from {url}: {detail}") from None
    except (
        urllib.error.URLError,
        TimeoutError,
        OSError,
        json.JSONDecodeError,
        UnicodeDecodeError,
    ) as e:
        # Everything that is not an HTTP status — DNS, TLS, a read timeout, a body
        # that is not JSON — has to arrive as a ModelError too, because that is the
        # only class `provider="auto"` catches. Escaping as URLError meant a failure
        # to *reach* Anthropic aborted the run instead of falling back to a healthy
        # OpenRouter. Not remembered as dead: these are transient by nature.
        raise ModelError(f"{type(e).__name__} from {url}: {e}") from None


def _anthropic(
    system: str,
    messages: list[dict],
    model: str,
    max_tokens: int,
    effort: str | None = None,
    cache: bool = False,
    usage: "Usage | None" = None,
    tools: list[dict] | None = None,
) -> dict:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise ModelError("ANTHROPIC_API_KEY not set")
    # No `thinking` and no sampling parameters: Fable 5 rejects an explicit
    # thinking config and rejects temperature/top_p/top_k outright, and on Opus 5
    # thinking is adaptive by default — omitting it is what we want either way.
    # Reasoning depth is set through `output_config.effort` instead.
    body: dict = {
        "model": model,
        "max_tokens": max_tokens,
        "system": (
            [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
            if cache
            else system
        ),
        "messages": messages,
    }
    if effort:
        body["output_config"] = {"effort": effort}
    if tools:
        body["tools"] = tools
    payload = _post(ANTHROPIC_URL, body, {"x-api-key": key, "anthropic-version": API_VERSION})
    if usage is not None:
        usage.add(payload.get("usage"))
    if payload.get("stop_reason") == "refusal":
        raise Refusal(f"declined: {(payload.get('stop_details') or {}).get('category')}")
    return {
        "content": payload.get("content") or [],
        "stop_reason": payload.get("stop_reason"),
    }


def reply_text(reply: dict) -> str:
    """The text of a reply, ignoring any tool calls in it."""
    return "".join(b.get("text", "") for b in reply.get("content", []) if b.get("type") == "text")


def tool_calls(reply: dict) -> list[dict]:
    """The tool_use blocks of a reply, in order."""
    return [b for b in reply.get("content", []) if b.get("type") == "tool_use"]


def _flatten(content) -> str:
    """Collapse content blocks to plain text, for providers that take strings."""
    if isinstance(content, str):
        return content
    return "\n\n".join(b.get("text", "") for b in content if b.get("type") == "text")


def _openai_parts(content):
    """Rewrite Anthropic content blocks into the OpenAI shape OpenRouter expects.

    Images are the only block that differs structurally: Anthropic takes base64 in
    an `image` block, OpenAI takes a data URI in `image_url`. Callers build the
    Anthropic shape because `provider="auto"` does not know which route will carry
    the request until it has tried one.
    """
    if isinstance(content, str):
        return content
    parts = []
    for b in content:
        if b.get("type") == "image":
            src = b.get("source") or {}
            parts.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:{src.get('media_type')};base64,{src.get('data')}"
                },
            })
        elif b.get("type") == "text":
            part = {"type": "text", "text": b.get("text", "")}
            # Carry the breakpoint across. Dropping it silently disables caching on
            # this route, which is the route that actually runs here.
            if "cache_control" in b:
                part["cache_control"] = b["cache_control"]
            parts.append(part)
    return parts


def _result_text(content) -> str:
    """A tool result as a string, whatever shape the caller built it in."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict))
    return str(content)


def _openai_tools(tools: list[dict]) -> list[dict]:
    """Anthropic tool definitions in the OpenAI shape, server tools excluded.

    Declaring a server tool here would tell OpenRouter that this process executes
    it. The model would call `web_search`, nothing would answer, and the run would
    stall waiting for a tool result that is never coming — a hang with no error.
    `_openrouter_plugins` carries those across instead.
    """
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("input_schema") or {"type": "object", "properties": {}},
            },
        }
        for t in tools
        if not is_server_tool(t)
    ]


def _openrouter_plugins(tools: list[dict] | None) -> list[dict]:
    """OpenRouter's equivalent of the server tools the caller asked for.

    The two providers do not merely spell this differently, as they do with
    caching and effort — they put the search in different places. Anthropic takes a
    tool definition and hands back `server_tool_use` and `web_search_tool_result`
    blocks; OpenRouter takes a `web` plugin and splices results into the prompt
    before the model ever sees a tool. So there is nothing to translate field by
    field; the request either asks for search or it doesn't.

    `max_uses` has no counterpart and is therefore dropped: OpenRouter's plugin
    caps results per search, not searches per request. The caller says so out loud
    rather than letting a configured cap look like it applies here.
    """
    for t in tools or []:
        if (t.get("type") or "").startswith("web_search"):
            return [{"id": "web"}]
    return []


def _openai_messages(messages: list[dict], passes_blocks: bool) -> list[dict]:
    """Rewrite a tool-using conversation into the shape OpenRouter expects.

    Anthropic keeps a turn's tool calls inside the assistant message's content and
    returns their results as `tool_result` blocks in the next user message. OpenAI
    splits both out: `tool_calls` sit beside the content, and each result is its own
    message with `role: "tool"`. A result must directly follow the call it answers,
    which is why results are emitted before anything else in their message.

    A `tool_result` loses any `cache_control` on the way through, and that is fine —
    measured, not assumed. `reviewer._rolling_cache` puts its moving breakpoint on
    the newest turn, which from turn two on is a tool result, so one of the two
    markers we send does not survive this rewrite. Carrying it across in an A/B
    against the live API changed nothing: identical cache reads, the same share of
    input served from cache, the same handful of fresh tokens. The top-level
    `cache_control` that `_openrouter` sets already advances to the last cacheable
    block, which is what the marker would have asked for. (Run the two conditions in
    either order — whichever goes second starts on a warm cache and looks better by
    a whole prefix if you do not.)

    So: no marker here, and no need for one. The route that does need its markers is
    Anthropic's, which takes them unchanged.
    """
    out: list[dict] = []
    for m in messages:
        content = m["content"]
        if isinstance(content, str):
            out.append({"role": m["role"], "content": content})
            continue
        results = [b for b in content if b.get("type") == "tool_result"]
        for b in results:
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": b.get("tool_use_id"),
                    "content": _result_text(b.get("content")),
                }
            )
        calls = [b for b in content if b.get("type") == "tool_use"]
        rest = [b for b in content if b.get("type") in ("text", "image")]
        if content and not (calls or rest or results):
            # A turn made entirely of blocks this shape has no room for —
            # `server_tool_use` and its result, from a turn the direct route served
            # before falling back here. Dropping it silently removes a turn from the
            # middle of the conversation, which either desynchronises the
            # tool_call/tool_result pairing or rewrites what the model said. Say
            # what happened instead; it is one line in a transcript either way.
            #
            # `results` is part of the test because a user turn of nothing but
            # tool_results is already fully emitted above and must not be followed
            # by a second, invented message.
            out.append({"role": m["role"], "content": "[searched the web]"})
            continue
        if calls:
            out.append(
                {
                    "role": "assistant",
                    "content": _flatten(rest) or None,
                    "tool_calls": [
                        {
                            "id": b.get("id"),
                            "type": "function",
                            "function": {
                                "name": b.get("name"),
                                "arguments": json.dumps(b.get("input") or {}),
                            },
                        }
                        for b in calls
                    ],
                }
            )
        elif rest:
            out.append(
                {
                    "role": m["role"],
                    "content": _openai_parts(rest) if passes_blocks else _flatten(rest),
                }
            )
    return out


def _from_openai(message: dict) -> list[dict]:
    """An OpenRouter reply as Anthropic-shaped content blocks.

    Arguments arrive as a JSON string and can be truncated or malformed. An
    unparseable call becomes a tool_use with empty input rather than an exception,
    so the caller's own validation rejects it with something the model can act on —
    the same road any other bad submission takes.
    """
    blocks: list[dict] = []
    if message.get("content"):
        blocks.append({"type": "text", "text": message["content"]})
    for call in message.get("tool_calls") or []:
        fn = call.get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {}
        blocks.append(
            {
                "type": "tool_use",
                "id": call.get("id"),
                "name": fn.get("name"),
                "input": args if isinstance(args, dict) else {},
            }
        )
    return blocks


def caches_anthropic_prompts(provider: str, model: str) -> bool:
    """Whether Anthropic-shaped `cache_control` survives this route.

    OpenRouter passes the blocks through for Anthropic-backed models, so the
    fallback route caches too — which is what makes caching worth having here at
    all, since the direct key is the one that is usually unavailable. Predicate
    carried over from that earlier client's cache-support check.
    """
    return provider == "anthropic" or model.startswith(("claude", "anthropic/"))


def _openrouter(
    system: str,
    messages: list[dict],
    model: str,
    max_tokens: int,
    effort: str | None = None,
    cache: bool = False,
    usage: "Usage | None" = None,
    tools: list[dict] | None = None,
) -> dict:
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise ModelError("OPENROUTER_API_KEY not set")
    # Prefix only an unqualified name: `anthropic/claude-opus-5` is what OpenRouter
    # wants, but blindly prefixing would relabel every model as Anthropic-backed and
    # send cache_control to a provider that rejects it.
    routed = model if "/" in model else f"anthropic/{model}"
    passes_blocks = caches_anthropic_prompts("openrouter", model)
    body: dict = {
        "model": routed,
        "max_tokens": max_tokens,
        # Content blocks go through untouched for Anthropic-backed models, so the
        # `cache_control` markers the caller placed still mean something. Flattened
        # otherwise, since a non-Anthropic model would reject them. Tool calls and
        # their results are restructured either way — OpenAI's shape for those is
        # different in kind, not just in spelling.
        "messages": [
            {"role": "system", "content": system},
            *_openai_messages(messages, passes_blocks),
        ],
    }
    if tools:
        # Both may be empty: a request carrying only a server tool declares no
        # functions, and one carrying only client tools declares no plugins.
        client_tools = _openai_tools(tools)
        if client_tools:
            body["tools"] = client_tools
        plugins = _openrouter_plugins(tools)
        if plugins:
            body["plugins"] = plugins
    if effort:
        # OpenRouter's own spelling. Not `output_config`, which is Anthropic-native.
        body["reasoning"] = {"effort": effort}
    if cache and passes_blocks:
        # Automatic caching of the last cacheable block, on top of the explicit
        # breakpoints in the message content.
        body["cache_control"] = {"type": "ephemeral"}
    payload = _post(OPENROUTER_URL, body, {"Authorization": f"Bearer {key}"})
    if usage is not None:
        usage.add(payload.get("usage"))
    choices = payload.get("choices") or []
    if not choices:
        raise ModelError(f"no choices in response: {str(payload)[:200]}")
    finish = choices[0].get("finish_reason")
    return {
        "content": _from_openai(choices[0].get("message") or {}),
        "stop_reason": "tool_use" if finish == "tool_calls" else finish,
    }


def exchange(
    system: str,
    messages: list[dict],
    model: str = DEFAULT_MODEL,
    max_tokens: int = 4096,
    provider: str = "auto",
    effort: str | None = None,
    cache: bool = False,
    usage: "Usage | None" = None,
    tools: list[dict] | None = None,
) -> dict:
    """One request against a full message list, returning Anthropic-shaped blocks.

    The primitive underneath everything. `converse` is the text-only view of it, for
    the callers that only ever wanted the words; the commenter needs the blocks
    because it answers with a tool call rather than with prose.

    `cache` marks the system prompt as a cache breakpoint; the caller places any
    further breakpoints on message blocks. It matters because the commenter resends
    the whole document on every turn — uncached, comment five would pay for the
    document five times.
    """
    load_env()
    args = (system, messages, model, max_tokens, effort, cache, usage, tools)
    if provider == "anthropic":
        return _anthropic(*args)
    if provider == "openrouter":
        return _openrouter(*args)
    if provider != "auto":
        raise ValueError(f"unknown provider {provider!r}")
    # Skip a route already known to be permanently unavailable. An unfunded key
    # answers in ~0.25s, which across a run's model calls is seconds of latency and
    # a pile of requests that were never going to succeed.
    if ("anthropic", model) not in dead_providers():
        try:
            return _anthropic(*args)
        except Refusal:
            raise
        except ModelError as e:
            _remember_dead("anthropic", model, e)
    # Direct access unavailable (no key, no credit) — same model, other route.
    return _openrouter(*args)


def converse(
    system: str,
    messages: list[dict],
    model: str = DEFAULT_MODEL,
    max_tokens: int = 4096,
    provider: str = "auto",
    effort: str | None = None,
    cache: bool = False,
    usage: "Usage | None" = None,
) -> str:
    """`exchange`, as text. For callers that ask a question and want an answer."""
    return reply_text(
        exchange(system, messages, model, max_tokens, provider, effort, cache, usage)
    )


def complete(
    system: str,
    user: str,
    model: str = DEFAULT_MODEL,
    max_tokens: int = 4096,
    provider: str = "auto",
    effort: str | None = None,
) -> str:
    """Single-turn convenience wrapper over `converse`."""
    return converse(
        system,
        [{"role": "user", "content": user}],
        model=model,
        max_tokens=max_tokens,
        provider=provider,
        effort=effort,
    )


def extract_json_object(text: str) -> dict:
    """Pull the first JSON object out of a model response.

    Uses a real JSON decoder rather than counting brackets: comment text about code
    frequently contains an unmatched `{` or `}`, and bracket counting rejects the
    whole response when it does. Models also wrap JSON in prose or fences often
    enough that demanding clean output would fail runs for no good reason.
    """
    decoder = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            parsed, _ = decoder.raw_decode(text[i:])
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    raise ValueError(f"no JSON object in model response: {text[:200]!r}")
