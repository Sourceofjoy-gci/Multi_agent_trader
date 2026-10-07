from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

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


@pytest.mark.parametrize("quality", [BarQuality.OK, BarQuality.NON_POSITIVE_PRICE])
def test_a_bar_is_available_exactly_when_its_timeframe_closes(quality: BarQuality) -> None:
    opened = datetime(2026, 8, 25, 9, 0, tzinfo=UTC)

    with pytest.raises(ValidationError, match="availability_time"):
        _bar(availability_time=opened + timedelta(minutes=2), quality=quality)


def test_a_bar_cannot_be_available_before_its_timeframe_closes() -> None:
    opened = datetime(2026, 8, 25, 9, 0, tzinfo=UTC)

    with pytest.raises(ValidationError, match="availability_time"):
        _bar(event_time=opened, availability_time=opened - timedelta(seconds=1))


def test_alignment_is_judged_in_the_brokers_frame_not_utc() -> None:
    """FBS runs UTC+3, so its H4 bars open at 21:00, 01:00, 05:00 UTC. A naive
    modulo against UTC would reject every one of them."""

    h4_open_utc = datetime(2026, 8, 25, 21, 0, tzinfo=UTC)

    assert is_aligned(Timeframe.H4, h4_open_utc, server_zone=timezone(timedelta(hours=3)))
    assert not is_aligned(Timeframe.H4, h4_open_utc, server_zone=UTC)


def test_alignment_is_judged_at_the_bars_own_seasons_offset() -> None:
    """An EU-rules broker is UTC+2 in January: its H4 bars open at 22:00 UTC
    then, and at 21:00 UTC in August. Judging a winter bar at today's summer
    offset would grade every winter H4 and D1 bar misaligned."""

    athens = ZoneInfo("Europe/Athens")

    assert is_aligned(Timeframe.H4, datetime(2026, 1, 14, 22, 0, tzinfo=UTC), server_zone=athens)
    assert is_aligned(Timeframe.H4, datetime(2026, 8, 25, 21, 0, tzinfo=UTC), server_zone=athens)
    assert not is_aligned(
        Timeframe.H4, datetime(2026, 1, 14, 21, 0, tzinfo=UTC), server_zone=athens
    )


def test_a_misaligned_bar_is_detected_at_any_offset() -> None:
    stray = datetime(2026, 8, 25, 9, 0, 37, tzinfo=UTC)

    assert not is_aligned(Timeframe.M1, stray, server_zone=timezone(timedelta(hours=3)))


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
