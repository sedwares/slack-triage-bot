"""Prefilter tests.

The prefilter is pure and synchronous, which makes it cheap to test
exhaustively — and it is the component whose bugs are most expensive, because a
wrongly-filtered message never reaches a model at all.

The tests are weighted accordingly. There are more cases guarding against
*under*-classification (a real incident dismissed as chatter) than against
over-classification, because the costs are wildly asymmetric: a needless Haiku
call costs $0.0002, a missed outage costs the client relationship.
"""

from __future__ import annotations

import pytest

from triage.triage.prefilter import Decision, prefilter

BOT_ID = "U0BOTBOT"


def event(text: str = "", **overrides) -> dict:
    """Build a minimal Slack message event."""
    base = {
        "type": "message",
        "text": text,
        "user": "U0HUMAN",
        "channel": "C0GENERAL",
        "ts": "1754236800.000100",
    }
    base.update(overrides)
    return base


# --- SKIP: not user-authored content ---------------------------------------


def test_skips_own_bot_messages():
    """Self-processing is the loop that gets an app rate-limited."""
    result = prefilter("hello", event("hello", user=BOT_ID), bot_user_id=BOT_ID)
    assert result.decision is Decision.SKIP
    assert result.reason == "bot_message"


def test_skips_other_bots():
    result = prefilter("deploy finished", event("deploy finished", bot_id="B123"))
    assert result.decision is Decision.SKIP


@pytest.mark.parametrize("subtype", ["channel_join", "message_changed", "channel_topic"])
def test_skips_non_content_subtypes(subtype):
    result = prefilter("x joined", event("x joined", subtype=subtype))
    assert result.decision is Decision.SKIP


def test_skips_empty_text():
    assert prefilter("", event()).decision is Decision.SKIP
    assert prefilter("   \n  ", event()).decision is Decision.SKIP


# --- AUTO_CHATTER: resolved without a model --------------------------------


@pytest.mark.parametrize("text", ["ok", "thanks!", "LGTM", "+1", "sounds good", "ty"])
def test_acknowledgements_resolve_without_llm(text):
    result = prefilter(text, event(text))
    assert result.decision is Decision.AUTO_CHATTER
    assert result.reason == "ack_phrase"


@pytest.mark.parametrize("text", [":tada:", ":thumbsup: :rocket:", "🎉"])
def test_emoji_only_resolves_without_llm(text):
    assert prefilter(text, event(text)).decision is Decision.AUTO_CHATTER


# --- The asymmetry: never filter out a real incident -----------------------


@pytest.mark.parametrize(
    "text",
    [
        "the api is down",
        "prod is broken",
        "we have an outage",
        "P1 - checkout failing",
        "possible data loss on the users table",
        "seeing 502s from the gateway",
        "URGENT: customers cannot log in",
        "security breach in the admin panel",
    ],
)
def test_critical_messages_route_to_deep_tier(text):
    """These must never be auto-classified as chatter, at any length."""
    result = prefilter(text, event(text))
    assert result.decision is Decision.ROUTE_DEEP
    assert result.reason == "critical_term"


def test_short_urgent_message_survives_length_rules():
    """"db is down" is 10 characters. The length rule must not swallow it.

    This is the specific ordering bug the rule sequence exists to prevent:
    critical-term matching runs BEFORE the too-short check.
    """
    text = "db is down"
    result = prefilter(text, event(text))
    assert result.decision is Decision.ROUTE_DEEP


def test_broadcast_routes_to_deep_even_when_short():
    text = "<!here> help"
    result = prefilter(text, event(text))
    assert result.decision is Decision.ROUTE_DEEP
    assert result.reason == "broadcast_mention"


def test_ack_matching_is_exact_not_substring():
    """"ok" is chatter. "ok the database is down" emphatically is not.

    Substring matching here would be a silent, severe bug: it would drop real
    incidents that happen to begin with a filler word.
    """
    text = "ok the database is down"
    result = prefilter(text, event(text))
    assert result.decision is not Decision.AUTO_CHATTER
    assert result.decision is Decision.ROUTE_DEEP


def test_word_boundary_prevents_false_critical_match():
    """"downtime" contains "down"; "rundown" should not trigger on its own."""
    text = "sharing the sprint rundown for this week and next"
    result = prefilter(text, event(text))
    assert result.decision is Decision.ROUTE_CHEAP


# --- Routing to the cheap tier ---------------------------------------------


def test_ordinary_message_routes_cheap():
    text = "Can someone review my PR when they get a chance? Not blocking."
    result = prefilter(text, event(text))
    assert result.decision is Decision.ROUTE_CHEAP
    assert result.reason == "default"


def test_long_message_routes_deep():
    text = "I have a few things to raise about the migration plan. " * 15
    result = prefilter(text, event(text))
    assert result.decision is Decision.ROUTE_DEEP
    assert result.reason == "long_message"


def test_multi_mention_question_routes_deep():
    text = "<@U111> <@U222> should we go with option A or B for the rollout?"
    result = prefilter(text, event(text))
    assert result.decision is Decision.ROUTE_DEEP


# --- Feature extraction -----------------------------------------------------


def test_features_computed_once_and_complete():
    text = "<@U123> check https://example.com — see `code` ?"
    result = prefilter(text, event(text))
    f = result.features
    assert f["mention_count"] == 1
    assert f["link_count"] == 1
    assert f["has_code_block"] is True
    assert f["has_question_mark"] is True
    assert f["char_len"] == len(text)


def test_thread_reply_detected():
    ev = event("a reply", ts="1754236900.000200", thread_ts="1754236800.000100")
    result = prefilter("a reply", ev)
    assert result.features["is_thread_reply"] is True


def test_thread_parent_is_not_a_reply():
    """A thread parent has thread_ts == ts and must not be flagged as a reply."""
    ev = event("parent", ts="1754236800.000100", thread_ts="1754236800.000100")
    assert prefilter("parent", ev).features["is_thread_reply"] is False
