"""Cost calculation against effective-dated rates.

Rates live in Postgres (``triage.model_pricing``), not in Python constants. That
is not ceremony — it is the difference between a dashboard that stays correct
across a price change and one that silently starts lying.

The immediate case: Claude Sonnet 5 runs at introductory pricing of $2/$10 per
MTok through 2026-08-31, then $3/$15 from 2026-09-01. Both windows are already
seeded. On September 1st the cache reloads, new calls price at the new rate, and
every historical row keeps the rate it was actually billed at. No deploy, no
backfill.

Three rules encoded here:

1. **``Decimal``, never ``float``.** Per-call costs are fractions of a cent and
   float error compounds across millions of rows.
2. **Cache-read and cache-write tokens are priced separately.** A cache hit is
   0.1x base input; treating them as ordinary input tokens overstates spend by
   an order of magnitude on cached workloads.
3. **The pricing row used is returned alongside the cost**, so the caller can
   store ``pricing_valid_from`` and satisfy the composite FK on
   ``llm_invocations``. Every cost figure stays traceable to a dated rate.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal

from triage.db.repository import PriceRow, PricingRepository
from triage.logging_setup import get_logger

log = get_logger(__name__)

_MILLION = Decimal(1_000_000)

# llm_invocations.cost_usd is NUMERIC(14, 8). Quantizing here rather than
# letting Postgres round means the value we log equals the value we store.
_COST_PRECISION = Decimal("0.00000001")


@dataclass(frozen=True, slots=True)
class CostBreakdown:
    """Priced usage for a single API call."""

    cost_usd: Decimal
    pricing_valid_from: date
    input_cost: Decimal
    output_cost: Decimal
    cache_read_cost: Decimal
    cache_write_cost: Decimal


class PricingCache:
    """In-memory view of ``model_pricing``, refreshed at startup.

    Pricing changes on the order of months, so an in-process cache is correct
    and saves a database round trip on the hot path. Long-running processes
    should call :meth:`refresh` periodically — a deploy that spans a price
    boundary would otherwise price at the stale rate.
    """

    def __init__(self) -> None:
        self._rows: list[PriceRow] = []

    async def refresh(self) -> None:
        """Reload all pricing windows from the database."""
        self._rows = await PricingRepository.load_all()
        log.info(
            "pricing_cache_loaded",
            rows=len(self._rows),
            models=sorted({r.model for r in self._rows}),
        )

    def _resolve(self, model: str, at: date) -> PriceRow:
        """Return the pricing window covering ``at`` for ``model``.

        Rows are ordered by ``valid_from``; the last window that has already
        started is the applicable one. The database's EXCLUDE constraint
        guarantees non-overlap, so this cannot be ambiguous.
        """
        candidates = [
            r for r in self._rows if r.model == model and r.valid_from <= at
        ]
        if not candidates:
            # Fail loudly. Guessing a price here would put a fabricated number
            # into a table whose entire purpose is auditability — and the
            # composite FK on llm_invocations would reject the write anyway.
            raise ValueError(
                f"No pricing row for model {model!r} effective {at.isoformat()}. "
                "Add it to db/seed/model_pricing.sql and re-run `make seed`."
            )
        return max(candidates, key=lambda r: r.valid_from)

    def price(
        self,
        *,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        at: datetime | None = None,
    ) -> CostBreakdown:
        """Price one API call.

        Args:
            model: Model ID as sent to the API, e.g. ``claude-haiku-4-5-20251001``.
            input_tokens: Uncached input tokens (``usage.input_tokens``).
            output_tokens: Generated tokens.
            cache_read_tokens: ``usage.cache_read_input_tokens``, billed at 0.1x.
            cache_write_tokens: ``usage.cache_creation_input_tokens``. Priced at
                the 5-minute write rate, which is what the default TTL uses.
            at: Billing instant; defaults to now (UTC).

        Returns:
            A :class:`CostBreakdown` with the total and its components.
        """
        moment = (at or datetime.now(timezone.utc)).date()
        row = self._resolve(model, moment)

        def component(tokens: int, rate: Decimal) -> Decimal:
            return (Decimal(tokens) / _MILLION) * rate

        input_cost = component(input_tokens, row.input_usd_per_mtok)
        output_cost = component(output_tokens, row.output_usd_per_mtok)
        cache_read_cost = component(cache_read_tokens, row.cache_read_usd_per_mtok)
        cache_write_cost = component(
            cache_write_tokens, row.cache_write_5m_usd_per_mtok
        )

        total = (
            input_cost + output_cost + cache_read_cost + cache_write_cost
        ).quantize(_COST_PRECISION, rounding=ROUND_HALF_UP)

        return CostBreakdown(
            cost_usd=total,
            pricing_valid_from=row.valid_from,
            input_cost=input_cost,
            output_cost=output_cost,
            cache_read_cost=cache_read_cost,
            cache_write_cost=cache_write_cost,
        )

    def pricing_valid_from(self, model: str, at: datetime | None = None) -> date:
        """Resolve just the effective date, for rows priced at zero (errors)."""
        moment = (at or datetime.now(timezone.utc)).date()
        return self._resolve(model, moment).valid_from


# Process-wide instance, populated during startup.
pricing_cache = PricingCache()
