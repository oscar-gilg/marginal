"""Can the commenter actually see a figure, and use what it shows?

Two separate claims, because they fail for different reasons. The transport test
proves an image survives the trip to whichever provider answers — a wrong content
block shape is a 400, and a dropped one is silent. The document test proves the
same for a figure that came out of a real Google Doc, which adds the Docs API's
`contentUri` and its short-lived download to the path.

The figures here carry a property that can be checked, so a model replying with
plausible boilerplate fails rather than passes.
"""

import base64
import io

import pytest

from marginal import figures, gdocs, model

pytestmark = pytest.mark.e2e


def bar_chart_png() -> bytes:
    """A chart whose tallest bar is unambiguous, and is not the last one."""
    matplotlib = pytest.importorskip("matplotlib")
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(4, 3))
    ax.bar(["alpha", "beta", "gamma"], [2, 9, 3])
    ax.set_ylabel("score")
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=80)
    plt.close(fig)
    return buf.getvalue()


def test_an_image_reaches_the_model_and_is_read_correctly(cfg):
    img = {
        "at": 0,
        "alt": "",
        "media_type": "image/png",
        "data": base64.b64encode(bar_chart_png()).decode(),
    }
    answer = model.converse(
        "Answer with one lowercase word and nothing else.",
        [{"role": "user", "content": figures.blocks([img]) + [
            {"type": "text", "text": "Which bar is tallest: alpha, beta or gamma?"}]}],
        model=cfg.model, provider=cfg.provider, max_tokens=100,
    )
    # Boilerplate cannot pass this: the answer is only knowable from the pixels.
    assert "beta" in answer.lower(), answer


def test_a_figure_from_a_real_document_reaches_the_model(fixture_doc, token, cfg):
    tab = gdocs.read_doc(fixture_doc, token)["tabs"][0]
    assert tab["figures"], "fixture has no inline image"
    imgs = figures.fetch(tab["figures"], cfg.max_figures, cfg.max_figure_bytes)
    assert imgs, "the document's figure could not be downloaded"
    assert imgs[0]["media_type"] in figures.MEDIA_TYPES
    answer = model.converse(
        "Answer in a few words.",
        [{"role": "user", "content": figures.blocks(imgs) + [
            {"type": "text", "text": "What company's logo is this?"}]}],
        model=cfg.model, provider=cfg.provider, max_tokens=100,
    )
    assert "google" in answer.lower(), answer


def test_an_oversized_figure_is_skipped_not_fetched(cfg):
    # A cap that raised instead of skipping would lose the whole review to one
    # oversized image.
    tiny = figures.fetch(
        [{"at": 0, "uri": "https://www.google.com/images/branding/googlelogo/2x/"
                          "googlelogo_color_92x30dp.png"}],
        limit=8, max_bytes=10,
    )
    assert tiny == []


def test_an_unreachable_figure_is_skipped(cfg):
    assert figures.fetch([{"at": 0, "uri": "https://example.invalid/x.png"}], 8, 10**6) == []
    assert figures.fetch([{"at": 0, "uri": None}], 8, 10**6) == []
