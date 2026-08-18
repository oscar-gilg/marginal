"""Minimal Chrome DevTools Protocol client — just enough to drive the Docs UI.

Raw CDP rather than Playwright: this only needs key events, text insertion and
`Runtime.evaluate`, and a websocket keeps a command at ~1ms instead of a
round trip through a higher-level driver.

Key events carry `windowsVirtualKeyCode`. Without it `event.keyCode` arrives as
0 and Google Docs' shortcut handler ignores the key entirely — the silent
failure mode that makes a naive driver look like it works while doing nothing.
"""

from __future__ import annotations

import json
import subprocess
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

import websocket

ALT, CTRL, META, SHIFT = 1, 2, 4, 8

# key name -> (key, code, windowsVirtualKeyCode)
KEYS = {
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


def launch_chrome(
    profile_dir: str, port: int = 9222, url: str | None = None, headless: bool = False
) -> subprocess.Popen:
    """Launch real Chrome with a dedicated profile and the debug port open.

    A dedicated profile keeps automation out of the user's live browsing session,
    and a second profile is how the model posts as its own Google account. Sign in
    once manually in each profile; the session persists in its directory.

    `headless` keeps runs off the desktop, which also stops Cmd-modified keys the
    page doesn't consume from reaching the native menu bar. Sign in **headful**
    first and reuse the same profile directory — Google frequently refuses sign-in
    in a headless browser. Whether clipboard read-back survives headless is
    unverified: it needs a focused document, and focus semantics differ.
    """
    chrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    args = [
        chrome,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    if headless:
        args += ["--headless=new", "--window-size=1400,1000"]
    if url:
        args.append(url)
    return subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def wait_for_port(port: int = 9222, timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1).read()
            return
        except Exception:
            time.sleep(0.2)
    raise TimeoutError(f"no CDP endpoint on port {port}")


def targets(port: int = 9222) -> list[dict]:
    raw = urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=5).read()
    return [t for t in json.loads(raw) if t.get("type") == "page"]


@dataclass
class Page:
    ws: websocket.WebSocket
    target_id: str | None = None
    port: int = 9222
    _id: int = field(default=0)

    @classmethod
    def _connect(cls, target: dict, port: int) -> "Page":
        # suppress_origin: Chrome 111+ rejects a CDP websocket that carries an
        # Origin header unless launched with --remote-allow-origins.
        ws = websocket.create_connection(
            target["webSocketDebuggerUrl"], timeout=30, suppress_origin=True
        )
        return cls(ws=ws, target_id=target.get("id"), port=port)

    @classmethod
    def attach(cls, url_contains: str, port: int = 9222) -> "Page":
        """Attach to an existing tab by URL substring.

        Ambiguous by nature — prefer `open`, which addresses the tab it created by
        id. Raises when several tabs match rather than silently driving whichever
        one Chrome happens to list first.
        """
        matches = [t for t in targets(port) if url_contains in t.get("url", "")]
        if not matches:
            raise LookupError(f"no open tab whose URL contains {url_contains!r}")
        if len(matches) > 1:
            raise LookupError(
                f"{len(matches)} tabs match {url_contains!r}; close the extras or use open()"
            )
        return cls._connect(matches[0], port)

    @classmethod
    def open(cls, url: str, port: int = 9222) -> "Page":
        """Open a new tab at `url` and attach to *that* tab.

        `/json/new` returns the created target, so the id it reports is used
        directly. Matching on the URL instead would attach to whichever
        same-document tab Chrome lists first — a stale tab that may be scrolled
        elsewhere, showing a different document tab, or view-only.
        """
        raw = urllib.request.urlopen(
            urllib.request.Request(
                f"http://127.0.0.1:{port}/json/new?{urllib.parse.quote(url, safe='')}",
                method="PUT",
            ),
            timeout=10,
        ).read()
        created = json.loads(raw)
        target_id = created.get("id")
        if not target_id:
            raise RuntimeError(f"/json/new returned no target id: {raw[:200]!r}")
        for _ in range(50):
            for t in targets(port):
                if t.get("id") == target_id and t.get("webSocketDebuggerUrl"):
                    return cls._connect(t, port)
            time.sleep(0.2)
        raise TimeoutError(f"tab {target_id} for {url} never became attachable")

    def send(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        self.ws.send(json.dumps({"id": self._id, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self._id:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})

    # --- input ---------------------------------------------------------------

    def key(
        self, name: str, modifiers: int = 0, count: int = 1, commands: list[str] | None = None
    ) -> None:
        """Send a key. `commands` carries native editing commands (e.g. ["copy"]).

        A synthetic Cmd+C does not reach Chrome's clipboard machinery — Chrome
        handles that shortcut in the browser process, not as a page key event — so
        clipboard operations must name the command explicitly.
        """
        key, code, vk = KEYS[name]
        down = {
            "type": "keyDown",
            "key": key,
            "code": code,
            "windowsVirtualKeyCode": vk,
            "nativeVirtualKeyCode": vk,
            "modifiers": modifiers,
        }
        if commands:
            down["commands"] = commands
        up = dict(down, type="keyUp")
        for _ in range(count):
            self.send("Input.dispatchKeyEvent", down)
            self.send("Input.dispatchKeyEvent", up)

    def insert_text(self, text: str) -> None:
        """One call for the whole string — never type character by character."""
        self.send("Input.insertText", {"text": text})

    def type_text(self, text: str) -> None:
        """Type text as real keypresses, one keyDown/keyUp pair per character.

        The slow spelling of `insert_text`, kept separate because the two are not
        interchangeable over a selection in Suggesting mode: `Input.insertText`
        inserts without deleting the selected text, so the "replacement" and the
        text it replaced would both end up in the document. A real keypress
        replaces the selection exactly — byte for byte, where Backspace and Delete
        both suggest-delete one extra character (Docs trims the space next to a
        word-shaped selection). Verified against a live document; see
        `docs_ui.suggest_edit`.
        """
        for ch in text:
            is_alnum = ch.isascii() and ch.isalnum()
            code = ""
            if ch.isascii() and ch.isalpha():
                code = f"Key{ch.upper()}"
            elif ch.isdigit():
                code = f"Digit{ch}"
            elif ch == " ":
                code = "Space"
            down = {
                "type": "keyDown",
                "key": ch,
                "code": code,
                "windowsVirtualKeyCode": ord(ch.upper()) if is_alnum else 0,
                "nativeVirtualKeyCode": ord(ch.upper()) if is_alnum else 0,
                "text": ch,
                "unmodifiedText": ch,
            }
            # The keyUp must not carry text: a pair that both carry it inserts the
            # character twice and was observed to wedge the editor's input pipeline.
            up = {k: v for k, v in down.items() if k not in ("text", "unmodifiedText")}
            up["type"] = "keyUp"
            self.send("Input.dispatchKeyEvent", down)
            self.send("Input.dispatchKeyEvent", up)

    def bring_to_front(self) -> None:
        """Clipboard reads require a focused document."""
        self.send("Page.bringToFront")

    def grant_clipboard(self, origin: str = "https://docs.google.com") -> None:
        self.send(
            "Browser.grantPermissions",
            {"origin": origin, "permissions": ["clipboardReadWrite", "clipboardSanitizedWrite"]},
        )

    def read_selection(self, timeout: float = 1.5) -> str | None:
        """Copy the current selection and read it back as plain text.

        This is the only way to inspect the editing selection in Docs: the text is
        painted to a canvas, so there is no DOM range to interrogate. Copying and
        reading the clipboard turns blind keyboard navigation into a closed loop.

        A sentinel is written first, so a copy that silently does nothing reads back
        as the sentinel rather than as stale clipboard contents from another app.
        Returns None if the copy never landed.
        """
        sentinel = "\x00marginal-nothing-copied\x00"
        self.eval(f"navigator.clipboard.writeText({json.dumps(sentinel)}).then(() => true)")
        self.key("c", META, commands=["copy"])
        deadline = time.time() + timeout
        while time.time() < deadline:
            got = self.eval("navigator.clipboard.readText().then(t => t)")
            if got is not None and got != sentinel:
                return got
            time.sleep(0.05)
        return None

    def eval(self, expression: str):
        """Evaluate JS, raising on a thrown exception.

        Returning None on failure would make a failed clipboard write look like a
        successful one, which is how a stale clipboard could validate the wrong
        selection.
        """
        r = self.send(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
        )
        if "exceptionDetails" in r:
            detail = r["exceptionDetails"]
            text = (detail.get("exception") or {}).get("description") or detail.get("text")
            raise RuntimeError(f"JS error: {text}")
        return r.get("result", {}).get("value")

    def wait_until(self, expression: str, timeout: float = 10.0, interval: float = 0.05) -> bool:
        """Poll a JS predicate instead of sleeping a fixed amount."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.eval(expression):
                return True
            time.sleep(interval)
        return False

    def close(self) -> None:
        """Close the websocket *and* the browser tab.

        Closing only the socket leaks a Chrome tab per run; those stale tabs then
        accumulate on the same document, which is what made URL-substring
        attachment dangerous in the first place.
        """
        try:
            self.ws.close()
        except Exception:
            pass
        if not self.target_id:
            return
        try:
            urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}/json/close/{self.target_id}", timeout=5
            ).read()
        except Exception:
            pass
