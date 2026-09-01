from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from trading_house.marketdata.models import (
    Bar,
    BarQuality,
    Timeframe,
    availability_of,
    duration,
    is_aligned,
)


def _bar(**overrides: object) -> Bar:
    kwargs: dict[str, object] = {
        "instrument_id": "fx.eurusd",
        "timeframe": Timeframe.M1,
        "event_time": datetime(2026, 8, 25, 9, 0, tzinfo=UTC),
        "availability_time": datetime(2026, 8, 25, 9, 1, tzinfo=UTC),
        "open": Decimal("1.10000"),
        "high": Decimal("1.10050"),
        "low": Decimal("1.09950"),
        "close": Decimal("1.10020"),
        "tick_volume": 42,
        "spread": 9,
        "real_volume": 0,
        "quality": BarQuality.OK,
    }
    kwargs.update(overrides)
    return Bar(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("timeframe", "seconds"),
    [
        (Timeframe.M1, 60),
        (Timeframe.M5, 300),
        (Timeframe.M15, 900),
        (Timeframe.H1, 3600),
        (Timeframe.H4, 14400),
        (Timeframe.D1, 86400),
    ],
)
def test_every_timeframe_knows_its_duration(timeframe: Timeframe, seconds: int) -> None:
    assert duration(timeframe) == timedelta(seconds=seconds)


def test_a_bar_is_available_only_once_it_has_closed() -> None:
    """The look-ahead guard. A 09:00 M1 bar is not knowable until 09:01."""

    opened = datetime(2026, 8, 25, 9, 0, tzinfo=UTC)

    assert availability_of(Timeframe.M1, opened) == datetime(2026, 8, 25, 9, 1, tzinfo=UTC)
    assert availability_of(Timeframe.H4, opened) == datetime(2026, 8, 25, 13, 0, tzinfo=UTC)


def test_alignment_is_judged_in_the_brokers_frame_not_utc() -> None:
    """FBS runs UTC+3, so its H4 bars open at 21:00, 01:00, 05:00 UTC. A naive
    modulo against UTC would reject every one of them."""

    h4_open_utc = datetime(2026, 8, 25, 21, 0, tzinfo=UTC)

    assert is_aligned(Timeframe.H4, h4_open_utc, server_offset_seconds=10800)
    assert not is_aligned(Timeframe.H4, h4_open_utc, server_offset_seconds=0)


def test_a_misaligned_bar_is_detected_at_any_offset() -> None:
    stray = datetime(2026, 8, 25, 9, 0, 37, tzinfo=UTC)

    assert not is_aligned(Timeframe.M1, stray, server_offset_seconds=10800)


def test_a_naive_timestamp_is_refused() -> None:
    with pytest.raises(ValidationError):
        _bar(event_time=datetime(2026, 8, 25, 9, 0))


def test_a_clean_bar_must_have_positive_prices() -> None:
    """Defective bars are stored (D-2), so the model cannot demand gt=0
    outright -- but a bar claiming to be clean and carrying a zero price is
    incoherent, and that is worth refusing at construction."""

    with pytest.raises(ValidationError):
        _bar(low=Decimal("0"))


def test_a_defective_bar_may_carry_the_impossible_value_it_was_sent() -> None:
    bar = _bar(low=Decimal("0"), quality=BarQuality.NON_POSITIVE_PRICE)

    assert bar.low == Decimal("0")


def test_bars_are_frozen() -> None:
    with pytest.raises(ValidationError):
        _bar().open = Decimal("2")  # type: ignore[misc]
