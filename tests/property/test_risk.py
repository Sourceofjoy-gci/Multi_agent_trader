"""I-19: an approved decision's realised risk never exceeds the book's budget,
and falls short by less than one lot step's worth unless the lot cap bound it.

The master spec's 8.1 asks only that realised loss equal the budget "within one
tick value". That tolerance is symmetric, so it passes an implementation that
overshoots the budget on half its trades. This property is one-sided.
"""

from decimal import Decimal

from hypothesis import given
from hypothesis import strategies as st

from tests.unit.risk.conftest import _contract
from trading_house.core.schemas import Side
from trading_house.risk.sizing import compute_volume, stop_price

PRICE_INCREMENTS = st.sampled_from([Decimal("0.00001"), Decimal("0.001"), Decimal("0.01")])
LOT_STEPS = st.sampled_from([Decimal("0.01"), Decimal("0.1"), Decimal("1")])
TICK_VALUES = st.decimals(min_value=Decimal("0.01"), max_value=Decimal("100"), places=2)
EQUITIES = st.decimals(min_value=Decimal("1000"), max_value=Decimal("10000000"), places=2)
RISK_PCTS = st.decimals(min_value=Decimal("0.01"), max_value=Decimal("5"), places=2)
TICK_COUNTS = st.integers(min_value=1, max_value=10_000)
# Far enough above TICK_COUNTS that a BUY stop always lands above zero.
ENTRY_TICKS = st.integers(min_value=100_000, max_value=200_000)
# Hundredths of a tick: the fractional part that puts entry off the grid.
OFF_GRID_HUNDREDTHS = st.integers(min_value=1, max_value=99)
SIDES = st.sampled_from([Side.BUY, Side.SELL])


@given(
    price_increment=PRICE_INCREMENTS,
    lot_step=LOT_STEPS,
    tick_value=TICK_VALUES,
    equity=EQUITIES,
    risk_pct=RISK_PCTS,
    ticks=TICK_COUNTS,
)
def test_realised_risk_never_exceeds_the_budget(
    price_increment: Decimal,
    lot_step: Decimal,
    tick_value: Decimal,
    equity: Decimal,
    risk_pct: Decimal,
    ticks: int,
) -> None:
    contract = _contract(
        price_increment=price_increment,
        point_size=price_increment,
        quantity_increment=lot_step,
        quantity_min=lot_step,
        quantity_max=Decimal("1000000"),
        value_per_price_increment=tick_value,
        min_stop_distance=price_increment,
        freeze_distance=price_increment,
    )
    distance = price_increment * ticks

    volume = compute_volume(
        stop_distance=distance,
        book_equity=equity,
        risk_per_trade_pct=risk_pct,
        contract=contract,
    )

    budget = equity * risk_pct / Decimal(100)
    money_per_lot = ticks * tick_value
    realised = money_per_lot * volume

    assert realised <= budget
    assert budget - realised < money_per_lot * lot_step


@given(
    price_increment=PRICE_INCREMENTS,
    lot_step=LOT_STEPS,
    tick_value=TICK_VALUES,
    equity=EQUITIES,
    risk_pct=RISK_PCTS,
    ticks=TICK_COUNTS,
    entry_ticks=ENTRY_TICKS,
    off_grid_hundredths=OFF_GRID_HUNDREDTHS,
    side=SIDES,
)
def test_realised_risk_at_the_emitted_stop_never_exceeds_the_budget(
    price_increment: Decimal,
    lot_step: Decimal,
    tick_value: Decimal,
    equity: Decimal,
    risk_pct: Decimal,
    ticks: int,
    entry_ticks: int,
    off_grid_hundredths: int,
    side: Side,
) -> None:
    """The same one-sided bound, over the composition rather than one function.

    The property above sizes from an on-grid distance and never reaches
    ``stop_price``, so the second rounding sits entirely outside its guard.
    A strategy's reference price is not guaranteed to sit on the tick grid,
    and ``stop_price`` quantises AWAY from entry, so the stop that is emitted
    can be further out than the distance the volume was derived from. What
    must hold is the loss at the emitted stop, not the loss at the model's.
    """

    contract = _contract(
        price_increment=price_increment,
        point_size=price_increment,
        quantity_increment=lot_step,
        quantity_min=lot_step,
        quantity_max=Decimal("1000000"),
        value_per_price_increment=tick_value,
        min_stop_distance=price_increment,
        freeze_distance=price_increment,
    )
    distance = price_increment * ticks
    entry = price_increment * (entry_ticks + Decimal(off_grid_hundredths) / Decimal(100))

    stop = stop_price(side=side, entry_price_ref=entry, stop_distance=distance, contract=contract)
    effective_distance = abs(entry - stop)
    volume = compute_volume(
        stop_distance=effective_distance,
        book_equity=equity,
        risk_per_trade_pct=risk_pct,
        contract=contract,
    )

    budget = equity * risk_pct / Decimal(100)
    realised = effective_distance / price_increment * tick_value * volume

    # Quantising away from entry can only widen, never narrow.
    assert effective_distance >= distance
    assert realised <= budget
