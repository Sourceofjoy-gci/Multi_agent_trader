"""The composition shim's one load-bearing property: the shared clock.

``tests/unit/research/backtest/`` wires its own ``ReplayClock`` into both the
``RiskEngine`` and the ``Backtester`` by hand, so every engine test there is
blind to how ``backtest run`` actually wires them. This module drives the real
composition function over the same synthetic ramp, so a ``SystemClock`` reaching
the risk engine fails here rather than shipping as a run with no trades.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from tests.unit.research.backtest.conftest import (
    ATR_PERIOD,
    SPREAD_WINDOW,
    FakeBarReader,
    _constitution,
    _contract,
    _cost_model,
    _ramp,
)
from trading_house.core.errors import ConfigurationError
from trading_house.core.schemas import Side
from trading_house.marketdata.models import Timeframe, duration
from trading_house.ops.backtest import (
    TOY_STRATEGY_ID,
    NeverBindingMargin,
    build_backtester,
    build_strategy,
)
from trading_house.research.backtest.engine import BacktestRequest
from trading_house.research.backtest.result import BacktestResult
from trading_house.research.backtest.snapshot import MIN_HORIZON_BARS
from trading_house.risk.engine import MARGIN_HEADROOM_MULTIPLE

BARS = 60
EVERY_N = 20


def _composed_run() -> BacktestResult:
    bars = _ramp(BARS)
    tester = build_backtester(
        bars=FakeBarReader(bars), contract=_contract(), constitution=_constitution()
    )
    return tester.run(
        BacktestRequest(
            strategy=build_strategy(TOY_STRATEGY_ID, every_n=EVERY_N, timeframe=Timeframe.M1),
            instrument_id="fx.eurusd",
            timeframe=Timeframe.M1,
            start=bars[0].event_time,
            end=bars[-1].event_time,
            firm_equity=Decimal("100000"),
            cost_model=_cost_model(),
            atr_period=ATR_PERIOD,
            spread_window=SPREAD_WINDOW,
        )
    )


def test_the_composed_backtester_shares_its_clock_with_its_risk_engine() -> None:
    """The single most likely error in this task, made loud.

    ``RiskEngine``'s tick-freshness gate reads its OWN clock and compares it to
    the snapshot's ``tick_time``; only a clock advanced to each bar's
    ``availability_time`` passes. Give the engine a ``SystemClock`` while the
    ``Backtester`` keeps the ``ReplayClock`` and nothing raises: every proposal
    is rejected ``tick_stale``, the run reports no trades, and the result is
    indistinguishable from a strategy that proposed nothing.

    So this asserts what such a run cannot produce -- trades -- rather than the
    clock's identity, which a future wiring could satisfy while still handing
    the engine a clock nobody advances. The rejection assertion is what names
    the cause when it does fail.
    """

    result = _composed_run()

    assert result.rejections == ()
    assert len(result.trades) == 2
    assert result.bars_seen == BARS


def test_the_toy_is_the_only_registered_strategy() -> None:
    """``--strategy`` names a registry with exactly one entry in Phase 6. An
    unknown id must be a typed configuration failure, not a run of the toy
    under someone else's name."""

    with pytest.raises(ConfigurationError):
        build_strategy("momentum", every_n=1, timeframe=Timeframe.M1)


@pytest.mark.parametrize("timeframe", list(Timeframe))
def test_the_toys_horizon_is_simulatable_on_every_timeframe(timeframe: Timeframe) -> None:
    """D-1 refuses a horizon shorter than ten bars. A toy stating its horizon in
    fixed seconds would be refused outright on the slow timeframes, which reads
    as the simulator rejecting the timeframe rather than the toy."""

    strategy = build_strategy(TOY_STRATEGY_ID, every_n=1, timeframe=timeframe)

    assert strategy.horizon_seconds >= MIN_HORIZON_BARS * duration(timeframe).total_seconds()


def test_the_simulated_margin_port_never_binds() -> None:
    """It is not an account: it is the headroom gate switched off, and the one
    thing it must never do is reject. ``required_margin`` must also stay
    strictly positive -- the engine rejects a non-positive requirement
    outright, which would turn "not modelled" into "every proposal refused"."""

    margin = NeverBindingMargin()
    required = margin.required_margin(
        instrument_id="fx.eurusd",
        side=Side.BUY,
        quantity=Decimal("1000000"),
        price=Decimal("1.10000"),
    )

    assert required > 0
    assert margin.free_margin() >= required * MARGIN_HEADROOM_MULTIPLE
