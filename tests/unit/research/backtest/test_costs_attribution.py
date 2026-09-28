"""The per-trade cost decomposition, and the three ways it is refused.

The checks here are the ones that keep a decomposition a decomposition. A split
whose components do not add back up to its own total is arithmetic that has
drifted, and a split that is internally consistent but describes another run's
trades is worse: it would pass every check it carries and still be evidence
about the wrong replay.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from tests.unit.research.backtest.conftest import ToyStrategy, _outcome, _ramp
from trading_house.research.backtest.costs_attribution import (
    CostAttribution,
    TradeCostAttribution,
)
from trading_house.research.backtest.mark import BacktestOutcome


def _run() -> BacktestOutcome:
    """Two TIME exits over the known-answer ramp, and therefore a split whose
    every component is reproducible by hand."""

    return _outcome(bars=_ramp(60), strategy=ToyStrategy(every_n=20))


def test_a_decomposition_that_does_not_reconstruct_its_own_total_is_refused() -> None:
    with pytest.raises(ValidationError, match="market PnL"):
        CostAttribution(
            trades=(
                TradeCostAttribution(
                    proposal_id="p-1",
                    market_pnl=Decimal("10"),
                    spread_cost=Decimal("2"),
                    slippage_cost=Decimal("1"),
                    post_fill_gross=Decimal("8"),
                ),
            )
        )


def test_an_empty_attribution_is_fine_and_says_nothing() -> None:
    """A run with no trades has no decomposition, and that is not a defect: the
    totals are all zero and both sides of the equality hold vacuously."""

    assert CostAttribution(trades=()).trades == ()


def test_a_falsified_decomposition_is_refused_against_the_result_it_describes() -> None:
    """The check that would catch a fill-model regression.

    The components are moved together so the split still reconstructs ITSELF --
    a pair of numbers a symmetric bug would happily produce -- and the refusal
    can only come from comparing ``post_fill_gross`` against the ``gross_pnl``
    the result already reports. Drop ``BacktestOutcome``'s validator and this
    test is green while the attribution describes a run that never happened.
    """

    outcome = _run()
    first = outcome.attribution.trades[0]
    falsified = TradeCostAttribution(
        proposal_id=first.proposal_id,
        market_pnl=first.market_pnl + 1,
        spread_cost=first.spread_cost,
        slippage_cost=first.slippage_cost,
        post_fill_gross=first.post_fill_gross + 1,
    )
    # Constructed through the model, so the split's own validator passes and the
    # refusal below is the outcome's and not the attribution's.
    attribution = CostAttribution(trades=(falsified, *outcome.attribution.trades[1:]))

    with pytest.raises(ValidationError, match="must equal its trade's gross PnL"):
        BacktestOutcome(result=outcome.result, equity=outcome.equity, attribution=attribution)


def test_an_attribution_that_does_not_cover_every_trade_is_refused() -> None:
    """A short tuple is internally consistent and describes fewer trades than
    the result reports, which is a silent under-report of cost."""

    outcome = _run()

    with pytest.raises(ValidationError, match="cover every trade exactly once"):
        BacktestOutcome(
            result=outcome.result,
            equity=outcome.equity,
            attribution=CostAttribution(trades=outcome.attribution.trades[:1]),
        )


def test_an_attribution_out_of_result_order_is_refused() -> None:
    """Same numbers, wrong order. A tuple can be reordered and stay internally
    consistent at every index; only binding each split to the trade beside it
    notices."""

    outcome = _run()

    with pytest.raises(ValidationError, match="must be in result order"):
        BacktestOutcome(
            result=outcome.result,
            equity=outcome.equity,
            attribution=CostAttribution(trades=tuple(reversed(outcome.attribution.trades))),
        )
