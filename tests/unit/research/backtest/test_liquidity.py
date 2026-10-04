"""Phase 12: each fill's bar volume, read from the bars the run replayed."""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from tests.unit.research.backtest.conftest import ToyStrategy, _contract, _outcome, _ramp
from trading_house.core.errors import CoverageError
from trading_house.research.backtest.liquidity import (
    Liquidity,
    TradeLiquidity,
    trade_liquidity,
)


def test_every_fill_reads_its_own_bars_volume_in_trade_order() -> None:
    bars = tuple(
        bar.model_copy(update={"tick_volume": 1000 + index}) for index, bar in enumerate(_ramp(60))
    )
    trades = _outcome(bars=bars, strategy=ToyStrategy(every_n=20)).result.trades
    assert trades
    volume = {bar.event_time: bar.tick_volume for bar in bars}

    liquidity = trade_liquidity(trades, bars, _contract())

    assert [t.proposal_id for t in liquidity.trades] == [t.proposal_id for t in trades]
    for trade, measured in zip(trades, liquidity.trades, strict=True):
        assert measured.entry_tick_volume == volume[trade.entry_at]
        assert measured.exit_tick_volume == volume[trade.exit_at]


def test_one_point_per_lot_is_the_contracts_own_value() -> None:
    """$1 per 0.00001 increment, and a point of 0.0001: $10 a point per lot."""

    bars = _ramp(60)
    trades = _outcome(bars=bars, strategy=ToyStrategy(every_n=20)).result.trades
    contract = _contract(point_size=Decimal("0.0001"))

    liquidity = trade_liquidity(trades, bars, contract)

    assert {t.money_per_point_per_lot for t in liquidity.trades} == {Decimal(10)}


def test_a_fill_on_a_bar_not_handed_in_is_refused_not_guessed() -> None:
    bars = _ramp(60)
    trades = _outcome(bars=bars, strategy=ToyStrategy(every_n=20)).result.trades
    missing = trades[0].exit_at

    with pytest.raises(CoverageError):
        trade_liquidity(trades, tuple(b for b in bars if b.event_time != missing), _contract())


def test_a_trade_is_recorded_once() -> None:
    once = TradeLiquidity(
        proposal_id="p",
        entry_tick_volume=1,
        exit_tick_volume=1,
        money_per_point_per_lot=Decimal(1),
    )

    with pytest.raises(ValidationError, match="recorded once"):
        Liquidity(trades=(once, once))
