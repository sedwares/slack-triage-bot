"""Heuristic prefilter — the cheapest tier, and the one that saves the most money.

Runs before any API call and answers two questions:

1. **Can this be decided without a model at all?** A bot post, a "thanks!", a
   lone emoji. In a real Slack channel this is a large share of traffic, and
   every one resolved here is a call not made.
2. **If a model is needed, which one?** Obvious high-stakes signals ("outage",
   "@here", "data loss") go straight to the deep tier, skipping a Haiku call
   that would have been escalated anyway.

Design constraints:

* **Pure and synchronous.** No I/O, no state. That makes it exhaustively
  testable, which matters because this is the component most likely to be wrong
  in an interesting way.
* **Precision over recall for auto-classification.** A message wrongly sent to a
  model costs a fraction of a cent. A real incident wrongly filtered out as
  chatter costs you the client. Every rule below is tuned accordingly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any

# --- Patterns --------------------------------------------------------------
# Compiled once at import. These run on every message in a busy channel.

_URL_RE = re.compile(r"https?://\S+")
_MENTION_RE = re.compile(r"<@[UW][A-Z0-9]+>")
_CODE_BLOCK_RE = re.compile(r"```|`[^`]+`")
_BROADCAST_RE = re.compile(r"<!(here|channel|everyone)>")
_EMOJI_ONLY_RE = re.compile(r"^(\s*(:[a-z0-9_+-]+:|[\U0001F300-\U0001FAFF])\s*)+$")

# Acknowledgements that carry no work content. Anchored and exact-matched after
# stripping punctuation — substring matching would swallow "ok, the database is
# down", which is emphatically not chatter.
_ACK_PHRASES = frozenset(
    {
        "ok", "okay", "k", "kk", "thanks", "thank you", "thx", "ty", "np",
        "yes", "no", "yep", "yeah", "nope", "sure", "got it", "gotcha",
        "lgtm", "ship it", "+1", "same", "agreed", "sounds good", "sgtm",
        "done", "nice", "cool", "great", "awesome", "perfect", "lol", "haha",
        "morning", "good morning", "gm", "bye", "brb", "afk", "ping",
    }
)

# Strong incident signals. Presence of any of these routes straight to the deep
# tier. Word-boundary matched so "downtime" does not match "down" spuriously and
# "produce" does not match "prod".
_CRITICAL_TERMS = frozenset(
    {
        "outage", "down", "downtime", "offline", "broken", "failing", "failed",
        "500s", "502", "503", "timeout", "timeouts", "crash", "crashed",
        "crashing", "p0", "p1", "sev0", "sev1", "sev-0", "sev-1", "incident",
        "urgent", "asap", "emergency", "critical", "blocker", "blocked",
        "data loss", "corrupted", "breach", "leaked", "exposed", "unauthorized",
        "rollback", "revert", "hotfix", "escalate", "escalation",
        "customer impact", "revenue", "sla",
    }
)

# The trailing `s?` is not cosmetic. Without it "502s" fails to match the term
# "502": the boundary between "2" and "s" is not a word boundary, so \b502\b
# never fires. Plural forms of critical terms are exactly how people actually
# write in Slack ("seeing 502s", "getting timeouts"), so missing them would
# silently defeat the rule on real traffic.
_CRITICAL_RE = re.compile(
    r"\b(" + "|".join(re.escape(t) for t in sorted(_CRITICAL_TERMS)) + r")s?\b",
    re.IGNORECASE,
)

# Subtypes that are not user-authored content.
_IGNORED_SUBTYPES = frozenset(
    {
        "channel_join", "channel_leave", "channel_topic", "channel_purpose",
        "channel_name", "channel_archive", "channel_unarchive",
        "bot_message", "message_changed", "message_deleted",
        "thread_broadcast", "file_comment", "pinned_item", "unpinned_item",
        "reminder_add", "tombstone", "huddle_thread",
    }
)

# Below this, there is not enough signal for a model to add value over the
# ack-phrase list. Tuned low deliberately: "db is down" is 11 characters.
_MIN_CHARS_FOR_LLM = 10

# Above this, the message is substantial enough that a cheap model is likely to
# hedge. Long messages tend to be multi-topic, which is exactly the ambiguity
# the deep tier exists for.
_LONG_MESSAGE_CHARS = 600


class Decision(str, Enum):
    """What the prefilter concluded."""

    SKIP = "skip"                # not classifiable content; do not store
    AUTO_CHATTER = "auto"        # confidently chatter; store, no LLM call
    ROUTE_CHEAP = "cheap"        # ordinary message; Haiku
    ROUTE_DEEP = "deep"          # ambiguous or high-stakes; Sonnet


@dataclass(frozen=True, slots=True)
class PrefilterResult:
    """Prefilter output.

    Attributes:
        decision: The routing decision.
        reason: Machine-readable reason code. Logged and used to analyse which
            rules actually fire in production — a rule that never fires is dead
            weight, and one that fires constantly may be too broad.
        features: Derived message features, reused for the ``message_events``
            row so they are computed exactly once.
    """

    decision: Decision
    reason: str
    features: dict[str, Any]


def extract_features(text: str, event: dict[str, Any]) -> dict[str, Any]:
    """Compute the metadata features stored on ``message_events``.

    These are the only representation of a message that is retained when a
    workspace has not opted into body storage, so analytics must work from them
    alone. Computed once and threaded through rather than recomputed at each
    layer.
    """
    return {
        "char_len": len(text),
        "word_count": len(text.split()),
        "mention_count": len(_MENTION_RE.findall(text)),
        "link_count": len(_URL_RE.findall(text)),
        "has_code_block": bool(_CODE_BLOCK_RE.search(text)),
        "has_question_mark": "?" in text,
        "is_broadcast": bool(_BROADCAST_RE.search(text)),
        "is_thread_reply": bool(event.get("thread_ts"))
        and event.get("thread_ts") != event.get("ts"),
    }


def _normalize_for_ack(text: str) -> str:
    """Strip emoji, punctuation, and whitespace for exact ack-phrase matching."""
    stripped = re.sub(r":[a-z0-9_+-]+:", "", text)          # :thumbsup:
    stripped = re.sub(r"[^\w\s]", "", stripped)             # punctuation
    return " ".join(stripped.lower().split())


def prefilter(
    text: str,
    event: dict[str, Any],
    *,
    bot_user_id: str | None = None,
) -> PrefilterResult:
    """Decide how (and whether) to classify a message.

    Rules are ordered cheapest-and-most-certain first. Each returns immediately,
    so the common cases cost a handful of string operations.

    Args:
        text: Raw message text from the Slack event.
        event: The full Slack event payload.
        bot_user_id: Our own bot user ID, so we never classify our own posts.

    Returns:
        A :class:`PrefilterResult`.
    """
    features = extract_features(text, event)

    # --- Rule 1: never process our own output -----------------------------
    # First because a self-processing loop is the one failure mode that gets
    # your app rate-limited within a minute.
    if event.get("bot_id") or (bot_user_id and event.get("user") == bot_user_id):
        return PrefilterResult(Decision.SKIP, "bot_message", features)

    # --- Rule 2: non-content subtypes -------------------------------------
    subtype = event.get("subtype")
    if subtype in _IGNORED_SUBTYPES:
        return PrefilterResult(Decision.SKIP, f"subtype_{subtype}", features)

    # --- Rule 3: empty or attachment-only ---------------------------------
    if not text or not text.strip():
        return PrefilterResult(Decision.SKIP, "empty_text", features)

    stripped = text.strip()

    # --- Rule 4: broadcasts jump the queue --------------------------------
    # Checked BEFORE the length rules: "<!here> down" is short, and is also the
    # most urgent thing that will happen all week. Ordering here is the rule.
    if features["is_broadcast"]:
        return PrefilterResult(Decision.ROUTE_DEEP, "broadcast_mention", features)

    # --- Rule 5: critical vocabulary --------------------------------------
    # Straight to deep tier. Sending these to Haiku wastes a call, because a
    # high-urgency cheap result gets escalated by the router anyway.
    if _CRITICAL_RE.search(stripped):
        return PrefilterResult(Decision.ROUTE_DEEP, "critical_term", features)

    # --- Rule 6: emoji-only ------------------------------------------------
    if _EMOJI_ONLY_RE.match(stripped):
        return PrefilterResult(Decision.AUTO_CHATTER, "emoji_only", features)

    # --- Rule 7: exact acknowledgement phrases ----------------------------
    # Exact match, never substring. This is the rule most likely to cause harm
    # if loosened: "ok" is chatter, "ok the database is down" is not.
    #
    # Checked twice, against the raw lowercased text and against the normalized
    # form. Normalization strips punctuation, which would turn "+1" into "1" and
    # miss it; the raw check catches punctuation-bearing acks directly.
    lowered = stripped.lower()
    normalized = _normalize_for_ack(stripped)
    if lowered in _ACK_PHRASES or normalized in _ACK_PHRASES:
        return PrefilterResult(Decision.AUTO_CHATTER, "ack_phrase", features)

    # --- Rule 8: too short to carry meaning -------------------------------
    # Safe only because Rules 4 and 5 already rescued short urgent messages.
    if len(normalized) < _MIN_CHARS_FOR_LLM:
        return PrefilterResult(Decision.AUTO_CHATTER, "too_short", features)

    # --- Rule 9: signals of ambiguity -> deep tier ------------------------
    if len(stripped) > _LONG_MESSAGE_CHARS:
        return PrefilterResult(Decision.ROUTE_DEEP, "long_message", features)

    # A question aimed at specific people is usually a real request that needs
    # correct owner attribution — worth the better model.
    if features["has_question_mark"] and features["mention_count"] >= 2:
        return PrefilterResult(Decision.ROUTE_DEEP, "multi_mention_question", features)

    # --- Rule 10: default ---------------------------------------------------
    # The router may still escalate this on low confidence or high urgency.
    return PrefilterResult(Decision.ROUTE_CHEAP, "default", features)
