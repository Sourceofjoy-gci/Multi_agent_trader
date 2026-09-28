"""The per-trade cost split, over generated trades rather than one fixture.

Design section 7 promises this file: "over generated trades, the three components
reconstruct ``gross_pnl`` exactly -- the same falsifiable shape 8B1's property
suite was corrected to, asserting a *mutated* series is refused rather than that a
valid one is accepted." The promise was not kept when the slice was executed, and
a committed design document asserting test evidence that does not exist is the
same defect class as the two live bugs the slice closed, so the test is written
here rather than the claim struck.

Two rules from earlier in this slice are respected, and both are about not
writing a test that passes either way:

* **Everything is built valid by construction.** The trades carry a generated
  gross, commission and swap with ``net_pnl`` derived from them, and the splits
  carry a generated non-negative spread and slippage with ``post_fill_gross`` and
  ``market_pnl`` derived from them. No ``assume``, no filtering: the budget goes
  to the assertion rather than to rejected draws.
* **The money stays well inside every bound the models impose.** There is no
  equity series here, so ``test_mark.py``'s lesson -- a generated equity that can
  reach zero and trip a division -- has no analogue. The only bound is
  ``NonNegativeDecimal`` on the two charges, and the ranges are otherwise chosen
  for readable numbers rather than to dodge an identity.

Which quantity to mutate is the other half of it. The identity
``market_pnl - spread_cost - slippage_cost == post_fill_gross`` constrains all
four fields, so a cent anywhere in it is caught by ``CostAttribution`` before the
shared predicate is ever reached. Both layers are therefore refused, each by its
own rule and in its own words, and the unmutated tuple is asserted to satisfy both
-- so the refusals belong to the mutation and not to the fixture.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from fractions import Fraction

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from trading_house.core.exits import NoExitPolicy
from trading_house.core.schemas import Side
from trading_house.marketdata.models import Timeframe
from trading_house.research.backtest.costs import CostModel
from trading_house.research.backtest.costs_attribution import (
    CostAttribution,
    TradeCostAttribution,
    attribution_disagreement,
)
from trading_house.research.backtest.fills import ExitKind
from trading_house.research.backtest.result import BacktestResult, SimulatedTrade

_START = datetime(2026, 9, 21, 9, 0, tzinfo=UTC)
# At or above zero, because ``spread_cost`` and ``slippage_cost`` are
# ``NonNegativeDecimal``: the one bound the generated data has to respect.
_CHARGE = st.decimals(min_value=0, max_value=500, places=2, allow_nan=False)
# A gross is a bare ``Decimal`` on both models, and the only relation a result
# imposes on it is that its ``net_pnl`` is the sum of the trades' nets.
_MONEY = st.decimals(min_value=-2000, max_value=2000, places=2, allow_nan=False)
# One cent, in the units the generated money is in. Any perturbation would do; a
# cent is the smallest a reader cannot mistake for a rounding artefact.
_CENT = Decimal("0.01")

_TRADE_COUNT = st.integers(min_value=1, max_value=6)


@st.composite
def _run(draw: st.DrawFn) -> tuple[tuple[SimulatedTrade, ...], list[tuple[Decimal, Decimal]]]:
    """One generated run: its trades, and the two charges each split will carry.

    Drawn together rather than as two independent strategies, because the charges
    have to be one per trade -- a length mismatch would be a fixture that cannot
    be asserted about, not a case to reason around.
    """

    count = draw(_TRADE_COUNT)
    trades = []
    for index in range(count):
        gross, commission, swap = draw(_MONEY), draw(_CHARGE), draw(_MONEY)
        trades.append(
            SimulatedTrade(
                proposal_id=f"p-{index}",
                side=Side.BUY,
                lots=Decimal("0.10"),
                entry_price=Decimal("1.10000"),
                entry_at=_START + timedelta(minutes=index),
                exit_price=Decimal("1.10100"),
                exit_at=_START + timedelta(minutes=index + 5),
                exit_kind=ExitKind.TIME,
                gross_pnl=gross,
                commission=commission,
                swap=swap,
                net_pnl=gross - commission + swap,
            )
        )
    charges = [(draw(_CHARGE), draw(_CHARGE)) for _ in range(count)]
    return tuple(trades), charges


def _result_of(trades: tuple[SimulatedTrade, ...]) -> BacktestResult:
    return BacktestResult(
        run_id="run-1",
        strategy_id="strat-1",
        strategy_version="v1",
        exit_policy=NoExitPolicy(kind="none"),
        constitution_sha256="a" * 64,
        contract_sha256="b" * 64,
        instrument_id="fx.eurusd",
        timeframe=Timeframe.M1,
        start=_START,
        end=_START + timedelta(hours=1),
        firm_equity=Decimal("100000"),
        cost_model=CostModel(
            commission_per_lot_per_side=Decimal("3.50"),
            slippage_points_per_side=Decimal("1"),
            swap_long_points_per_day=Decimal("-1"),
            swap_short_points_per_day=Decimal("-1"),
            triple_swap_weekday=2,
        ),
        atr_period=14,
        spread_window=20,
        defective_bar_tolerance=Fraction(0),
        trades=trades,
        rejections=(),
        bars_seen=60,
        snapshots_skipped=0,
        net_pnl=sum((trade.net_pnl for trade in trades), Decimal(0)),
    )


def _attribution_of(
    trades: tuple[SimulatedTrade, ...], charges: list[tuple[Decimal, Decimal]]
) -> CostAttribution:
    """Splits that genuinely decompose the trades.

    ``post_fill_gross`` is the trade's own ``gross_pnl``, and ``market_pnl`` is
    what is left once the two charges are put back in, so the identity holds for
    every draw and both refusals below are attributable to the mutation alone.
    """

    return CostAttribution(
        trades=tuple(
            TradeCostAttribution(
                proposal_id=trade.proposal_id,
                market_pnl=trade.gross_pnl + spread + slippage,
                spread_cost=spread,
                slippage_cost=slippage,
                post_fill_gross=trade.gross_pnl,
            )
            for trade, (spread, slippage) in zip(trades, charges, strict=True)
        )
    )


@given(run=_run(), target=st.integers(min_value=0, max_value=10_000))
@settings(max_examples=50)
def test_a_generated_split_reconstructs_its_gross_and_a_mutated_one_is_refused(
    run: tuple[tuple[SimulatedTrade, ...], list[tuple[Decimal, Decimal]]], target: int
) -> None:
    """Both layers, each refused in its own words, against an unmutated control.

    The two refusals are separate rules rather than one assertion twice. A cent
    on ``post_fill_gross`` is caught by ``CostAttribution``'s own reconstruction
    when the split is built through the model, and -- for a split that reached
    read time by any other route -- by ``attribution_disagreement``'s comparison
    against the trade's ``gross_pnl``. Keeping both means deleting either check
    turns something here red, which is the falsifiable shape 8B1's suite was
    corrected to and the one a "the validator agrees with itself" test cannot be.
    """

    trades, charges = run
    result = _result_of(trades)
    attribution = _attribution_of(trades, charges)

    # The control. Without it the two refusals below would prove only that a
    # mutation breaks *something*, not that the unmutated pair satisfies the chain.
    assert attribution_disagreement(attribution, result) is None

    index = target % len(trades)
    original = attribution.trades[index]
    mutated = original.model_copy(update={"post_fill_gross": original.post_fill_gross + _CENT})
    splits = (*attribution.trades[:index], mutated, *attribution.trades[index + 1 :])

    with pytest.raises(ValidationError, match="market PnL less spread and slippage"):
        CostAttribution(trades=splits)

    # ``model_copy`` is what reaches the second layer: the mutated split never got
    # past the constructor, and a document that did would still have to match the
    # trade it claims to decompose.
    tampered = attribution.model_copy(update={"trades": splits})
    assert (
        attribution_disagreement(tampered, result)
        == "a split's post-fill gross must equal its trade's gross PnL"
    )
