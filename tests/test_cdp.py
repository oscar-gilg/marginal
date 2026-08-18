"""The CDP client: the layer that actually drives the editor, and had no tests.

Everything here runs against a fake websocket. That is enough, because the failures
this module has are about the *shape* of what it sends and what it concludes from
what comes back — not about Chrome. Each is silent in the way this repository cares
about: a key that does nothing, a selection read that returns another application's
clipboard, a wait that times out and is read as success.

That last one is not hypothetical. `wait_until` returns False rather than raising,
and a caller that ignored the return treated a page which never loaded as a page
that had — which shipped, and was caught in review rather than by a test.
"""

from __future__ import annotations

import json

import pytest

from marginal import cdp


class FakeSocket:
    """A CDP endpoint that records what it was sent and replies in order.

    `replies` maps a method name to the `result` it should return, or to a callable
    taking the params. Anything unnamed gets an empty result, which is what most of
    CDP's input commands actually return.
    """

    def __init__(self, replies: dict | None = None):
        self.sent: list[dict] = []
        self.replies = replies or {}
        self._queue: list[str] = []
        self.closed = False

    def send(self, raw: str) -> None:
        msg = json.loads(raw)
        self.sent.append(msg)
        reply = self.replies.get(msg["method"], {})
        if callable(reply):
            reply = reply(msg.get("params") or {})
        self._queue.append(json.dumps({"id": msg["id"], "result": reply}))

    def recv(self) -> str:
        return self._queue.pop(0)

    def close(self) -> None:
        self.closed = True

    # Convenience for assertions.
    def methods(self) -> list[str]:
        return [m["method"] for m in self.sent]

    def params_for(self, method: str) -> list[dict]:
        return [m.get("params") or {} for m in self.sent if m["method"] == method]


def _page(replies: dict | None = None) -> tuple[cdp.Page, FakeSocket]:
    ws = FakeSocket(replies)
    return cdp.Page(ws=ws, target_id=None), ws


# --- key events --------------------------------------------------------------


def test_every_key_carries_a_virtual_key_code():
    # Without it `event.keyCode` arrives as 0 and Docs' shortcut handler ignores the
    # key. Nothing raises: the driver looks like it works and does nothing, which is
    # the failure this whole module's docstring is about.
    page, ws = _page()
    for name in cdp.KEYS:
        page.key(name)
    for params in ws.params_for("Input.dispatchKeyEvent"):
        assert params["windowsVirtualKeyCode"], params
        assert params["nativeVirtualKeyCode"] == params["windowsVirtualKeyCode"]


def test_the_key_codes_are_the_ones_the_platform_defines():
    # Written out rather than derived from the table under test, which would only
    # prove the table is self-consistent. A wrong virtual key code is undetectable
    # at runtime: Docs receives a key it does not recognise and does nothing.
    assert cdp.KEYS == {
        "ArrowRight": ("ArrowRight", "ArrowRight", 39),
        "ArrowLeft": ("ArrowLeft", "ArrowLeft", 37),
        "ArrowUp": ("ArrowUp", "ArrowUp", 38),
        "ArrowDown": ("ArrowDown", "ArrowDown", 40),
        "Enter": ("Enter", "Enter", 13),
        "Escape": ("Escape", "Escape", 27),
        "a": ("a", "KeyA", 65),
        "c": ("c", "KeyC", 67),
        "m": ("m", "KeyM", 77),
        "f": ("f", "KeyF", 70),
        "z": ("z", "KeyZ", 90),
        "Backspace": ("Backspace", "Backspace", 8),
    }


def test_the_modifier_bits_are_the_ones_the_protocol_defines():
    # Cmd+Opt+M is the comment shortcut; a wrong bit sends a different chord and the
    # editor does something else, or nothing, with no error either way.
    assert (cdp.ALT, cdp.CTRL, cdp.META, cdp.SHIFT) == (1, 2, 4, 8)


def test_a_key_sends_a_down_and_an_up_in_that_order():
    # A keyDown with no keyUp leaves a modifier stuck down in the editor, and the
    # next navigation key then does something else entirely.
    page, ws = _page()
    page.key("ArrowRight")
    kinds = [p["type"] for p in ws.params_for("Input.dispatchKeyEvent")]
    assert kinds == ["keyDown", "keyUp"]


def test_a_repeated_key_sends_one_pair_per_press():
    page, ws = _page()
    page.key("ArrowRight", count=5)
    kinds = [p["type"] for p in ws.params_for("Input.dispatchKeyEvent")]
    assert kinds == ["keyDown", "keyUp"] * 5


def test_modifiers_reach_both_halves_of_the_press():
    page, ws = _page()
    page.key("m", modifiers=cdp.META | cdp.ALT)
    for params in ws.params_for("Input.dispatchKeyEvent"):
        assert params["modifiers"] == cdp.META | cdp.ALT


def test_typed_text_is_one_real_keypress_per_character():
    # `type_text` exists because `Input.insertText` over a selection in Suggesting
    # mode inserts without deleting the selection. It must therefore be keypresses
    # all the way down — an implementation that fell back to insertText would look
    # identical on a collapsed caret and corrupt every replacement.
    page, ws = _page()
    page.type_text("ab c")
    assert "Input.insertText" not in ws.methods()
    kinds = [p["type"] for p in ws.params_for("Input.dispatchKeyEvent")]
    assert kinds == ["keyDown", "keyUp"] * 4


def test_the_typed_character_rides_the_key_down_and_not_the_key_up():
    # A pair that both carry the text inserts the character twice — and was observed
    # live to wedge the editor's input pipeline, taking the whole session with it.
    page, ws = _page()
    page.type_text("x")
    down, up = ws.params_for("Input.dispatchKeyEvent")
    assert down["text"] == "x" and down["unmodifiedText"] == "x"
    assert "text" not in up and "unmodifiedText" not in up


def test_typed_punctuation_is_sent_even_without_a_key_code():
    # Replacements are prose: commas, quotes and dashes must reach the editor, not
    # be refused for lacking a KEYS entry the way `key()` refuses unknown names.
    page, ws = _page()
    page.type_text("–, ‘y’")
    downs = [p for p in ws.params_for("Input.dispatchKeyEvent") if p["type"] == "keyDown"]
    assert [p["text"] for p in downs] == ["–", ",", " ", "‘", "y", "’"]


def test_a_native_command_reaches_the_key_down():
    # `commands` is how a copy reaches Chrome's clipboard machinery at all: a
    # synthetic Cmd+C is handled in the browser process, not as a page key event.
    #
    # Only the keyDown is required. `up = dict(down, type="keyUp")` currently copies
    # the command onto the release too, and whether Chrome then runs it twice is a
    # question for a browser this suite deliberately does not have. So that is left
    # unasserted rather than pinned: requiring it would make the safer shape — one
    # command per press — fail this test, and a suspected bug should not become a
    # contract by accident.
    page, ws = _page()
    page.key("c", cdp.META, commands=["copy"])
    down = ws.params_for("Input.dispatchKeyEvent")[0]
    assert down["type"] == "keyDown"
    assert down["commands"] == ["copy"]


def test_an_unknown_key_is_refused_rather_than_sent_blank():
    page, _ = _page()
    with pytest.raises(KeyError):
        page.key("F13")


# --- send / eval ---------------------------------------------------------------


def test_a_protocol_error_is_raised_rather_than_returned_as_a_result():
    ws = FakeSocket()
    page = cdp.Page(ws=ws, target_id=None)

    def failing(raw):
        msg = json.loads(raw)
        ws.sent.append(msg)
        ws._queue.append(json.dumps({"id": msg["id"], "error": {"message": "no such target"}}))

    ws.send = failing
    with pytest.raises(RuntimeError, match="no such target"):
        page.send("Runtime.evaluate", {})


def test_a_reply_to_an_earlier_command_is_skipped_rather_than_returned():
    # CDP interleaves events and replies on one socket. Returning the first message
    # that arrives would answer a command with an unrelated result — and the results
    # here decide whether a selection is correct.
    ws = FakeSocket()
    page = cdp.Page(ws=ws, target_id=None)

    def noisy(raw):
        msg = json.loads(raw)
        ws.sent.append(msg)
        ws._queue.append(json.dumps({"method": "Page.frameNavigated", "params": {}}))
        ws._queue.append(json.dumps({"id": msg["id"] - 1, "result": {"stale": True}}))
        ws._queue.append(json.dumps({"id": msg["id"], "result": {"value": "mine"}}))

    ws.send = noisy
    assert page.send("Runtime.evaluate", {}) == {"value": "mine"}


def test_a_thrown_javascript_error_raises_instead_of_reading_as_null():
    # Returning None on a thrown exception would make a failed clipboard write look
    # like a successful one, and then a stale clipboard validates the wrong span.
    page, _ = _page({
        "Runtime.evaluate": {
            "exceptionDetails": {"exception": {"description": "TypeError: nope"}},
            "result": {"value": None},
        }
    })
    with pytest.raises(RuntimeError, match="TypeError: nope"):
        page.eval("boom()")


def test_eval_awaits_promises_and_returns_by_value():
    # `read_selection` evaluates promise-returning clipboard calls; without both
    # flags the result is a handle to an unresolved promise rather than the text.
    page, ws = _page({"Runtime.evaluate": {"result": {"value": "ok"}}})
    assert page.eval("Promise.resolve('ok')") == "ok"
    params = ws.params_for("Runtime.evaluate")[0]
    assert params["awaitPromise"] is True and params["returnByValue"] is True


# --- wait_until ----------------------------------------------------------------


def test_wait_until_returns_false_on_timeout_rather_than_raising():
    # The contract a caller has to check. Ignoring this return is how a page that
    # never loaded was read as a page that had.
    page, _ = _page({"Runtime.evaluate": {"result": {"value": False}}})
    assert page.wait_until("never()", timeout=0.05, interval=0.01) is False


def test_wait_until_returns_true_as_soon_as_the_predicate_holds():
    seen = {"n": 0}

    def flips(_params):
        seen["n"] += 1
        return {"result": {"value": seen["n"] >= 3}}

    page, ws = _page({"Runtime.evaluate": flips})
    assert page.wait_until("eventually()", timeout=2, interval=0.001) is True
    # Stops on success rather than polling out the whole timeout.
    assert len(ws.params_for("Runtime.evaluate")) == 3


# --- read_selection ------------------------------------------------------------


def _clipboard(sequence):
    """Replies for a clipboard round trip: each read returns the next value."""
    values = list(sequence)

    def evaluate(params):
        if "writeText" in params["expression"]:
            return {"result": {"value": True}}
        return {"result": {"value": values.pop(0) if values else None}}

    return {"Runtime.evaluate": evaluate}


def test_a_selection_is_read_back_once_the_copy_lands():
    page, ws = _page(_clipboard(["the selected words"]))
    assert page.read_selection() == "the selected words"
    # The copy has to be a native command, or it never reaches the clipboard.
    down = ws.params_for("Input.dispatchKeyEvent")[0]
    assert down["commands"] == ["copy"]


def test_a_copy_that_never_lands_reads_as_none_not_as_stale_clipboard():
    # The sentinel is the whole point: without it a copy that silently did nothing
    # returns whatever another application last put on the clipboard, and that text
    # then validates a selection that was never made.
    page, _ = _page(_clipboard([]))  # every read returns the sentinel it wrote
    assert page.read_selection(timeout=0.05) is None


def test_the_sentinel_is_written_before_the_copy_is_asked_for():
    page, ws = _page(_clipboard(["text"]))
    page.read_selection()
    order = [m["method"] for m in ws.sent]
    first_eval = order.index("Runtime.evaluate")
    first_key = order.index("Input.dispatchKeyEvent")
    assert first_eval < first_key, order
    assert "writeText" in ws.params_for("Runtime.evaluate")[0]["expression"]


def test_the_sentinel_itself_is_never_returned_as_a_selection():
    # Reading the sentinel back means the copy did nothing; returning it would hand
    # a caller a span of text that exists nowhere in the document, which would then
    # be compared against the span we meant to select.
    sentinel = "\x00marginal-nothing-copied\x00"

    def evaluate(params):
        if "writeText" in params["expression"]:
            # The sentinel is written as a JSON literal, so it must survive the trip.
            assert json.dumps(sentinel) in params["expression"], params["expression"]
            return {"result": {"value": True}}
        return {"result": {"value": sentinel}}  # a dead copy echoes it straight back

    page, _ = _page({"Runtime.evaluate": evaluate})
    assert page.read_selection(timeout=0.05) is None


# --- lifecycle -----------------------------------------------------------------


def test_closing_closes_the_tab_as_well_as_the_socket(monkeypatch):
    # Closing only the socket leaks a Chrome tab per run, and those stale tabs are
    # what made URL-substring attachment dangerous in the first place.
    asked = []
    monkeypatch.setattr(
        cdp.urllib.request, "urlopen", lambda url, timeout=5: asked.append(url) or _Readable()
    )
    ws = FakeSocket()
    cdp.Page(ws=ws, target_id="T1", port=9333).close()
    assert ws.closed
    assert asked and "/json/close/T1" in asked[0] and "9333" in asked[0]


def test_closing_a_page_with_no_target_id_does_not_call_the_endpoint(monkeypatch):
    monkeypatch.setattr(
        cdp.urllib.request,
        "urlopen",
        lambda *a, **k: pytest.fail("closed a tab without knowing which one"),
    )
    ws = FakeSocket()
    cdp.Page(ws=ws, target_id=None).close()
    assert ws.closed


def test_a_failure_closing_the_tab_does_not_propagate(monkeypatch):
    # `close` runs in `finally` blocks; raising here would replace the real error
    # with a cleanup one.
    def boom(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr(cdp.urllib.request, "urlopen", boom)
    ws = FakeSocket()
    ws.close = boom
    cdp.Page(ws=ws, target_id="T1").close()  # must not raise


class _Readable:
    def read(self):
        return b"{}"


# --- attach --------------------------------------------------------------------


def test_attaching_to_an_ambiguous_url_is_refused_rather_than_guessed(monkeypatch):
    # Two tabs on one document is normal after a crash. Driving whichever Chrome
    # lists first may drive a stale tab, scrolled elsewhere or showing another
    # document tab — a wrong selection with no error.
    monkeypatch.setattr(
        cdp,
        "targets",
        lambda port=9222: [
            {"id": "a", "url": "https://docs.google.com/document/d/X/edit"},
            {"id": "b", "url": "https://docs.google.com/document/d/X/edit"},
        ],
    )
    with pytest.raises(LookupError, match="2 tabs match"):
        cdp.Page.attach("/document/d/X/")


def test_attaching_to_a_url_no_tab_has_is_refused(monkeypatch):
    monkeypatch.setattr(cdp, "targets", lambda port=9222: [])
    with pytest.raises(LookupError, match="no open tab"):
        cdp.Page.attach("/document/d/X/")


# --- open ----------------------------------------------------------------------


def test_open_attaches_to_the_tab_it_created_and_not_a_matching_one(monkeypatch):
    """The guarantee the whole module's tab handling rests on.

    Matching on the URL instead would attach to whichever same-document tab Chrome
    lists first — a stale one, possibly scrolled elsewhere, showing a different
    document tab, or view-only. Every selection then happens in the wrong place,
    and nothing reports an error. A regression to URL matching passes every other
    test in this file, which is why this one exists.
    """
    url = "https://docs.google.com/document/d/X/edit"
    listing = [
        {"id": "stale", "url": url, "webSocketDebuggerUrl": "ws://stale", "type": "page"},
        {"id": "fresh", "url": url, "webSocketDebuggerUrl": "ws://fresh", "type": "page"},
    ]
    requested = []

    def fake_urlopen(req, timeout=10):
        requested.append(getattr(req, "full_url", req))
        assert getattr(req, "method", None) == "PUT", "the tab must be created, not found"
        return _Body(b'{"id": "fresh"}')

    connected = {}
    monkeypatch.setattr(cdp.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(cdp, "targets", lambda port=9222: listing)
    monkeypatch.setattr(
        cdp.websocket,
        "create_connection",
        lambda ws_url, timeout=30, suppress_origin=True: connected.setdefault("url", ws_url)
        or FakeSocket(),
    )
    page = cdp.Page.open(url, port=9222)
    assert page.target_id == "fresh"
    assert connected["url"] == "ws://fresh", "attached to the wrong tab"
    assert "/json/new" in requested[0]


def test_open_refuses_a_created_tab_with_no_id(monkeypatch):
    # Without an id there is nothing to address, and falling back to URL matching
    # here is exactly the behaviour the id exists to avoid.
    monkeypatch.setattr(
        cdp.urllib.request, "urlopen", lambda req, timeout=10: _Body(b"{}")
    )
    with pytest.raises(RuntimeError, match="no target id"):
        cdp.Page.open("https://example.com")


class _Body:
    def __init__(self, raw):
        self._raw = raw

    def read(self):
        return self._raw
