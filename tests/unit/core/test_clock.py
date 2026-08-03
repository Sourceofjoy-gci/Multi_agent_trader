from datetime import UTC, datetime, timedelta, timezone

import pytest

from trading_house.core.clock import FixedClock, SystemClock, ensure_utc
from trading_house.core.errors import TimestampError


def test_ensure_utc_rejects_naive_datetime() -> None:
    with pytest.raises(TimestampError, match="timezone-aware"):
        ensure_utc(datetime(2026, 8, 3, 12, 0))


def test_ensure_utc_normalizes_an_aware_offset() -> None:
    value = datetime(2026, 8, 3, 14, 0, tzinfo=timezone(timedelta(hours=2)))

    assert ensure_utc(value) == datetime(2026, 8, 3, 12, 0, tzinfo=UTC)


def test_fixed_clock_is_deterministic() -> None:
    instant = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)

    assert FixedClock(instant).now() == instant


def test_system_clock_returns_utc() -> None:
    assert SystemClock().now().tzinfo is UTC
