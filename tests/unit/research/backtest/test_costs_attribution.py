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


def _known_answer_run() -> BacktestOutcome:
    """Two TIME exits over the known-answer ramp, and therefore a split whose
    every component is reproducible by hand.

    Not named ``_run``: ``conftest`` has a helper of that name returning a
    ``BacktestResult``, and a reader reaching for it in this module would get
    the other one.
    """

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


@pytest.mark.parametrize("negative", ["spread_cost", "slippage_cost"])
def test_a_cost_that_was_charged_cannot_be_reported_as_a_credit(negative: str) -> None:
    """A negative component is refused outright, on either field.

    A bare ``Decimal`` accepted both, and these are the numbers an operator
    reads as what a run paid. A credit presented as a cost is the silent lie
    this slice exists to prevent, and the engine cannot produce one -- which is
    exactly why a split read off disk in Task 3 can, and why the bound belongs
    on the field rather than on the engine that happens to respect it today.
    """

    with pytest.raises(ValidationError, match="greater than or equal to 0"):
        TradeCostAttribution(
            proposal_id="p-1",
            market_pnl=Decimal("10"),
            post_fill_gross=Decimal("12"),
            **{"spread_cost": Decimal("0"), "slippage_cost": Decimal("0"), negative: Decimal("-2")},
        )


def test_a_compensating_pair_of_costs_is_refused_too() -> None:
    """Worse than either negative on its own, because every other check passes.

    ``spread +2 / slippage -2`` reconstructs its own total exactly, so the
    identity in ``CostAttribution`` cannot see it and neither could a
    sum-against-``costs`` check in Task 3. Before the sign bound this was
    accepted while reporting one of the two terms as money the run was *paid*.
    """

    with pytest.raises(ValidationError, match="greater than or equal to 0"):
        TradeCostAttribution(
            proposal_id="p-1",
            market_pnl=Decimal("10"),
            spread_cost=Decimal(2),
            slippage_cost=Decimal("-2"),
            post_fill_gross=Decimal("10"),
        )


def test_market_pnl_stays_signed_where_the_two_charges_do_not() -> None:
    """The asymmetry a later reader must not "fix" the same way twice.

    A losing trade's ``market_pnl`` is negative by definition, and bounding it
    at zero would report every loser as a break-even -- so this field is left a
    bare ``Decimal`` while the two charges beside it are not. Built through
    ``CostAttribution`` rather than on its own, so the negative term is
    exercised through the reconstruction too rather than past it.
    """

    attribution = CostAttribution(
        trades=(
            TradeCostAttribution(
                proposal_id="p-1",
                market_pnl=Decimal("-10"),
                spread_cost=Decimal("2"),
                slippage_cost=Decimal("1"),
                post_fill_gross=Decimal("-13"),
            ),
        )
    )

    assert attribution.trades[0].market_pnl == Decimal("-10")


def test_a_falsified_decomposition_is_refused_against_the_result_it_describes() -> None:
    """The check that would catch a fill-model regression.

    The components are moved together so the split still reconstructs ITSELF --
    a pair of numbers a symmetric bug would happily produce -- and the refusal
    can only come from comparing ``post_fill_gross`` against the ``gross_pnl``
    the result already reports. Drop ``BacktestOutcome``'s validator and this
    test is green while the attribution describes a run that never happened.
    """

    outcome = _known_answer_run()
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

    outcome = _known_answer_run()

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

    outcome = _known_answer_run()

    with pytest.raises(ValidationError, match="must be in result order"):
        BacktestOutcome(
            result=outcome.result,
            equity=outcome.equity,
            attribution=CostAttribution(trades=tuple(reversed(outcome.attribution.trades))),
        )
