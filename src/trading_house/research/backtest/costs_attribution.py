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

``attribution_disagreement`` lives here because it is the rule this module's own
model is bound to a result by, and because the boundary runs the other way from
the one a reader expects: ``research/`` may import from ``research/backtest/``
(it already does, for ``EquitySeries`` and ``BacktestResult``) and not the
reverse, so Task 3's sealed bundle shares this function rather than keeping a
copy of its refusal messages.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Self

from pydantic import model_validator

from trading_house.core.values import CanonicalModel, NonEmptyStr, NonNegativeDecimal
from trading_house.research.backtest.result import BacktestResult


class TradeCostAttribution(CanonicalModel):
    """One trade's costs, in money, split three ways.

    ``market_pnl`` is the raw move and does not scale with any stress: the
    market's move is a fact about the run. ``spread_cost`` and
    ``slippage_cost`` are what the fills actually charged, so they do, and
    neither may be negative. A cost that was charged is not a credit, and a
    compensating pair -- ``+spread`` against ``-slippage`` -- would reconstruct
    its own total perfectly while reporting one of its terms as money the run
    was *paid*, in the very artifact whose purpose is to say what a run paid.

    ``post_fill_gross`` is stored rather than derived because §6.3 names it and
    because a stored value which must equal its own derivation is a *checked*
    duplicate rather than a silent one -- the same pattern ``BacktestOutcome``
    uses to bind a series to a result.

    **What this identifies is the total, not the split.** Every check in the
    chain -- the reconstruction below, ``attribution_disagreement``, and the
    bundle's four sums -- sees the pair of charges only through the value they
    add up to, so moving an amount from ``spread_cost`` to ``slippage_cost``
    (both non-negative, total unchanged) is accepted by all of them. That is
    inherent rather than a gap to be closed: ``gross_pnl`` was computed from two
    prices, and nothing anywhere records which part of the difference was the
    broker's spread and which was the price moving past us. The engine is the sole
    producer and its components are pinned by fixture tests against
    hand-derived numbers, so the split is attested by those fixtures -- not
    re-derived at read time, and not attested separately by this evidence.
    ``tests/unit/research/backtest/test_costs_attribution.py`` pins the limit as a
    test rather than leaving it as a sentence here.
    """

    proposal_id: NonEmptyStr
    market_pnl: Decimal
    """The raw move in the trade's favour, and the one field here that is
    *meant* to be negative: a losing trade has a negative market PnL, and
    bounding it at zero would report a loss as a break-even. It is left a bare
    ``Decimal`` for that reason -- the two fields above are charges, this is a
    result, and they are not the same kind of quantity."""

    spread_cost: NonNegativeDecimal
    slippage_cost: NonNegativeDecimal
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


def attribution_disagreement(attribution: CostAttribution, result: BacktestResult) -> str | None:
    """Why ``attribution`` does not describe ``result`` -- or ``None`` when it does.

    The contract is exact and is the point of the function: ``None`` means the
    split describes the result, and anything else is *the message to raise*. Not
    a boolean and not an exception, because two surfaces that both call this
    have to report the same reason for the same disagreement, and a caller
    writing its own wording is exactly how a sealed bundle starts disagreeing
    with the model it was built from.

    A tuple can be internally consistent and still describe another run:
    reordered, padded, or carrying a ``post_fill_gross`` the ``gross_pnl`` no
    longer matches. All three are refused here, in that order.

    Shared rather than restated because the import constraint runs downward
    only. ``BACKTEST_ALLOWED`` admits ``trading_house.research.backtest`` and
    not ``trading_house.research``, which stops a module in *this* subtree
    reaching up into ``research/``; ``research/evidence.py`` already imports
    from here, so Task 3's ``EvidenceBundle`` calls this instead of copying its
    wording. Duplicated refusal messages drift the moment one is edited and the
    other is not, and the drift is invisible until the two surfaces disagree
    about the same defect in front of a reader.
    """

    if len(attribution.trades) != len(result.trades):
        return "the attribution must cover every trade exactly once"
    for split, trade in zip(attribution.trades, result.trades, strict=True):
        if split.proposal_id != trade.proposal_id:
            return "the attribution must be in result order"
        if split.post_fill_gross != trade.gross_pnl:
            return "a split's post-fill gross must equal its trade's gross PnL"
    return None
