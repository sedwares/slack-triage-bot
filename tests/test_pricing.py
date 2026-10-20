"""Cost calculation tests.

These run against an in-memory pricing cache rather than the database, so they
are fast and deterministic. The rows mirror db/seed/model_pricing.sql.

The test that matters most is the Sonnet 5 transition: the same call priced on
2026-08-15 and 2026-09-15 must produce different numbers. That is the behaviour
that keeps historical cost reporting honest across a price change, and it is
exactly the thing that would break silently if rates were Python constants.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from triage.db.repository import PriceRow
from triage.llm.pricing import PricingCache

# Verified against Anthropic's published pricing on 2026-08-03.
SEED_ROWS = [
    PriceRow(
        model="claude-haiku-4-5-20251001",
        valid_from=date(2025, 10, 1),
        input_usd_per_mtok=Decimal("1.0000"),
        output_usd_per_mtok=Decimal("5.0000"),
        cache_read_usd_per_mtok=Decimal("0.1000"),
        cache_write_5m_usd_per_mtok=Decimal("1.2500"),
        cache_write_1h_usd_per_mtok=Decimal("2.0000"),
    ),
    PriceRow(
        model="claude-sonnet-5",
        valid_from=date(2026, 6, 30),
        input_usd_per_mtok=Decimal("2.0000"),
        output_usd_per_mtok=Decimal("10.0000"),
        cache_read_usd_per_mtok=Decimal("0.2000"),
        cache_write_5m_usd_per_mtok=Decimal("2.5000"),
        cache_write_1h_usd_per_mtok=Decimal("4.0000"),
    ),
    PriceRow(
        model="claude-sonnet-5",
        valid_from=date(2026, 9, 1),
        input_usd_per_mtok=Decimal("3.0000"),
        output_usd_per_mtok=Decimal("15.0000"),
        cache_read_usd_per_mtok=Decimal("0.3000"),
        cache_write_5m_usd_per_mtok=Decimal("3.7500"),
        cache_write_1h_usd_per_mtok=Decimal("6.0000"),
    ),
]


@pytest.fixture()
def cache() -> PricingCache:
    c = PricingCache()
    c._rows = SEED_ROWS  # noqa: SLF001 — bypassing the DB round trip on purpose
    return c


def at(y: int, m: int, d: int) -> datetime:
    return datetime(y, m, d, 12, 0, tzinfo=timezone.utc)


def test_haiku_basic_cost(cache):
    """1M input + 1M output on Haiku = $1 + $5."""
    result = cache.price(
        model="claude-haiku-4-5-20251001",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        at=at(2026, 8, 3),
    )
    assert result.cost_usd == Decimal("6.00000000")


def test_realistic_classification_cost(cache):
    """A representative Haiku classification: ~700 in, ~80 out."""
    result = cache.price(
        model="claude-haiku-4-5-20251001",
        input_tokens=700,
        output_tokens=80,
        at=at(2026, 8, 3),
    )
    # 700/1e6 * 1.00 + 80/1e6 * 5.00 = 0.0007 + 0.0004
    assert result.cost_usd == Decimal("0.00110000")


def test_sonnet_uses_introductory_rate_before_september(cache):
    result = cache.price(
        model="claude-sonnet-5",
        input_tokens=1_000_000,
        output_tokens=0,
        at=at(2026, 8, 15),
    )
    assert result.cost_usd == Decimal("2.00000000")
    assert result.pricing_valid_from == date(2026, 6, 30)


def test_sonnet_uses_standard_rate_from_september(cache):
    """The transition that makes effective-dated pricing worth the complexity."""
    result = cache.price(
        model="claude-sonnet-5",
        input_tokens=1_000_000,
        output_tokens=0,
        at=at(2026, 9, 15),
    )
    assert result.cost_usd == Decimal("3.00000000")
    assert result.pricing_valid_from == date(2026, 9, 1)


def test_transition_boundary_is_exact(cache):
    """August 31 is introductory; September 1 is standard. No off-by-one."""
    assert cache.price(
        model="claude-sonnet-5", input_tokens=1_000_000, output_tokens=0,
        at=at(2026, 8, 31),
    ).pricing_valid_from == date(2026, 6, 30)

    assert cache.price(
        model="claude-sonnet-5", input_tokens=1_000_000, output_tokens=0,
        at=at(2026, 9, 1),
    ).pricing_valid_from == date(2026, 9, 1)


def test_cache_reads_priced_at_one_tenth(cache):
    """Billing cache reads as ordinary input would overstate cost 10x."""
    cached = cache.price(
        model="claude-haiku-4-5-20251001",
        input_tokens=0,
        output_tokens=0,
        cache_read_tokens=1_000_000,
        at=at(2026, 8, 3),
    )
    uncached = cache.price(
        model="claude-haiku-4-5-20251001",
        input_tokens=1_000_000,
        output_tokens=0,
        at=at(2026, 8, 3),
    )
    assert cached.cost_usd == Decimal("0.10000000")
    assert cached.cost_usd * 10 == uncached.cost_usd


def test_unknown_model_raises_rather_than_guessing(cache):
    """A fabricated price in an audit table is worse than a loud failure."""
    with pytest.raises(ValueError, match="No pricing row"):
        cache.price(
            model="claude-nonexistent-9",
            input_tokens=100,
            output_tokens=10,
            at=at(2026, 8, 3),
        )


def test_date_before_any_pricing_window_raises(cache):
    with pytest.raises(ValueError, match="No pricing row"):
        cache.price(
            model="claude-sonnet-5",
            input_tokens=100,
            output_tokens=10,
            at=at(2020, 1, 1),
        )


def test_cost_is_decimal_not_float(cache):
    """Float accumulation across millions of rows drifts. Decimal does not."""
    result = cache.price(
        model="claude-haiku-4-5-20251001",
        input_tokens=333,
        output_tokens=77,
        at=at(2026, 8, 3),
    )
    assert isinstance(result.cost_usd, Decimal)
    # Quantized to NUMERIC(14, 8), matching the column definition.
    assert result.cost_usd.as_tuple().exponent == -8
