"""Prompt construction.

Prompts are built from :mod:`triage.triage.schemas`, never hand-written with the
category list retyped. One definition, rendered into both the tool schema and
the prompt text, so they cannot drift.

The system prompt is **byte-identical across every call**, which is what makes
prompt caching work: cache hits bill at 0.1x base input. Anything that varies per
message — the message itself, the channel — belongs in the user turn.

Whenever you edit anything in this module, bump ``PROMPT_VERSION`` in ``.env``.
Historical classifications carry the version that produced them; if two different
prompts share a version string, your eval comparisons are silently meaningless.
"""

from __future__ import annotations

from triage.triage.schemas import MESSAGE_TYPES, URGENCY_SCALE

_TYPE_BLOCK = "\n".join(
    f"- {key}: {description}" for key, description in MESSAGE_TYPES.items()
)

_URGENCY_BLOCK = "\n".join(
    f"- {level}: {description}" for level, description in sorted(URGENCY_SCALE.items())
)

CLASSIFIER_SYSTEM_PROMPT = f"""\
You triage messages posted in a team's Slack channel. For each message you \
assign a category, an urgency level, and the role best suited to handle it.

CATEGORIES
{_TYPE_BLOCK}

URGENCY SCALE
{_URGENCY_BLOCK}

GUIDELINES

Judge the message on its own terms. A message that sounds calm can still be \
urgent, and a message with exclamation marks is often not.

Assign urgency 5 only when something is actively broken, data is at risk, or \
there is a security concern. Reserve it — a system that cries wolf gets muted, \
and a muted system has no value.

For suggested_owner, name a ROLE or TEAM, never an individual. You do not know \
who is on call or who owns which service, and a confidently wrong @mention is \
worse than a generic but correct role.

Report confidence honestly. Low confidence is not a failure — it routes the \
message to a stronger model, which is the right outcome for a genuinely \
ambiguous message. Inflating confidence to seem decisive defeats the routing.

If a message contains several distinct concerns, classify it by the one with \
the highest urgency.

Call the record_triage tool exactly once. Do not write any other text.\
"""


def build_user_content(
    *,
    text: str,
    channel_name: str | None = None,
    is_thread_reply: bool = False,
) -> str:
    """Assemble the per-message user turn.

    Everything variable lives here so the cached system prefix stays stable.

    Args:
        text: The raw message text.
        channel_name: Channel name when known. Real context — the same words in
            ``#incidents`` and in ``#random`` warrant different urgency.
        is_thread_reply: Whether this is a reply within a thread. Replies are
            usually lower-urgency continuations of an existing conversation.

    Returns:
        The user message content string.
    """
    lines: list[str] = []

    if channel_name:
        lines.append(f"Channel: #{channel_name}")
    if is_thread_reply:
        lines.append("Context: this is a reply within an existing thread.")

    if lines:
        lines.append("")

    # Delimited so message content cannot be confused with instructions. The
    # system prompt is fixed and the model is constrained to a tool call, so
    # this is defense in depth rather than the only safeguard.
    lines.append("Message to triage:")
    lines.append("<message>")
    lines.append(text)
    lines.append("</message>")

    return "\n".join(lines)
