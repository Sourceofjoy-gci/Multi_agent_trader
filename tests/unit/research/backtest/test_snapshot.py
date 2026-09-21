from datetime import UTC, datetime
from decimal import Decimal

import pytest

from trading_house.marketdata.models import Bar, BarQuality, Timeframe, duration
from trading_house.research.backtest.snapshot import (
    MIN_HORIZON_BARS,
    FeatureSnapshot,
    horizon_is_simulatable,
)

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)


def _snapshot(
    *,
    spread: int,
    tick_spread_points: Decimal | None = None,
    tick_time: datetime | None = None,
) -> FeatureSnapshot:
    timeframe = Timeframe.M1
    bar = Bar(
        instrument_id="fx.eurusd",
        timeframe=timeframe,
        event_time=NOW - duration(timeframe),
        availability_time=NOW,
        open=Decimal("1.10000"),
        high=Decimal("1.10050"),
        low=Decimal("1.09950"),
        close=Decimal("1.10020"),
        tick_volume=100,
        spread=spread,
        real_volume=0,
        quality=BarQuality.OK,
    )
    return FeatureSnapshot(
        as_of=NOW,
        instrument_id="fx.eurusd",
        timeframe=timeframe,
        bar=bar,
        atr=Decimal("0.00050"),
        median_spread_points=Decimal(spread),
        tick_spread_points=(
            tick_spread_points if tick_spread_points is not None else Decimal(spread)
        ),
        tick_time=tick_time if tick_time is not None else bar.availability_time,
    )


def test_the_snapshot_ties_its_tick_fields_to_the_bar_that_closed() -> None:
    """tick_spread_points and tick_time are named for a tick stream this phase
    does not have. Their meaning is the closing bar's own spread and
    availability_time -- stated as a constructed invariant rather than left to
    whichever caller builds a snapshot next."""

    snapshot = _snapshot(spread=17)

    assert snapshot.tick_spread_points == Decimal(17)
    assert snapshot.tick_time == snapshot.bar.availability_time
    assert snapshot.tick_time == snapshot.as_of


def test_a_snapshot_whose_tick_fields_disagree_with_its_bar_is_refused() -> None:
    """Without this the two representations drift, and the risk engine's
    spread-blowout gate silently judges a different bar than the strategy saw."""

    with pytest.raises(ValueError, match="tick_spread_points"):
        _snapshot(spread=17, tick_spread_points=Decimal(3))


@pytest.mark.parametrize(
    ("horizon_seconds", "timeframe", "expected"),
    [
        (600, Timeframe.M1, True),  # 10 bars exactly -- the boundary is allowed
        (599, Timeframe.M1, False),  # one second under it
        (30, Timeframe.M1, False),  # the 30-second scalp D-1 exists to refuse
        (36_000, Timeframe.H1, True),
    ],
)
def test_a_horizon_under_ten_bars_is_not_simulatable(
    horizon_seconds: int, timeframe: Timeframe, expected: bool
) -> None:
    """D-1. Below roughly ten bars most trades open and close inside a couple of
    bars, so D-2's stop-first pessimism dominates and the number being measured
    is the simulator's convention rather than the strategy."""

    assert horizon_is_simulatable(horizon_seconds=horizon_seconds, timeframe=timeframe) is expected


def test_the_threshold_is_a_named_constant_not_a_literal() -> None:
    """A future phase that ingests ticks lowers this. A literal buried in a
    comparison is one nobody finds."""

    assert MIN_HORIZON_BARS == 10
