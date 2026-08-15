"""Preparing the Markdown export: figure payloads, and slicing it per tab."""

from marginal import md

B64 = "[image1]: <data:image/png;base64,iVBORw0KGgoAAAANSUhEUg" + "A" * 400 + ">"


def test_a_figure_payload_is_replaced_by_a_placeholder():
    # An inlined payload dwarfs the prose around it: a small logo can be most of an
    # export. Eight figures would spend tens of thousands of tokens on unreadable bytes.
    src = f"Throughput rose.\n\n![][image1]\n\nAnd fell.\n\n{B64}\n"
    out = md.sanitise(src)
    assert "data:image" not in out and "base64" not in out
    assert "[figure 1]" in out
    assert "Throughput rose." in out and "And fell." in out
    assert len(out) < len(src) / 3


def test_alt_text_is_kept_because_it_describes_the_figure():
    out = md.sanitise("![Figure 2: accuracy over time][image1]\n\n" + B64)
    assert "[figure 1: Figure 2: accuracy over time]" in out


def test_inline_image_syntax_is_handled_too():
    out = md.sanitise("see ![chart](https://example.com/a.png) here")
    assert out == "see [figure 1: chart] here"


def test_figures_are_numbered_in_order():
    out = md.sanitise("![][image1] then ![][image2] then ![][image3]")
    assert "[figure 1]" in out and "[figure 2]" in out and "[figure 3]" in out


def test_a_document_without_figures_is_untouched():
    src = "# Results\n\nProse with an * asterisk and a [link](http://x).\n"
    assert md.sanitise(src) == src.strip()


def test_an_orphan_payload_is_still_stripped():
    # A definition whose reference was deleted still costs the same tokens.
    assert "data:image" not in md.sanitise(f"Prose.\n\n{B64}\n")


TAB_A = ("Adaptive difficulty in introductory programming courses at scale\n"
         "We ran the system across two sections during the autumn term.\n")
TAB_B = ("Appendix: instrumentation and the logging pipeline we deployed\n"
         "Events were written to a queue and batched every thirty seconds.\n")
EXPORT = ("# Adaptive difficulty in introductory programming courses at scale\n\n"
          "We ran the system across two sections during the autumn term.\n\n"
          "# Appendix: instrumentation and the logging pipeline we deployed\n\n"
          "Events were written to a queue and batched every thirty seconds.\n")


def tab(title, text):
    return {"title": title, "text": text}


TABS = [tab("Tab 1", TAB_A), tab("Tab 2", TAB_B)]


def test_a_single_tab_takes_the_whole_export():
    out = md.for_tab(EXPORT, [tab("Tab 1", TAB_A + TAB_B)], 0)
    assert out and "Appendix" in out


def test_each_tab_gets_only_its_own_share():
    first = md.for_tab(EXPORT, TABS, 0)
    second = md.for_tab(EXPORT, TABS, 1)
    assert first and "two sections during the autumn term" in first
    assert "Events were written to a queue" not in first, "leaked the next tab"
    assert second and "Events were written to a queue" in second
    assert "two sections during the autumn term" not in second


def test_a_tab_missing_from_the_export_is_refused():
    # Better to read the plain text than to anchor into the wrong tab.
    absent = "Nothing in this tab appears in the export at all, not one line of it.\n"
    assert md.for_tab(EXPORT, [tab("Tab 1", TAB_A), tab("Nope", absent)], 1) is None


def test_an_export_that_does_not_match_the_tab_is_refused():
    assert md.for_tab("# Something else entirely, unrelated to the document\n",
                      [tab("Tab 1", TAB_A)], 0) is None


def test_empty_inputs_are_refused_rather_than_guessed():
    assert md.for_tab("", [tab("Tab 1", TAB_A)], 0) is None
    assert md.for_tab(EXPORT, [], 0) is None


def test_a_sliced_tab_is_also_sanitised():
    export = EXPORT.replace("We ran the system", "![][image1]\n\nWe ran the system") + B64
    out = md.for_tab(export, TABS, 0)
    assert out and "[figure 1]" in out and "data:image" not in out


# --- boundaries, from a two-tab document -----------------------------------

REAL = ("# Tab 1\n\n# Survey Coverage in the District Pilot\n\n"
        "The pilot reached forty percent of residents, which the rollout is assumed "
        "to match at full scale.\n\n# Tab 2\n")
REAL_TABS = [
    {"title": "Tab 1", "text": "Survey Coverage in the District Pilot\nThe pilot reached "
                               "forty percent of residents, which the rollout is assumed "
                               "to match at full scale.\n"},
    {"title": "Tab 2", "text": "\n"},
]


def test_a_tab_does_not_swallow_the_next_tabs_heading():
    # An empty following tab has no prose to find, so content-matching alone left
    # the previous slice running to the end of the file.
    out = md.for_tab(REAL, REAL_TABS, 0)
    assert out and "# Tab 2" not in out


def test_a_short_heading_before_the_first_long_line_is_kept():
    # The document's own title is short, under the distinctive-line threshold, so
    # content-matching started the slice after it and lost it.
    out = md.for_tab(REAL, REAL_TABS, 0)
    assert out and out.startswith("# Survey Coverage in the District Pilot")


def test_an_empty_tab_has_no_markdown():
    assert md.for_tab(REAL, REAL_TABS, 1) is None


def test_duplicate_tab_titles_fall_back_rather_than_guess():
    dupe = [{"title": "Notes", "text": REAL_TABS[0]["text"]},
            {"title": "Notes", "text": "wholly different content, long enough to match\n"}]
    export = "# Notes\n\n" + REAL_TABS[0]["text"] + "\n# Notes\n\nwholly different content, long enough to match\n"
    out = md.for_tab(export, dupe, 0)
    assert out is None or "wholly different" not in out


def test_a_missing_tab_title_falls_back_to_content_matching():
    export = REAL.replace("# Tab 1\n\n", "")  # first title absent
    out = md.for_tab(export, REAL_TABS, 0)
    assert out and "The pilot reached forty percent" in out


# --- a tab hierarchy, where a child renders its label differently ------------

DEEP_TABS = [
    {"title": "Report", "text": "The headline finding is that the pilot "
                                "reached forty percent of residents.\n"},
    {"title": "One-pager", "text": "The headline finding is that the pilot "
                                   "reached forty percent of residents.\n"},
    {"title": "Appendix", "text": "Weighting reaches ~40% of districts and "
                                  "is not obviously gameable.\n"},
]
DEEP = (
    "# Report\n\n"
    "The headline finding is that the pilot reached forty percent of residents.\n\n"
    "## # One-pager\n\n"
    "The headline finding is that the pilot reached forty percent of residents.\n\n"
    "# Appendix\n\n"
    "## Weighting reaches \\~40% of districts {#the-weighting}\n\n"
    "Weighting reaches \\~40% of districts and is not obviously gameable.\n"
)


def test_a_nested_tab_label_is_matched():
    # A child tab renders its label as `## # One-pager`; requiring `# One-pager`
    # missed it, and one miss used to discard the split for every tab.
    pos = md._title_positions(DEEP, [t["title"] for t in DEEP_TABS])
    assert all(p is not None for p in pos), pos


def test_markdown_escapes_do_not_defeat_the_coverage_check():
    # The export writes `\~40%` and appends `{#slug}`; comparing raw made a tab fail
    # its own coverage check and fall back to plain text.
    out = md.for_tab(DEEP, DEEP_TABS, 2)
    assert out and "of districts" in out


def test_tabs_that_share_text_are_both_kept():
    # A child tab condensing its parent scores as highly against the parent's slice
    # as the parent does. Rejecting on that discarded a correctly-bounded region.
    parent = md.for_tab(DEEP, DEEP_TABS, 0)
    child = md.for_tab(DEEP, DEEP_TABS, 1)
    assert parent and child
    assert "Appendix" not in parent, "parent must stop at the next located label"


def test_one_unmatched_label_does_not_cost_the_others():
    tabs = DEEP_TABS + [{"title": "Never Appears Anywhere", "text": "x" * 60 + "\n"}]
    pos = md._title_positions(DEEP, [t["title"] for t in tabs])
    assert pos[-1] is None and all(p is not None for p in pos[:-1])
    assert md.for_tab(DEEP, tabs, 0) is not None
