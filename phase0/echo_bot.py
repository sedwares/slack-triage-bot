#!/usr/bin/env python3
"""Phase 0 — verify Slack credentials and Socket Mode connectivity.

This file exists to isolate one question: **do my tokens work?**

It touches no database and calls no LLM. When it fails, there is exactly one
category of cause, which is worth a great deal when you are staring at a
misconfigured Slack app at 11pm.

Run it, then message the channel the bot was invited to.

    python phase0/echo_bot.py

What a healthy startup looks like:

    ✓ auth.test passed
      workspace : Acme Corp (T01ABCDEF)
      bot user  : triage-bot (U09XYZ)
    ⚡ connected — send a message in a channel the bot is in

Keep this file after Phase 1 ships. It stays useful as a five-second
credential check whenever the real bot goes quiet.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from slack_bolt.adapter.socket_mode.aiohttp import AsyncSocketModeHandler
from slack_bolt.app.async_app import AsyncApp

REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / ".env")


def _require(name: str, expected_prefix: str) -> str:
    """Fetch a required token and sanity-check its shape.

    Swapping the bot token and the app token is the single most common Slack
    setup mistake, and the resulting error from the API is opaque. Checking the
    prefix here turns a confusing 401 into a sentence that tells you what to fix.
    """
    value = os.environ.get(name, "").strip()
    if not value:
        sys.exit(f"✗ {name} is not set. Copy .env.example to .env and fill it in.")
    if not value.startswith(expected_prefix):
        sys.exit(
            f"✗ {name} should start with '{expected_prefix}' but starts with "
            f"'{value[:5]}...'. Check you have not swapped the bot and app tokens."
        )
    return value


async def main() -> None:
    bot_token = _require("SLACK_BOT_TOKEN", "xoxb-")
    app_token = _require("SLACK_APP_TOKEN", "xapp-")

    app = AsyncApp(token=bot_token)

    # auth.test before opening the socket. If the bot token is wrong we want to
    # fail here with a clear message, not inside a WebSocket reconnect loop.
    try:
        auth = await app.client.auth_test()
    except Exception as exc:  # noqa: BLE001 — top-level startup diagnostic
        sys.exit(
            f"✗ auth.test failed: {exc}\n"
            "  Check SLACK_BOT_TOKEN, and that the app is installed to the workspace."
        )

    print("✓ auth.test passed")
    print(f"  workspace : {auth['team']} ({auth['team_id']})")
    print(f"  bot user  : {auth['user']} ({auth['user_id']})")
    bot_user_id = auth["user_id"]

    @app.event("message")
    async def echo(event: dict, say, logger) -> None:  # noqa: ANN001
        """Echo back what the bot observed.

        The two guards below are not incidental — they are the same guards the
        Phase 1 prefilter needs, for the same reasons.
        """
        # 1. Never respond to ourselves. Without this the bot echoes its own
        #    echo forever and Slack rate-limits the app.
        if event.get("user") == bot_user_id or event.get("bot_id"):
            return

        # 2. Ignore subtypes (joins, edits, deletions, topic changes). These
        #    arrive as `message` events but often carry no user-authored text.
        if event.get("subtype"):
            return

        text = event.get("text", "")
        print(
            f"  [message] channel={event.get('channel')} "
            f"user={event.get('user')} ts={event.get('ts')} chars={len(text)}"
        )

        await say(
            text=(
                f"Phase 0 echo — received {len(text)} chars "
                f"from <@{event.get('user')}>"
            ),
            # Reply in-thread so verification does not spam the channel.
            thread_ts=event.get("thread_ts") or event.get("ts"),
        )

    handler = AsyncSocketModeHandler(app, app_token)
    print("⚡ connected — send a message in a channel the bot is in (Ctrl-C to stop)")
    await handler.start_async()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nstopped")
