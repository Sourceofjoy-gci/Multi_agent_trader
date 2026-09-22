"""The point-in-time read a strategy evaluates against.

Everything a ``Strategy`` sees during a backtest passes through
``FeatureSnapshot``. It carries the bar that just closed plus the features
computed from it, and nothing else -- so a strategy structurally cannot see
past ``as_of`` (I-17).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Self

from pydantic import model_validator

from trading_house.core.values import CanonicalModel, InstrumentId
from trading_house.marketdata.models import Bar, Timeframe, duration

MIN_HORIZON_BARS: int = 10
"""D-1. Ten bars is a judgement, not a derivation -- below roughly that, most
trades open and close inside a couple of bars, so D-2's stop-first pessimism
dominates and the number being measured is the simulator's convention rather
than the strategy. A future phase that ingests ticks may lower this."""


class FeatureSnapshot(CanonicalModel):
    """A point-in-time read: the bar that just closed, plus its features.

    ``tick_spread_points`` and ``tick_time`` are named for a tick stream this
    phase does not have. Their meaning is pinned to the closing bar's own
    spread and ``availability_time`` -- a constructed invariant rather than a
    convention left to whichever caller builds a snapshot next, so the risk
    engine's spread-blowout gate always judges the same bar the strategy saw.
    """

    as_of: datetime
    instrument_id: InstrumentId
    timeframe: Timeframe
    bar: Bar
    atr: Decimal
    median_spread_points: Decimal
    tick_spread_points: Decimal
    tick_time: datetime

    @model_validator(mode="after")
    def tick_fields_match_the_closing_bar(self) -> Self:
        if self.tick_spread_points != Decimal(self.bar.spread):
            raise ValueError("tick_spread_points must equal the closing bar's spread")
        if self.tick_time != self.bar.availability_time:
            raise ValueError("tick_time must equal the closing bar's availability_time")
        if self.tick_time != self.as_of:
            raise ValueError("tick_time must equal as_of")
        return self


def horizon_is_simulatable(*, horizon_seconds: int, timeframe: Timeframe) -> bool:
    """D-1. Ten bars is a judgement, not a derivation -- below roughly that,
    most trades open and close inside a couple of bars, so D-2's stop-first
    pessimism dominates and the number being measured is the simulator's
    convention rather than the strategy."""

    bar_seconds = duration(timeframe).total_seconds()
    return horizon_seconds >= MIN_HORIZON_BARS * bar_seconds
