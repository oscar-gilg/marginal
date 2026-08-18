"""The suggesting-mode gate in `docs_ui`: probe, switch, refuse.

Typing a replacement while the editor is in Editing mode is a real edit — the
worst failure this feature can have. Every test here is about the gate that
prevents it: an unreadable mode refuses, a switch that did not take refuses, and
a refusal must have typed *nothing*.

The mode probe reads the switcher's *class* (`edit-mode` / `suggest-mode`),
established against the live editor: the label text follows the UI language, the
class does not.
"""

import pytest

from marginal import docs_ui
from marginal.docs_ui import Span, editor_mode, set_mode, suggest_edit


class FakePage:
    """Scripted answers to the two mode evals; records everything typed.

    `probes` is a list of switcher dicts (or None) returned in order by the mode
    probe, the last one repeating forever. `pick` is the mode-menu click result.
    """

    def __init__(self, probes, pick="ok"):
        self.probes = list(probes)
        self.pick = pick
        self.typed = []
        self.picked = 0

    def eval(self, expr):
        if "MouseEvent" in expr:
            self.picked += 1
            return self.pick
        probe = self.probes[0] if len(self.probes) == 1 else self.probes.pop(0)
        return probe

    def type_text(self, text):
        self.typed.append(text)


def switcher(cls, text="whatever"):
    return {"cls": f"docs-toolbar-menu-button docs-mode-switcher {cls}", "aria": "", "text": text}


# --- editor_mode -------------------------------------------------------------


def test_the_mode_is_read_from_the_class_not_the_label():
    # A German UI says "Bearbeitung", not "Editing"; the class is the same.
    page = FakePage([switcher("edit-mode", text="Bearbeitung")])
    assert editor_mode(page) == "editing"
    assert editor_mode(FakePage([switcher("suggest-mode", text="Vorschlagen")])) == "suggesting"


def test_a_missing_switcher_refuses_rather_than_guessing():
    with pytest.raises(RuntimeError, match="could not be read"):
        editor_mode(FakePage([None]), timeout=0.2)


def test_an_unrecognised_class_refuses_rather_than_guessing():
    # A Docs UI change that renames the classes must fail loudly, not read as any
    # particular mode.
    page = FakePage([switcher("some-new-mode")])
    with pytest.raises(RuntimeError, match="could not be read"):
        editor_mode(page, timeout=0.2)


def test_a_switcher_still_loading_is_waited_for():
    page = FakePage([None, switcher("edit-mode")])
    assert editor_mode(page) == "editing"


# --- set_mode ----------------------------------------------------------------


def test_set_mode_confirms_the_switch_took_and_reports_the_previous_mode():
    page = FakePage([switcher("edit-mode"), switcher("suggest-mode")])
    assert set_mode(page, "suggesting") == "editing"
    assert page.picked == 1


def test_set_mode_does_not_click_when_already_there():
    page = FakePage([switcher("suggest-mode")])
    assert set_mode(page, "suggesting") == "suggesting"
    assert page.picked == 0


def test_a_switch_that_did_not_take_raises():
    page = FakePage([switcher("edit-mode")])  # probe never changes
    with pytest.raises(RuntimeError, match="stayed in"):
        set_mode(page, "suggesting", timeout=0.3)


def test_a_missing_menu_item_raises():
    page = FakePage([switcher("edit-mode")], pick="no matching menu item")
    with pytest.raises(RuntimeError, match="mode menu"):
        set_mode(page, "suggesting", timeout=0.3)


# --- suggest_edit: nothing may be typed unless everything checked out --------


def _selects(monkeypatch, exact=True):
    monkeypatch.setattr(docs_ui, "select_span", lambda *a, **k: (exact, 7, "quote text"))


def test_a_wrong_mode_at_the_last_probe_types_nothing(monkeypatch):
    # The mode is probed immediately before typing, after the selection work: a
    # mode that changed under us must refuse, not edit the document for real.
    _selects(monkeypatch)
    page = FakePage([switcher("edit-mode")])
    with pytest.raises(RuntimeError, match="editing mode, not suggesting"):
        suggest_edit(page, Span(0, 10, "quote text"), "better text", [])
    assert page.typed == []


def test_an_unconfirmed_selection_types_nothing(monkeypatch):
    _selects(monkeypatch, exact=False)
    page = FakePage([switcher("suggest-mode")])
    with pytest.raises(RuntimeError, match="selection not confirmed"):
        suggest_edit(page, Span(0, 10, "quote text"), "better text", [])
    assert page.typed == []


def test_an_empty_replacement_is_refused(monkeypatch):
    # An empty replacement would mean Backspace, and Backspace suggest-deletes one
    # extra character next to a word-shaped selection. Deletions are expressed as
    # a wider replacement instead.
    _selects(monkeypatch)
    page = FakePage([switcher("suggest-mode")])
    with pytest.raises(ValueError, match="non-empty replacement"):
        suggest_edit(page, Span(0, 10, "quote text"), "", [])
    assert page.typed == []


def test_a_confirmed_selection_in_suggesting_mode_types_the_replacement(monkeypatch):
    _selects(monkeypatch)
    page = FakePage([switcher("suggest-mode")])
    events = suggest_edit(page, Span(0, 10, "quote text"), "better text", [])
    assert page.typed == ["better text"]
    assert events == 7 + len("better text")
