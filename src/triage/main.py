"""Application entrypoint.

Startup order matters and is not arbitrary:

1. Logging — so every later failure is captured in structured form.
2. Config — validates all secrets and fails immediately if any are missing.
3. Database pool — proves connectivity before we accept any traffic.
4. Pricing cache — **must** load before the first LLM call, or costing raises.
5. Slack auth — verifies the token and gives us our own user ID.
6. Socket Mode — only now do we start receiving events.

Every step before 6 is a fail-fast gate. A bot that starts successfully and then
discovers it cannot reach Postgres has already accepted events it will drop.

Run with:  python -m triage.main
"""

from __future__ import annotations

import asyncio
import signal
import sys

from slack_bolt.adapter.socket_mode.aiohttp import AsyncSocketModeHandler
from slack_bolt.app.async_app import AsyncApp

from triage.config import get_settings
from triage.db.pool import close_pool, init_pool
from triage.llm.client import AnthropicClient
from triage.llm.pricing import pricing_cache
from triage.logging_setup import configure_logging, get_logger
from triage.slack.handlers import register_handlers
from triage.triage.pipeline import TriagePipeline
from triage.triage.router import TieredRouter

log = get_logger(__name__)


async def run() -> None:
    # --- 1. Logging ---------------------------------------------------------
    settings = get_settings()
    configure_logging(level=settings.log_level, renderer=settings.log_renderer)

    log.info(
        "starting",
        environment=settings.environment,
        cheap_model=settings.triage_cheap_model,
        deep_model=settings.triage_deep_model,
        prompt_version=settings.prompt_version,
        pipeline_version=settings.pipeline_version,
    )

    # --- 2/3. Database ------------------------------------------------------
    await init_pool(settings)

    # --- 4. Pricing ---------------------------------------------------------
    # Before any LLM call. Costing raises on an unknown model rather than
    # inventing a number, so an unseeded database must fail here, loudly,
    # at startup — not on the first classification in production.
    await pricing_cache.refresh()

    # --- 5. Slack auth ------------------------------------------------------
    app = AsyncApp(token=settings.slack_bot_token.get_secret_value())
    try:
        auth = await app.client.auth_test()
    except Exception as exc:  # noqa: BLE001
        log.error("slack_auth_failed", error=str(exc))
        await close_pool()
        sys.exit(
            "Slack authentication failed. Run `make phase0` to isolate the "
            "credential problem."
        )

    bot_user_id: str = auth["user_id"]
    log.info(
        "slack_authenticated",
        team=auth["team"],
        team_id=auth["team_id"],
        bot_user_id=bot_user_id,
    )

    # --- Wire the object graph ----------------------------------------------
    # Constructor injection throughout: no module-level singletons beyond the
    # pool and pricing cache, so tests can substitute any layer.
    anthropic_client = AnthropicClient(settings)
    router = TieredRouter(anthropic_client, settings)
    pipeline = TriagePipeline(router, settings)
    register_handlers(app, pipeline, bot_user_id)

    # --- 6. Socket Mode -----------------------------------------------------
    handler = AsyncSocketModeHandler(
        app, settings.slack_app_token.get_secret_value()
    )

    stop = asyncio.Event()

    def request_shutdown(sig: signal.Signals) -> None:
        log.info("shutdown_signal", signal=sig.name)
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        # SIGTERM handling matters for Fly.io: without it, a deploy kills the
        # process mid-classification and the in-flight work is lost silently.
        loop.add_signal_handler(sig, request_shutdown, sig)

    log.info("connected", note="listening for events")

    socket_task = asyncio.create_task(handler.start_async())
    await stop.wait()

    # --- Graceful shutdown --------------------------------------------------
    log.info("shutting_down")
    socket_task.cancel()
    try:
        await socket_task
    except asyncio.CancelledError:
        pass

    await anthropic_client.close()
    await close_pool()
    log.info("stopped")


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
