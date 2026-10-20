"""Anthropic API wrapper with cost and latency accounting on every call.

Every call — successes, retries, and terminal failures alike — produces an
invocation record. Recording only successes gives you optimistic cost figures
and an error rate of zero, which is worse than no metrics because it looks fine.

Structured output uses **forced tool use**
(``tool_choice={"type": "tool", "name": ...}``), so the response is constrained
to the tool's input schema. No JSON-in-prose, no stripping markdown fences, no
"the model added a preamble again" class of bug.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from anthropic import AsyncAnthropic
from anthropic import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    RateLimitError,
)

from triage.config import Settings
from triage.llm.pricing import pricing_cache
from triage.logging_setup import get_logger

log = get_logger(__name__)

_MAX_ATTEMPTS = 3
_BASE_BACKOFF_SECONDS = 0.5


@dataclass(slots=True)
class ToolCallOutcome:
    """Result of one logical LLM operation, including any retries.

    Attributes:
        tool_input: The validated tool arguments, or ``None`` if every attempt
            failed.
        invocations: One record per HTTP attempt, ready for
            ``llm_invocations``. Always populated, even on total failure.
    """

    tool_input: dict[str, Any] | None
    invocations: list[dict[str, Any]] = field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        return self.tool_input is not None


class AnthropicClient:
    """Thin wrapper adding retries, cost accounting, and structured logging."""

    def __init__(self, settings: Settings) -> None:
        self._client = AsyncAnthropic(
            api_key=settings.anthropic_api_key.get_secret_value(),
            # Our own retry loop handles backoff so that each attempt produces
            # its own invocation row. The SDK's internal retries would be
            # invisible to the cost table.
            max_retries=0,
            timeout=30.0,
        )
        self._settings = settings

    async def call_with_tool(
        self,
        *,
        model: str,
        tier: str,
        purpose: str,
        system: str,
        user_content: str,
        tool: dict[str, Any],
        max_tokens: int = 512,
        temperature: float = 0.0,
    ) -> ToolCallOutcome:
        """Invoke the model and force it to answer through ``tool``.

        Args:
            model: Model ID. Must exist in ``triage.model_pricing``.
            tier: ``'cheap'`` or ``'deep'``; stored on the invocation row.
            purpose: ``'classify'``, ``'escalate'``, or ``'summarize'``.
            system: System prompt. Marked for caching — it is identical across
                every message, so after the first call it bills at 0.1x.
            user_content: The message to classify.
            tool: Tool definition from ``build_classification_tool()``.
            max_tokens: Output cap. Classification output is tiny; a low cap is
                a cheap guard against a runaway generation.
            temperature: 0.0 for classification — determinism makes evals
                meaningful and makes bug reports reproducible.

        Returns:
            A :class:`ToolCallOutcome`.
        """
        outcome = ToolCallOutcome(tool_input=None)

        for attempt in range(1, _MAX_ATTEMPTS + 1):
            started = time.perf_counter()
            try:
                response = await self._client.messages.create(
                    model=model,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    system=[
                        {
                            "type": "text",
                            "text": system,
                            # The system prompt plus tool schema is the largest
                            # constant part of every request. Caching it is the
                            # single highest-leverage cost optimisation here.
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                    messages=[{"role": "user", "content": user_content}],
                    tools=[tool],
                    tool_choice={"type": "tool", "name": tool["name"]},
                )
                latency_ms = int((time.perf_counter() - started) * 1000)

                usage = response.usage
                input_tokens = getattr(usage, "input_tokens", 0) or 0
                output_tokens = getattr(usage, "output_tokens", 0) or 0
                cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
                cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0

                cost = pricing_cache.price(
                    model=model,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cache_read_tokens=cache_read,
                    cache_write_tokens=cache_write,
                )

                # Pull the forced tool call out of the content blocks.
                tool_input: dict[str, Any] | None = None
                for block in response.content:
                    if getattr(block, "type", None) == "tool_use":
                        tool_input = dict(block.input)
                        break

                status = "ok" if tool_input is not None else "refusal"

                outcome.invocations.append(
                    {
                        "purpose": purpose,
                        "model": model,
                        "tier": tier,
                        "provider_request_id": getattr(response, "id", None),
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                        "cache_read_tokens": cache_read,
                        "cache_write_tokens": cache_write,
                        "cost_usd": cost.cost_usd,
                        "pricing_valid_from": cost.pricing_valid_from,
                        "latency_ms": latency_ms,
                        "attempt": attempt,
                        "status": status,
                        "stop_reason": response.stop_reason,
                        "error_class": None,
                    }
                )

                log.info(
                    "llm_call_complete",
                    model=model,
                    tier=tier,
                    purpose=purpose,
                    status=status,
                    latency_ms=latency_ms,
                    input_tokens=input_tokens,
                    cache_read_tokens=cache_read,
                    output_tokens=output_tokens,
                    cost_usd=str(cost.cost_usd),
                    attempt=attempt,
                )

                if tool_input is not None:
                    outcome.tool_input = tool_input
                    return outcome

                # Forced tool use returned no tool block. Retrying is unlikely
                # to help at temperature 0 — same input, same output.
                return outcome

            except (RateLimitError, APITimeoutError, APIConnectionError) as exc:
                latency_ms = int((time.perf_counter() - started) * 1000)
                status = (
                    "rate_limited"
                    if isinstance(exc, RateLimitError)
                    else "timeout"
                    if isinstance(exc, APITimeoutError)
                    else "error"
                )
                outcome.invocations.append(
                    self._failure_record(
                        purpose=purpose,
                        model=model,
                        tier=tier,
                        latency_ms=latency_ms,
                        attempt=attempt,
                        status=status,
                        error_class=type(exc).__name__,
                    )
                )
                log.warning(
                    "llm_call_retryable_error",
                    model=model,
                    tier=tier,
                    attempt=attempt,
                    error_class=type(exc).__name__,
                    latency_ms=latency_ms,
                )

                if attempt == _MAX_ATTEMPTS:
                    return outcome

                # Exponential backoff. Jitter is omitted deliberately: this is a
                # single-process bot, so there is no thundering herd to spread.
                await asyncio.sleep(_BASE_BACKOFF_SECONDS * (2 ** (attempt - 1)))

            except APIStatusError as exc:
                # 4xx other than 429 — bad request, auth failure, unknown model.
                # Retrying an unchanged request cannot fix any of these.
                latency_ms = int((time.perf_counter() - started) * 1000)
                outcome.invocations.append(
                    self._failure_record(
                        purpose=purpose,
                        model=model,
                        tier=tier,
                        latency_ms=latency_ms,
                        attempt=attempt,
                        status="error",
                        error_class=f"{type(exc).__name__}:{exc.status_code}",
                    )
                )
                log.error(
                    "llm_call_fatal_error",
                    model=model,
                    tier=tier,
                    status_code=exc.status_code,
                    error_class=type(exc).__name__,
                )
                return outcome

        return outcome

    def _failure_record(
        self,
        *,
        purpose: str,
        model: str,
        tier: str,
        latency_ms: int,
        attempt: int,
        status: str,
        error_class: str,
    ) -> dict[str, Any]:
        """Build an invocation row for a call that produced no usage data.

        Cost is zero (nothing was billed) but ``pricing_valid_from`` must still
        resolve, because the composite FK to ``model_pricing`` is NOT NULL. That
        constraint is doing its job here: it forces every invocation row,
        including failures, to name a model the system actually knows about.
        """
        from decimal import Decimal

        return {
            "purpose": purpose,
            "model": model,
            "tier": tier,
            "provider_request_id": None,
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "cost_usd": Decimal(0),
            "pricing_valid_from": pricing_cache.pricing_valid_from(model),
            "latency_ms": latency_ms,
            "attempt": attempt,
            "status": status,
            "stop_reason": None,
            "error_class": error_class,
        }

    async def close(self) -> None:
        await self._client.close()
