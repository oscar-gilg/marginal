"""Emoji reactions: splitting them out of the export, and the join back.

The export half is exercised against a captured export shape; everything that can be
wrong without a browser is here. The join is the part worth pinning — a reaction
hung on the wrong comment is the same class of failure as a comment anchored to
the wrong sentence, and just as invisible in the output.
"""

from marginal import reactions

EXPORTED = (
    "**fable-5**\nDoes the pilot's response rate hold once the sample is district-wide?\n"
    "1 total reaction\n"
    "Ada Lovelace reacted with 😬 at 2026-01-02 5:43 PM UTC"
)


def test_the_export_carries_reactions_in_the_comment_text():
    """The shape the docx export writes, with the reaction inside the comment text.

    Better than the DOM on every axis but availability: the reactor's name rather
    than "You", a timestamp, and the whole document in one download.
    """
    body, reacts = reactions.split_body(EXPORTED)
    assert body == (
        "**fable-5**\nDoes the pilot's response rate hold once the sample is district-wide?"
    )
    assert reacts == [{
        "emoji": "😬", "count": 1,
        "voters": ["Ada Lovelace"], "at": "2026-01-02 5:43 PM UTC",
    }]


def test_a_reacted_comment_still_matches_the_body_that_was_posted():
    """The regression that made this urgent rather than merely missing.

    `post.verify` matches `c["body"] == r.body.strip()`. The export appends the
    reaction trailer to the comment's own text, so under `source = "browser"` a
    comment somebody reacted to stopped equalling the body we posted, and a live,
    correctly anchored comment was reported as "posted comment could not be read
    back". Nothing about it looked like a reactions problem.
    """
    posted = ("**fable-5**\nDoes the pilot's response rate hold once the sample is "
              "district-wide?")
    assert reactions.split_body(EXPORTED)[0] == posted


def test_several_reactions_each_keep_their_reactor():
    text = ("the body\n2 total reactions\n"
            "Ada Lovelace reacted with 👍 at 2026-01-02 5:43 PM UTC\n"
            "Grace Hopper reacted with 🎉 at 2026-01-02 6:01 PM UTC")
    body, reacts = reactions.split_body(text)
    assert body == "the body"
    assert [r["voters"][0] for r in reacts] == ["Ada Lovelace", "Grace Hopper"]
    assert [r["emoji"] for r in reacts] == ["👍", "🎉"]


def test_a_comment_with_no_reactions_is_returned_unchanged():
    assert reactions.split_body("just a comment\nwith two lines") == (
        "just a comment\nwith two lines", []
    )


def test_a_body_that_merely_mentions_a_total_keeps_its_line():
    # Splitting on the marker alone would eat a line of somebody's actual comment.
    text = "the counts do not add up: 3 total reactions on a doc with two reviewers"
    assert reactions.split_body(text) == (text, [])


def test_a_model_comment_joins_despite_its_attribution_header():
    """Why the join strips the header before matching.

    The API and the export do not render the header identically, and every comment
    this tool posts carries one — so the raw join would fail on exactly the
    comments being collected about, and fail as "no reactions", which reads as a
    document nobody reacted to.
    """
    card = ("Ada Lovelace6:37 PM TodayNew Approver *fable-5*Does the pilot's response "
            "rate hold once the sample is district-wide?")
    comments = [{
        "id": "c1",
        "content": "**fable-5**\nDoes the pilot's response rate hold once the sample is "
                   "district-wide?",
        "replies": [],
    }]
    posts = [{"text": card, "reactions": [{"emoji": "😬", "count": 1, "voters": ["You"]}]}]
    assert reactions.attach(comments, posts) == []
    assert comments[0]["reactions"][0]["emoji"] == "😬"


def _comment(cid, content, replies=()):
    return {"id": cid, "content": content, "replies": [{"content": r} for r in replies]}


def test_a_reaction_lands_on_the_comment_whose_text_the_card_carries():
    comments = [
        _comment("c1", "The pilot sample over-represents dense districts, so the rate will not transfer."),
        _comment("c2", "Response rate is easy to raise in ways that make the sample worse."),
    ]
    posts = [{
        "text": "Ada Lovelace 14:35 Response rate is easy to raise in ways that make the sample worse.",
        "reactions": [{"emoji": "👍", "count": 1, "voters": ["Ada"]}],
    }]
    assert reactions.attach(comments, posts) == []
    assert "reactions" not in comments[0]
    assert comments[1]["reactions"][0]["emoji"] == "👍"


def test_a_reply_carries_its_own_reaction():
    # One card is one post, so a reply's card contains the reply and not its parent.
    comments = [_comment(
        "c1",
        "The pilot sample over-represents dense districts, so the rate will not transfer.",
        replies=["Agreed, we reweight by district size before extrapolating."],
    )]
    posts = [{
        "text": "Grace Hopper 09:12 Agreed, we reweight by district size before extrapolating.",
        "reactions": [{"emoji": "🎉", "count": 1, "voters": []}],
    }]
    assert reactions.attach(comments, posts) == []
    assert "reactions" not in comments[0], "the reaction was hung on the parent comment"
    assert comments[0]["replies"][0]["reactions"][0]["emoji"] == "🎉"


def test_a_card_matching_two_comments_is_refused_rather_than_guessed():
    """Ambiguity resolves to absent, as it does for anchors.

    A tie-break here would be a plausible guess that is silently wrong and looks in
    the output exactly like a correct answer — the failure mode this repository
    treats as worse than no answer at all.
    """
    comments = [_comment(
        "c1",
        "The pilot sample over-represents dense districts, so the rate will not transfer.",
        replies=["Agreed, we reweight by district size before extrapolating."],
    )]
    posts = [{
        "text": ("Ada Lovelace The pilot sample over-represents dense districts, so the "
                 "rate will not transfer. Grace Hopper Agreed, we reweight by district "
                 "size before extrapolating."),
        "reactions": [{"emoji": "🎉", "count": 1, "voters": []}],
    }]
    unmatched = reactions.attach(comments, posts)
    assert unmatched and "2 comments" in unmatched[0]
    assert "reactions" not in comments[0]
    assert "reactions" not in comments[0]["replies"][0]


def test_a_short_body_is_left_unjoined_rather_than_guessed():
    # "+1" appears inside half the cards on a document; a containment join on it
    # would attach reactions to whichever comment happened to be scanned first.
    comments = [_comment("c1", "+1")]
    posts = [{"text": "Ada 10:00 +1", "reactions": [{"emoji": "👍", "count": 1, "voters": []}]}]
    unmatched = reactions.attach(comments, posts)
    assert unmatched and "reactions" not in comments[0]


def test_an_unmatched_card_is_reported():
    comments = [_comment("c1", "a comment long enough to be joined by containment")]
    posts = [{"text": "Ada 10:00 something else entirely, from a deleted comment",
              "reactions": [{"emoji": "👀", "count": 1, "voters": []}]}]
    assert len(reactions.attach(comments, posts)) == 1


def test_the_join_survives_rewrapped_whitespace():
    comments = [_comment("c1", "The pilot sample over-represents\ndense districts, so the rate moves.")]
    posts = [{"text": "Ada  The pilot sample over-represents dense districts, so the rate moves.",
              "reactions": [{"emoji": "👍", "count": 1, "voters": []}]}]
    assert reactions.attach(comments, posts) == []
    assert comments[0]["reactions"]
