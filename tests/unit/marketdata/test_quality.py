from datetime import UTC, datetime
from decimal import Decimal

import pytest

from trading_house.marketdata.models import BarQuality, Timeframe
from trading_house.marketdata.quality import assess

ALIGNED = datetime(2026, 8, 25, 9, 0, tzinfo=UTC)
OFFSET = 10800


def _assess(**overrides: object) -> BarQuality:
    kwargs: dict[str, object] = {
        "bar_open": Decimal("1.10000"),
        "high": Decimal("1.10050"),
        "low": Decimal("1.09950"),
        "close": Decimal("1.10020"),
        "spread": 9,
        "timeframe": Timeframe.M1,
        "event_time": ALIGNED,
        "server_offset_seconds": OFFSET,
    }
    kwargs.update(overrides)
    return assess(**kwargs)  # type: ignore[arg-type]


def test_a_normal_bar_passes() -> None:
    assert _assess() is BarQuality.OK


def test_a_high_below_the_body_is_impossible() -> None:
    assert _assess(high=Decimal("1.09000")) is BarQuality.OHLC_INCOHERENT


def test_a_low_above_the_body_is_impossible() -> None:
    assert _assess(low=Decimal("1.20000")) is BarQuality.OHLC_INCOHERENT


def test_a_flat_bar_is_coherent() -> None:
    """All four prices equal is a real, if quiet, minute -- not a defect."""

    flat = Decimal("1.10000")

    assert _assess(bar_open=flat, high=flat, low=flat, close=flat) is BarQuality.OK


@pytest.mark.parametrize("field", ["bar_open", "high", "low", "close"])
def test_a_non_positive_price_is_impossible(field: str) -> None:
    assert _assess(**{field: Decimal("0")}) is BarQuality.NON_POSITIVE_PRICE


def test_a_negative_spread_is_impossible() -> None:
    assert _assess(spread=-1) is BarQuality.NEGATIVE_SPREAD


def test_a_zero_spread_is_allowed() -> None:
    assert _assess(spread=0) is BarQuality.OK


def test_a_bar_off_its_timeframe_boundary_is_a_conversion_bug() -> None:
    """This gate is the tripwire for a wrong server-clock offset."""

    assert (
        _assess(event_time=datetime(2026, 8, 25, 9, 0, 37, tzinfo=UTC))
        is BarQuality.MISALIGNED_TIMESTAMP
    )


def test_price_defects_are_reported_before_alignment() -> None:
    """A bar with two defects reports one verdict. Ordering must be fixed, or
    the same bar classifies differently depending on evaluation order."""

    verdict = _assess(low=Decimal("0"), event_time=datetime(2026, 8, 25, 9, 0, 37, tzinfo=UTC))

    assert verdict is BarQuality.NON_POSITIVE_PRICE
