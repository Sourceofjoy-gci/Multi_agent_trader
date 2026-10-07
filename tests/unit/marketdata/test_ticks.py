from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import numpy as np
import pytest

from trading_house.core.errors import TimestampError
from trading_house.marketdata.ticks import (
    RawTicks,
    TickArrays,
    TickDataError,
    day_digest,
    epoch_ms,
    to_tick_arrays,
)

EURUSD_POINT = Decimal("0.00001")
GOLD_POINT = Decimal("0.01")
DAY = date(2026, 10, 6)


def _raw(
    times: list[int],
    bid: list[float],
    ask: list[float],
    *,
    last: float = 0.0,
    volume: int = 0,
    volume_real: float = 0.0,
    flags: int = 6,
) -> RawTicks:
    n = len(times)
    return RawTicks(
        time_ms=np.array(times, dtype=np.int64),
        bid=np.array(bid, dtype=np.float64),
        ask=np.array(ask, dtype=np.float64),
        last=np.full(n, last, dtype=np.float64),
        volume=np.full(n, volume, dtype=np.int64),
        volume_real=np.full(n, volume_real, dtype=np.float64),
        flags=np.full(n, flags, dtype=np.int64),
    )


def test_epoch_ms_is_integer_utc_milliseconds() -> None:
    assert epoch_ms(datetime(1970, 1, 1, 0, 0, 1, 500_000, tzinfo=UTC)) == 1500


def test_epoch_ms_refuses_a_naive_datetime() -> None:
    with pytest.raises(TimestampError):
        epoch_ms(datetime(2026, 10, 6))


def test_eurusd_floats_snap_exactly_to_points() -> None:
    """1.12427 / 0.00001 is 112426.99999999999 in binary; it is still 112427 points."""

    arrays = to_tick_arrays(_raw([1, 2], [1.12427, 1.12430], [1.12437, 1.12440]), EURUSD_POINT)
    assert arrays.bid.tolist() == [112427, 112430]
    assert arrays.ask.tolist() == [112437, 112440]
    assert arrays.bid.dtype == np.int64


def test_gold_floats_snap_exactly_to_points() -> None:
    arrays = to_tick_arrays(_raw([1], [2650.12], [2650.45]), GOLD_POINT)
    assert arrays.bid.tolist() == [265012]
    assert arrays.ask.tolist() == [265045]


def test_a_price_off_the_point_grid_is_refused() -> None:
    with pytest.raises(TickDataError, match="point grid"):
        to_tick_arrays(_raw([1], [1.124275], [1.12437]), EURUSD_POINT)


@pytest.mark.parametrize(
    "kwargs", [{"last": 1.1}, {"volume": 3}, {"volume_real": 0.5}], ids=["last", "volume", "real"]
)
def test_a_trade_print_is_refused(kwargs: dict[str, float]) -> None:
    with pytest.raises(TickDataError, match="trade print"):
        to_tick_arrays(_raw([1], [1.1], [1.1001], **kwargs), EURUSD_POINT)  # type: ignore[arg-type]


def test_time_going_backwards_is_refused() -> None:
    with pytest.raises(TickDataError, match="backwards"):
        to_tick_arrays(_raw([5, 4], [1.1, 1.1], [1.1001, 1.1001]), EURUSD_POINT)


def test_ticks_sharing_a_millisecond_keep_the_broker_order() -> None:
    arrays = to_tick_arrays(_raw([5, 5], [1.10000, 1.10003], [1.1001, 1.1001]), EURUSD_POINT)
    assert arrays.bid.tolist() == [110000, 110003]


def test_a_non_positive_price_is_refused() -> None:
    with pytest.raises(TickDataError, match="not positive"):
        to_tick_arrays(_raw([1], [0.0], [1.1]), EURUSD_POINT)


def test_crossed_quotes_are_counted_not_dropped() -> None:
    arrays = to_tick_arrays(_raw([1, 2], [1.10010, 1.1], [1.10000, 1.1001]), EURUSD_POINT)
    assert len(arrays) == 2
    assert arrays.crossed_quotes() == 1


def test_an_empty_batch_is_an_empty_array_set() -> None:
    arrays = to_tick_arrays(RawTicks.empty(), EURUSD_POINT)
    assert len(arrays) == 0
    assert arrays.flags.dtype == np.uint16


def test_within_is_half_open() -> None:
    raw = _raw([10, 20, 30], [1.1] * 3, [1.1001] * 3)
    assert raw.within(10, 30).time_ms.tolist() == [10, 20]


def _arrays() -> TickArrays:
    return to_tick_arrays(_raw([1, 2], [1.1, 1.10001], [1.1001, 1.10011]), EURUSD_POINT)


def test_the_day_digest_is_stable_hex() -> None:
    first = day_digest("fx.eurusd", DAY, EURUSD_POINT, _arrays())
    assert first == day_digest("fx.eurusd", DAY, EURUSD_POINT, _arrays())
    assert len(first) == 64
    assert int(first, 16) >= 0


def test_the_day_digest_names_instrument_day_and_point_size() -> None:
    base = day_digest("fx.eurusd", DAY, EURUSD_POINT, _arrays())
    assert day_digest("metal.xauusd", DAY, EURUSD_POINT, _arrays()) != base
    assert day_digest("fx.eurusd", DAY + timedelta(days=1), EURUSD_POINT, _arrays()) != base
    assert day_digest("fx.eurusd", DAY, Decimal("0.0001"), _arrays()) != base


def test_the_day_digest_covers_every_array() -> None:
    base = _arrays()
    for field in ("time_ms", "bid", "ask", "flags"):
        changed = {name: getattr(base, name).copy() for name in ("time_ms", "bid", "ask", "flags")}
        changed[field][0] += 1
        assert day_digest("fx.eurusd", DAY, EURUSD_POINT, TickArrays(**changed)) != day_digest(
            "fx.eurusd", DAY, EURUSD_POINT, base
        ), field


def test_the_point_size_spelling_does_not_change_the_digest() -> None:
    assert day_digest("fx.eurusd", DAY, Decimal("0.000010"), _arrays()) == day_digest(
        "fx.eurusd", DAY, Decimal("1E-5"), _arrays()
    )
