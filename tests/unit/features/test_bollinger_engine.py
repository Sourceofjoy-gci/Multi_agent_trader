from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tests.unit.research.backtest.conftest import FakeBarReader
from trading_house.core.errors import InsufficientHistoryError
from trading_house.features.engine import FeatureEngine
from trading_house.marketdata.models import Bar, BarQuality, Timeframe

START = datetime(2026, 9, 7, 0, 0, tzinfo=UTC)
BIG = Decimal("0.0010")
TINY = Decimal("0.00001")


def _h1(closes: Sequence[Decimal], *, start: datetime = START) -> tuple[Bar, ...]:
    return tuple(
        Bar(
            instrument_id="fx.eurusd",
            timeframe=Timeframe.H1,
            event_time=start + timedelta(hours=index),
            availability_time=start + timedelta(hours=index + 1),
            open=close,
            high=close + Decimal("0.0001"),
            low=close - Decimal("0.0001"),
            close=close,
            tick_volume=100,
            spread=10,
            real_volume=0,
            quality=BarQuality.OK,
        )
        for index, close in enumerate(closes)
    )


def _alternating(count: int, amplitude: Decimal, *, offset: int = 0) -> list[Decimal]:
    """Closes alternating ``amplitude`` either side of 1.1. Index parity is taken
    from ``offset + i`` so two segments joined end to end keep alternating."""

    return [Decimal("1.1") + (amplitude if (offset + i) % 2 else -amplitude) for i in range(count)]


def _block(bars: tuple[Bar, ...], *, as_of: datetime | None = None):  # type: ignore[no-untyped-def]
    engine = FeatureEngine(FakeBarReader(bars))
    return engine.bollinger(
        "fx.eurusd",
        Timeframe.H1,
        as_of=as_of if as_of is not None else bars[199].availability_time,
    )


def test_the_block_has_the_hand_computed_values() -> None:
    """Every window alternates 0.0001 either side of 1.1001, so sigma is
    exactly 0.0001, the bands are exactly 1.1003 / 1.0999, and every bar's
    bandwidth is equal -- a tie, which counts as a squeeze, so the last bar
    is one and ``bars_since_squeeze`` is zero."""

    closes = [Decimal("1.1000"), Decimal("1.1002")] * 100
    block = _block(_h1(closes))

    assert block.middle == Decimal("1.1001")
    assert block.upper == Decimal("1.1003")
    assert block.lower == Decimal("1.0999")
    assert block.bandwidth == Decimal("0.0004") / Decimal("1.1001")
    assert block.previous_close == Decimal("1.1000")
    assert block.previous_upper == Decimal("1.1003")
    assert block.previous_lower == Decimal("1.0999")
    assert block.sma_200 == Decimal("1.1001")
    assert block.bars_since_squeeze == 0


def test_a_squeeze_ten_bars_back_is_counted() -> None:
    """Big swings, then a tiny-swing run whose last bar is index 189, then big
    swings again. Bar 189's 20-bar window (170..189) is all tiny, so it is the
    125-bar minimum; every later window holds big swings, so nothing after it
    is. 199 - 189 = 10, the edge of the recency window."""

    closes = _alternating(170, BIG) + _alternating(20, TINY, offset=170)
    closes += _alternating(10, BIG, offset=190)
    assert _block(_h1(closes)).bars_since_squeeze == 10


def test_a_squeeze_eleven_bars_back_is_not() -> None:
    """The same shape shifted one bar earlier: the tiny run ends at 188, so the
    latest squeeze is eleven bars back -- outside the window -- and bar 189's
    window already holds a big swing."""

    closes = _alternating(169, BIG) + _alternating(20, TINY, offset=169)
    closes += _alternating(11, BIG, offset=189)
    assert _block(_h1(closes)).bars_since_squeeze is None


def test_expanding_volatility_has_no_squeeze() -> None:
    closes = _alternating(170, TINY) + _alternating(30, BIG, offset=170)
    assert _block(_h1(closes)).bars_since_squeeze is None


def test_fewer_than_two_hundred_bars_is_insufficient_history() -> None:
    bars = _h1([Decimal("1.1000"), Decimal("1.1002")] * 100)[:199]
    engine = FeatureEngine(FakeBarReader(bars))
    with pytest.raises(InsufficientHistoryError):
        engine.bollinger("fx.eurusd", Timeframe.H1, as_of=bars[-1].availability_time)


def test_the_same_instant_gives_the_same_block_however_much_history_is_stored() -> None:
    """I-18: fifty extra older bars behind the same 200 change nothing."""

    closes = _alternating(170, BIG) + _alternating(20, TINY, offset=170)
    closes += _alternating(10, BIG, offset=190)
    shallow = _h1(closes)
    deep = _h1(_alternating(50, BIG) + closes, start=START - timedelta(hours=50))

    assert _block(shallow) == _block(deep, as_of=shallow[199].availability_time)


def test_a_bar_not_yet_available_cannot_move_the_block() -> None:
    """I-17: a wild bar knowable only after ``as_of`` is invisible."""

    closes = [Decimal("1.1000"), Decimal("1.1002")] * 100
    bars = _h1(closes)
    later = _h1([Decimal("1.5000")], start=START + timedelta(hours=200))

    assert _block(bars + later, as_of=bars[199].availability_time) == _block(bars)


def test_the_trend_mean_spans_two_hundred_bars() -> None:
    closes = [Decimal("1.0")] * 180 + [Decimal("1.1000"), Decimal("1.1002")] * 10
    assert _block(_h1(closes)).sma_200 == (Decimal("1.0") * 180 + Decimal("22.002")) / 200
