"""Structured logging, configured once at process start.

Two decisions worth calling out:

1. **JSON in production, human-readable locally.** Same structured events, two
   renderers. You never have to choose between greppable logs and readable ones.

2. **A redaction processor sits in the pipeline, not at the call sites.**
   Relying on every developer to remember not to log message bodies is a policy
   that fails silently. Enforcing it in one processor means the failure mode is
   a redacted log line rather than a privacy incident.
"""

from __future__ import annotations

import logging
import sys
from typing import Any, MutableMapping

import structlog

# Keys that must never reach a log sink in cleartext. The pipeline drops or
# masks these regardless of what the calling code passed.
_REDACTED_KEYS = frozenset(
    {
        "body",
        "text",
        "message_text",
        "content",
        "token",
        "api_key",
        "authorization",
        "slack_bot_token",
        "slack_app_token",
        "anthropic_api_key",
        "content_hmac_master_key",
        "prompt",
        "completion",
    }
)

_REDACTION_PLACEHOLDER = "[redacted]"


def _redact_sensitive(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Mask sensitive keys anywhere in the event dict, including nested dicts.

    Replaces values rather than deleting keys, so the shape of a log line stays
    stable and you can still tell that a field was present.
    """

    def scrub(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                k: (_REDACTION_PLACEHOLDER if k.lower() in _REDACTED_KEYS else scrub(v))
                for k, v in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [scrub(v) for v in value]
        return value

    for key in list(event_dict.keys()):
        if key.lower() in _REDACTED_KEYS:
            event_dict[key] = _REDACTION_PLACEHOLDER
        else:
            event_dict[key] = scrub(event_dict[key])

    return event_dict


def _add_service_context(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Stamp every line with the service name so logs stay attributable once
    they are shipped somewhere shared."""
    event_dict.setdefault("service", "slack-triage-bot")
    return event_dict


def configure_logging(level: str = "INFO", renderer: str = "console") -> None:
    """Configure structlog and the stdlib root logger.

    Call exactly once, as early as possible in ``main``.

    Args:
        level: Standard logging level name, e.g. ``"INFO"``.
        renderer: ``"json"`` for one-line JSON, ``"console"`` for coloured
            development output.
    """
    # Route stdlib logging (slack_bolt, asyncpg, httpx all use it) through the
    # same handler so output is uniform rather than two interleaved formats.
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, level.upper(), logging.INFO),
    )

    # These libraries are chatty at INFO and say nothing useful.
    logging.getLogger("slack_bolt").setLevel(logging.WARNING)
    logging.getLogger("slack_sdk").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)

    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,  # request-scoped bound context
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _add_service_context,
        _redact_sensitive,  # runs before any renderer — no bypass path
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if renderer == "json":
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer(colors=True))

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a bound logger for a module."""
    return structlog.get_logger(name)
