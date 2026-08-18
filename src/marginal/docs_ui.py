"""Post a native anchored comment by driving the Google Docs UI.

What a probe against the live editor established, and why this module looks like
it does:

* Docs' find (Cmd+F) **does not move the editing selection**. It only paints
  matches. Typing a quote into find and then pressing the comment shortcut
  anchors the comment wherever the caret already was — a silent wrong-anchor,
  which is the worst possible failure. So find is not used here at all.
* A real selection followed by Cmd+Opt+M anchors correctly. Verified by reading
  `quotedFileContent` back from the Drive API: byte-exact.
* Escape does not close the find bar, and closing it drops any selection.

Selection is therefore made by keyboard navigation from a known origin. Two
strategies, so their accuracy and speed can be compared on real documents:

* ``chars``     — step one character at a time. Exact by construction *if* one
                  arrow press equals one character of our text stream.
* ``paragraph`` — jump by paragraph, then step characters within it. Far fewer
                  events; relies on Option+Down landing on paragraph starts.

Neither is trusted: every post is verified against the API afterwards.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from .cdp import ALT, META, SHIFT, Page

# The comment composer, across the selectors Docs has used for it.
_COMPOSER_SEL = (
    ".docos-input-textarea, "
    '[contenteditable="true"][aria-label*="omment"], textarea[aria-label*="omment"]'
)

# Existence is not enough: the sidebar keeps matching elements around after a post,
# so a shortcut that silently failed would look like an open composer — and the
# comment body would then be typed into the document, replacing the selection.
# Require the *focused* element to be the composer.
COMPOSER_FOCUSED_JS = (
    "(() => { const a = document.activeElement; "
    f"return !!a && a.matches({_COMPOSER_SEL!r}); }})()"
)


class SpanError(ValueError):
    """The requested quote does not identify exactly one span."""


class SuggestNotReady(RuntimeError):
    """A suggestion was refused before anything was typed; safe to retry.

    A distinct type because the caller's retry policy hangs on it: everything up
    to the first typed character can be retried freely, and anything after it is
    terminal — retrying a typing failure could apply the same edit twice.
    """


@dataclass
class Span:
    start: int
    end: int
    quote: str


def resolve_quote(text: str, quote: str) -> Span:
    """Locate `quote` in `text`, requiring exactly one case-sensitive occurrence.

    Model-proposed quotes are almost always unique, so ambiguity is an
    edge case worth failing loudly on rather than guessing at.
    """
    quote = quote.strip()
    if not quote:
        raise SpanError("empty quote")
    n = text.count(quote)
    if n == 0:
        raise SpanError(f"quote not found verbatim: {quote[:60]!r}")
    if n > 1:
        raise SpanError(f"quote occurs {n} times, cannot disambiguate: {quote[:60]!r}")
    start = text.index(quote)
    return Span(start, start + len(quote), quote)


def paragraph_of(paragraphs: list[dict], offset: int) -> tuple[int, int]:
    """(index, start_offset) of the paragraph containing `offset`."""
    if not paragraphs:
        return 0, 0
    for i, p in enumerate(paragraphs):
        if p["start"] <= offset < p["end"]:
            return i, p["start"]
    return len(paragraphs) - 1, paragraphs[-1]["start"]


def goto_document_start(page: Page) -> None:
    """Select-all then collapse left: a deterministic origin without relying on
    a 'move to document start' shortcut, which differs across platforms."""
    page.key("a", META)
    page.key("ArrowLeft")


def _navigate_to(page: Page, offset: int, paragraphs: list[dict], strategy: str) -> int:
    """Put the caret at `offset`, counting key events sent."""
    goto_document_start(page)
    events = 2
    if strategy == "chars":
        page.key("ArrowRight", count=offset)
        return events + offset
    if strategy == "paragraph":
        idx, para_start = paragraph_of(paragraphs, offset)
        if idx:
            page.key("ArrowDown", ALT, count=idx)
            events += idx
        within = offset - para_start
        if within:
            page.key("ArrowRight", count=within)
            events += within
        return events
    raise ValueError(f"unknown selection strategy {strategy!r}")


def select_span(
    page: Page,
    span: Span,
    paragraphs: list[dict],
    strategy: str = "paragraph",
    text: str | None = None,
    max_rounds: int = 4,
) -> tuple[bool, int, str | None]:
    """Select `span`, then correct the selection until it matches exactly.

    Blind keyboard navigation drifts: smart chips and inline objects occupy caret
    positions while contributing no characters, and a paragraph jump was observed
    to lose one character off the end of the selection. Rather than model every
    such quirk, the selection is copied, read back, and adjusted.

    Returns (exact, key_events, selection_actually_made).
    """
    length = span.end - span.start
    events = _navigate_to(page, span.start, paragraphs, strategy)
    page.key("ArrowRight", SHIFT, count=length)
    events += length

    got = None
    for _ in range(max_rounds):
        got = (page.read_selection() or "").replace("\r\n", "\n").strip("\n")
        if got == span.quote:
            return True, events, got

        # Right length, wrong place: locate what we actually grabbed and shift by
        # the difference. Only safe when the grabbed text is itself unambiguous.
        if text and got and text.count(got) == 1:
            delta = span.start - text.index(got)
            if delta:
                page.key("ArrowLeft")  # collapse to the selection's left edge
                page.key("ArrowRight" if delta > 0 else "ArrowLeft", count=abs(delta))
                page.key("ArrowRight", SHIFT, count=length)
                events += 1 + abs(delta) + length
                continue

        # Same start, wrong length: extend or retract the tail.
        if got and span.quote.startswith(got):
            short = len(span.quote) - len(got)
            page.key("ArrowRight", SHIFT, count=short)
            events += short
            continue
        if got and got.startswith(span.quote):
            over = len(got) - len(span.quote)
            page.key("ArrowLeft", SHIFT, count=over)
            events += over
            continue
        break

    return False, events, got


# The editor's mode switcher. The *class* carries the current mode
# (`edit-mode` / `suggest-mode`), which unlike the label text does not depend on
# the UI language; the aria-label is read alongside as a cross-check. Empty text
# and a `-disabled` class right after load mean "not ready yet", not "unknown".
_MODE_JS = """(() => {
  const el = document.querySelector('#docs-toolbar-mode-switcher');
  if (!el) return null;
  return {cls: el.className, aria: el.getAttribute('aria-label') || '',
          text: el.textContent.trim()};
})()"""

# Probed against the live editor: the Cmd+Opt+Shift+Z chord does not switch
# modes, so the switcher's dropdown is driven directly. The menu item is found by
# its text, which *is* language-dependent — an English UI is assumed, as the
# probe-and-refuse below turns a miss into a refusal rather than a wrong mode.
_PICK_MODE_JS = """((pattern) => {
  const btn = document.querySelector('#docs-toolbar-mode-switcher');
  if (!btn) return 'no mode switcher';
  for (const type of ['mousedown', 'mouseup', 'click'])
    btn.dispatchEvent(new MouseEvent(type, {bubbles: true}));
  const item = [...document.querySelectorAll('.goog-menuitem')]
    .find(m => m.offsetParent !== null && new RegExp(pattern, 'i').test(m.textContent));
  if (!item) return 'no matching menu item';
  for (const type of ['mousedown', 'mouseup', 'click'])
    item.dispatchEvent(new MouseEvent(type, {bubbles: true}));
  return 'ok';
})"""

_MODE_CLASSES = {"edit-mode": "editing", "suggest-mode": "suggesting", "view-mode": "viewing"}
_MODE_PATTERNS = {"editing": "^Editing", "suggesting": "^Suggesting", "viewing": "^Viewing"}


def editor_mode(page: Page, timeout: float = 5.0) -> str:
    """The editor's current mode: 'editing', 'suggesting' or 'viewing'.

    Raises rather than guessing when the switcher is missing or unreadable — an
    unknown mode must refuse exactly as an ambiguous anchor does, because the
    caller is deciding whether a keystroke becomes a tracked change or a real
    edit.
    """
    deadline = time.time() + timeout
    probe = None
    while time.time() < deadline:
        probe = page.eval(_MODE_JS)
        if probe:
            for marker, mode in _MODE_CLASSES.items():
                if marker in probe["cls"].split():
                    return mode
        time.sleep(0.1)
    raise RuntimeError(f"editor mode could not be read (switcher: {probe!r})")


def set_mode(page: Page, mode: str, timeout: float = 5.0) -> str:
    """Switch the editor to `mode`, confirm it took, and return the previous mode.

    Confirm-or-raise: nothing downstream may type until the probe reads the mode
    it asked for, because a keystroke in the wrong mode either edits the document
    for real (wanted suggesting, got editing) or silently does nothing.
    """
    if mode not in _MODE_PATTERNS:
        raise ValueError(f"unknown editor mode {mode!r}")
    before = editor_mode(page, timeout=timeout)
    if before == mode:
        return before
    outcome = page.eval(f"{_PICK_MODE_JS}({_MODE_PATTERNS[mode]!r})")
    if outcome != "ok":
        raise RuntimeError(f"could not reach the mode menu: {outcome}")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if editor_mode(page, timeout=timeout) == mode:
            return before
        time.sleep(0.1)
    raise RuntimeError(f"asked the editor for {mode} mode and it stayed in {editor_mode(page)}")


def suggest_edit(
    page: Page,
    span: Span,
    replacement: str,
    paragraphs: list[dict],
    strategy: str = "paragraph",
    text: str | None = None,
) -> int:
    """Type `replacement` over `span` as a suggested edit. Returns key events sent.

    Assumes `set_mode(page, "suggesting")` has already succeeded; the mode is
    probed again here, immediately before typing, so a mode that changed under us
    refuses rather than editing the document for real.

    The replacement is typed as real keypresses (`Page.type_text`), not
    `Input.insertText`: probed live, insertText over a selection in Suggesting
    mode inserts *without* deleting the selection, and Backspace/Delete both
    suggest-delete one extra character next to a word-shaped selection. A typed
    character replaces the selection byte-exactly. Pure deletions are therefore
    expressed by the caller as a wider replacement that omits the text, never as
    an empty replacement.

    Everything before the first typed character is safe to retry; the typing is
    the terminal act, exactly as submission is for a comment.
    """
    if not replacement:
        raise ValueError("a suggestion needs a non-empty replacement")
    exact, events, got = select_span(page, span, paragraphs, strategy, text=text)
    if not exact:
        raise SuggestNotReady(f"selection not confirmed (got {(got or '')[:60]!r})")
    if (current := editor_mode(page)) != "suggesting":
        raise SuggestNotReady(f"editor is in {current} mode, not suggesting; nothing typed")
    page.type_text(replacement)
    return events + len(replacement)


def post_comment(page: Page, body: str, timeout: float = 5.0, settle: float = 0.35) -> None:
    """Open the comment composer on the current selection, fill it, submit.

    Assumes a live selection — call `select_span` first. Raises if the composer
    does not take focus, so the body is never typed into the document, which would
    replace the selected text rather than comment on it.

    Submission is *not* confirmed here. The sidebar keeps composer-shaped elements
    around after posting, so a DOM check reports failure on comments that actually
    posted. The API verification pass is the only trustworthy confirmation, so this
    just lets the mutation flush.
    """
    page.key("m", META | ALT)
    if not page.wait_until(COMPOSER_FOCUSED_JS, timeout=timeout):
        raise RuntimeError("comment composer did not open and take focus")
    page.insert_text(body)
    page.key("Enter", META)
    time.sleep(settle)


def open_doc(doc_id: str, tab_id: str | None = None, port: int = 9222) -> Page:
    url = f"https://docs.google.com/document/d/{doc_id}/edit"
    if tab_id:
        url += f"?tab={tab_id}"
    page = Page.open(url, port=port)
    # The canvas editor is ready once the document body has been laid out.
    if not page.wait_until("!!document.querySelector('.kix-appview-editor')", timeout=30):
        raise RuntimeError("Docs editor did not finish loading (is this profile signed in?)")
    page.grant_clipboard()
    page.bring_to_front()  # clipboard reads need a focused document
    time.sleep(0.5)  # let the first paint settle before sending input
    return page
