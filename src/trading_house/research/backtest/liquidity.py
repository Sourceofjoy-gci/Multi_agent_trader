"""What the market offered at each fill: the input the capacity model reads.

Phase 12. A capacity claim needs, for every fill, how much the market traded on the
bar the fill happened on, and what one price point is worth per lot. Neither is in
``SimulatedTrade``, and putting them there would move every result digest, so they
ride beside the trades -- the same sidecar pattern as the cost attribution -- and only
when the run's protocol declared a capacity model to read them.

A fill is stamped at its bar's ``event_time`` (Phase 7, D-9), so the bar is found by
that instant among the bars the replay read. ``tick_volume`` is MetaTrader 5's count of
price updates, not lots traded: the conversion to lots is the protocol's declared
assumption (``CapacitySpec.lots_per_tick``), never this module's.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Self

from pydantic import NonNegativeInt, model_validator

from trading_house.core.errors import CoverageError
from trading_house.core.instruments import InstrumentContract
from trading_house.core.values import CanonicalModel, NonEmptyStr, PositiveDecimal
from trading_house.marketdata.models import Bar
from trading_house.research.backtest.result import SimulatedTrade


class TradeLiquidity(CanonicalModel):
    proposal_id: NonEmptyStr
    entry_tick_volume: NonNegativeInt
    exit_tick_volume: NonNegativeInt
    money_per_point_per_lot: PositiveDecimal


class Liquidity(CanonicalModel):
    """One record per trade, in the trades' order."""

    trades: tuple[TradeLiquidity, ...]

    @model_validator(mode="after")
    def proposal_ids_are_unique(self) -> Self:
        ids = [trade.proposal_id for trade in self.trades]
        if len(set(ids)) != len(ids):
            raise ValueError("each trade's liquidity is recorded once")
        return self


def trade_liquidity(
    trades: Sequence[SimulatedTrade], bars: Sequence[Bar], contract: InstrumentContract
) -> Liquidity:
    """Each trade's entry and exit bar volume, read from the bars the run replayed.

    A fill whose bar is not among them is ``CoverageError``: the bars handed in are not
    the run's, and a volume read from some other bar would be a guess.
    """

    volume_at = {bar.event_time: bar.tick_volume for bar in bars}
    money_per_point = (
        contract.value_per_price_increment * contract.point_size / contract.price_increment
    )

    def volume(at: datetime) -> int:
        if at not in volume_at:
            raise CoverageError()
        return volume_at[at]

    return Liquidity(
        trades=tuple(
            TradeLiquidity(
                proposal_id=trade.proposal_id,
                entry_tick_volume=volume(trade.entry_at),
                exit_tick_volume=volume(trade.exit_at),
                money_per_point_per_lot=money_per_point,
            )
            for trade in trades
        )
    )
