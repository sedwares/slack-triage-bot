"""Slack Bolt event handlers.

One rule governs this module: **ack fast, work in the background.**

Slack requires an acknowledgement within three seconds. A tiered classification
with a possible escalation can exceed that. If we block the handler on the
pipeline, Slack times out, retries the event, and we pay twice — and the second
delivery is also slow, so it retries again.

So handlers acknowledge immediately and hand off to a background task. The
idempotency guard in the pipeline is the second line of defense for the cases
where a retry still slips through.
"""

from __future__ import annotations

import asyncio
from typing import Any

from slack_bolt.app.async_app import AsyncApp

from triage.logging_setup import get_logger
from triage.triage.pipeline import TriagePipeline

log = get_logger(__name__)

# Strong references to in-flight tasks. Without this, asyncio only holds weak
# references and the garbage collector can cancel a task mid-flight — a genuinely
# baffling bug to debug, because it manifests as messages silently vanishing.
_background_tasks: set[asyncio.Task[Any]] = set()


def _spawn(coro: Any) -> None:
    """Run a coroutine in the background, keeping a strong reference."""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


def register_handlers(
    app: AsyncApp,
    pipeline: TriagePipeline,
    bot_user_id: str,
) -> None:
    """Attach event handlers to the Bolt app.

    Args:
        app: The Bolt application.
        pipeline: Configured triage pipeline.
        bot_user_id: Our bot's user ID, for self-message filtering.
    """

    @app.event("message")
    async def handle_message(event: dict, body: dict, ack) -> None:  # noqa: ANN001
        """Handle an incoming channel message.

        Acks first, classifies second. Everything expensive happens after the
        acknowledgement has already gone back to Slack.
        """
        await ack()

        team_id = body.get("team_id") or event.get("team")
        event_id = body.get("event_id")

        if not team_id or not event_id:
            # Without an event_id there is no idempotency key, and without a
            # team_id there is no tenant. Both are structural, not transient.
            log.warning(
                "malformed_envelope",
                has_team_id=bool(team_id),
                has_event_id=bool(event_id),
            )
            return

        async def run() -> None:
            try:
                outcome = await pipeline.process(
                    event=event,
                    slack_team_id=team_id,
                    slack_event_id=event_id,
                    bot_user_id=bot_user_id,
                )

                # Phase 3 hooks in here. For now the decision is logged and
                # persisted so the trigger logic can be developed against real
                # data rather than guesses.
                if outcome.should_trigger:
                    log.info(
                        "trigger_threshold_crossed",
                        classification_id=outcome.classification_id,
                        urgency=outcome.urgency,
                        message_type=outcome.message_type,
                        note="Phase 3 will dispatch a workflow trigger here.",
                    )

            except Exception:  # noqa: BLE001
                # A background task that raises dies silently and takes the
                # message with it. Catch, log with traceback, keep the bot up.
                log.exception(
                    "pipeline_failed",
                    slack_event_id=event_id,
                    slack_team_id=team_id,
                )

        _spawn(run())

    @app.event("app_mention")
    async def handle_mention(event: dict, ack, say) -> None:  # noqa: ANN001
        """Respond to a direct mention.

        Kept minimal in Phase 1. The `message` handler already classifies the
        text; this exists so mentioning the bot produces some acknowledgement
        rather than apparent silence.
        """
        await ack()
        await say(
            text=(
                "I'm watching this channel and triaging messages. "
                "Thread summarization arrives in Phase 2."
            ),
            thread_ts=event.get("thread_ts") or event.get("ts"),
        )

    log.info("handlers_registered", events=["message", "app_mention"])
