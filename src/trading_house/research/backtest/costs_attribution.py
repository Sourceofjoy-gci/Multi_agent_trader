"""What one trade was actually charged, split from the number that reported it.

Phase 8B2a. ``SimulatedTrade.gross_pnl`` is gross of commission and swap but
already contains spread and slippage, because the fill model charges both into
the two prices and discards the components. This module carries that split
beside the trade rather than inside it: a field on ``SimulatedTrade`` would move
``BacktestResult.digest()`` for every run, and with it four pinned digest
constants naming Phase 7 artifacts that exist on no machine and can never be
re-derived.

The equality checked here is not arithmetic added for its own sake. It is the
claim that a reported number was not merely restated but genuinely decomposed:
``market_pnl - spread_cost - slippage_cost`` must equal the ``gross_pnl`` the
result already carries, exactly, for every trade.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Self

from pydantic import model_validator

from trading_house.core.values import CanonicalModel, NonEmptyStr


class TradeCostAttribution(CanonicalModel):
    """One trade's costs, in money, split three ways.

    ``market_pnl`` is the raw move and does not scale with any stress: the
    market's move is a fact about the run. ``spread_cost`` and
    ``slippage_cost`` are what the fills actually charged, so they do.

    ``post_fill_gross`` is stored rather than derived because §6.3 names it and
    because a stored value which must equal its own derivation is a *checked*
    duplicate rather than a silent one -- the same pattern ``BacktestOutcome``
    uses to bind a series to a result.
    """

    proposal_id: NonEmptyStr
    market_pnl: Decimal
    spread_cost: Decimal
    slippage_cost: Decimal
    post_fill_gross: Decimal


class CostAttribution(CanonicalModel):
    """Every trade's split, in result order.

    Ordered and parallel to ``result.trades``, matched by ``proposal_id`` at
    every index by ``BacktestOutcome`` -- a tuple that can be reordered, padded
    or paired with another run's trades would otherwise still be internally
    consistent.
    """

    trades: tuple[TradeCostAttribution, ...]

    @model_validator(mode="after")
    def each_split_reconstructs_its_own_total(self) -> Self:
        for split in self.trades:
            rebuilt = split.market_pnl - split.spread_cost - split.slippage_cost
            if rebuilt != split.post_fill_gross:
                raise ValueError(
                    "a trade's market PnL less spread and slippage must equal its post-fill gross"
                )
        return self
