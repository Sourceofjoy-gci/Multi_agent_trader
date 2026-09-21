from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from trading_house.core.instruments import FillPolicy, FinancingModel, InstrumentContract
from trading_house.core.schemas import Side
from trading_house.core.values import AssetClass
from trading_house.research.backtest.costs import (
    CostModel,
    commission_cost,
    slippage_price_offset,
    swap_cost,
)


def _model(**overrides: object) -> CostModel:
    defaults: dict[str, object] = {
        "commission_per_lot_per_side": Decimal("3.50"),
        "slippage_points_per_side": Decimal("1"),
        "swap_long_points_per_day": Decimal("-1"),
        "swap_short_points_per_day": Decimal("-1"),
        "triple_swap_weekday": 2,
    }
    return CostModel(**{**defaults, **overrides})  # type: ignore[arg-type]


def _contract(**overrides: object) -> InstrumentContract:
    defaults: dict[str, object] = {
        "instrument_id": "fx.eurusd",
        "asset_class": AssetClass.FX,
        "base_currency": "EUR",
        "quote_currency": "USD",
        "price_increment": Decimal("0.00001"),
        "point_size": Decimal("0.00001"),
        "quantity_increment": Decimal("0.01"),
        "quantity_min": Decimal("0.01"),
        "quantity_max": Decimal("100"),
        "value_per_price_increment": Decimal("1"),
        "min_stop_distance": Decimal("0.0002"),
        "freeze_distance": Decimal("0.0001"),
        "session_calendar_id": "fx.24x5",
        "financing": FinancingModel.SWAP,
        "can_open_long": True,
        "can_open_short": True,
        "supported_fills": frozenset({FillPolicy.IOC, FillPolicy.FOK}),
    }
    return InstrumentContract(**{**defaults, **overrides})  # type: ignore[arg-type]


def test_a_cost_model_cannot_be_constructed_without_stating_its_costs() -> None:
    """D-5. The classic flattering backtest is one that silently assumed zero
    commission. Omitting a field must be an error, not a zero."""

    with pytest.raises(ValidationError):
        CostModel(slippage_points_per_side=Decimal("0.5"))  # type: ignore[call-arg]


def test_commission_is_charged_on_both_sides() -> None:
    """A round trip pays twice. Charging once understates cost by half, which
    is exactly the size of error that turns a losing strategy into a winner."""

    model = _model(commission_per_lot_per_side=Decimal("3.50"))

    assert commission_cost(model=model, lots=Decimal("2")) == Decimal("14.00")


def test_slippage_always_moves_the_price_against_the_trade() -> None:
    """D-6, in the one place a sign error is invisible: a buy slips up on entry
    and down on exit, and a sell the other way. Getting one of the four wrong
    produces a simulator that pays slippage on three legs and earns it on one."""

    model = _model(slippage_points_per_side=Decimal("2"))
    contract = _contract(point_size=Decimal("0.00001"))

    buy_in = slippage_price_offset(model=model, side=Side.BUY, contract=contract, opening=True)
    buy_out = slippage_price_offset(model=model, side=Side.BUY, contract=contract, opening=False)
    sell_in = slippage_price_offset(model=model, side=Side.SELL, contract=contract, opening=True)
    sell_out = slippage_price_offset(model=model, side=Side.SELL, contract=contract, opening=False)

    assert buy_in == Decimal("0.00002")  # pay up to get in
    assert buy_out == Decimal("-0.00002")  # sell lower to get out
    assert sell_in == Decimal("-0.00002")
    assert sell_out == Decimal("0.00002")


def test_the_stress_multiplier_scales_every_term_together() -> None:
    """Section 11.2's gate is "profitable at 1.5x-2x expected costs". One
    multiplier over the whole model rather than a second code path -- a
    separate stressed path is one that drifts from the unstressed one. Every
    term must scale, not just commission: a caller relying on this gate to
    catch a strategy that only survives at nominal costs needs slippage and
    swap stressed too, or the gate tests one third of the model."""

    base = _model(
        commission_per_lot_per_side=Decimal("3.00"),
        slippage_points_per_side=Decimal("2"),
        swap_long_points_per_day=Decimal("-1"),
    )
    stressed = base.model_copy(update={"stress_multiplier": Decimal(2)})
    contract = _contract(point_size=Decimal("0.00001"), value_per_price_increment=Decimal("1"))

    assert commission_cost(model=stressed, lots=Decimal(1)) == Decimal("12.00")
    assert commission_cost(model=base, lots=Decimal(1)) == Decimal("6.00")

    assert slippage_price_offset(
        model=stressed, side=Side.BUY, contract=contract, opening=True
    ) == Decimal("0.00004")
    assert slippage_price_offset(
        model=base, side=Side.BUY, contract=contract, opening=True
    ) == Decimal("0.00002")

    opened_at = datetime(2026, 9, 21, 9, 0, tzinfo=UTC)  # Monday
    closed_at = datetime(2026, 9, 22, 9, 0, tzinfo=UTC)  # Tuesday, one crossing

    assert swap_cost(
        model=stressed,
        side=Side.BUY,
        lots=Decimal(1),
        contract=contract,
        opened_at=opened_at,
        closed_at=closed_at,
    ) == Decimal("-2")
    assert swap_cost(
        model=base,
        side=Side.BUY,
        lots=Decimal(1),
        contract=contract,
        opened_at=opened_at,
        closed_at=closed_at,
    ) == Decimal("-1")


def test_swap_is_tripled_on_the_rollover_weekday() -> None:
    """MT5 charges three days of swap on one weekday to cover the weekend. A
    position held across it pays three times, and a swing strategy held for days
    pays it repeatedly."""

    model = _model(swap_long_points_per_day=Decimal("-1"), triple_swap_weekday=2)
    contract = _contract(point_size=Decimal("0.00001"), value_per_price_increment=Decimal(1))
    # Monday 2026-09-21 to Thursday 2026-09-24: crosses Mon->Tue, Tue->Wed
    # (the triple), Wed->Thu. Five days of swap charged over three nights.
    cost = swap_cost(
        model=model,
        side=Side.BUY,
        lots=Decimal(1),
        contract=contract,
        opened_at=datetime(2026, 9, 21, 9, 0, tzinfo=UTC),
        closed_at=datetime(2026, 9, 24, 9, 0, tzinfo=UTC),
    )

    assert cost == Decimal("-5")


def test_swap_triple_weekday_matches_the_crossings_destination_not_source() -> None:
    """The brief's own worked example (Monday->Thursday, triple on Wednesday)
    does not discriminate between "the crossing's destination date" and "the
    crossing's source date" as the convention for matching
    ``triple_swap_weekday``: both readings total five days charged there,
    because over that range the triple night just shifts from Tue->Wed to
    Wed->Thu and the total count of triples (one) is unchanged.

    A single-night crossing does discriminate. Opened Monday, closed Tuesday:
    there is exactly one rollover, from Monday (weekday 0) to Tuesday
    (weekday 1). With ``triple_swap_weekday=0`` (Monday):

    - destination convention: the crossing's destination is Tuesday (weekday
      1) != 0, so it is charged as a single ordinary day -> -1.
    - source convention: the crossing's source is Monday (weekday 0) == 0,
      so it would be tripled -> -3.

    Other ranges considered: reusing the brief's own Monday-Thursday range
    but changing ``triple_swap_weekday`` from 2 (Wednesday) to 0 (Monday)
    also discriminates (destination gives 3, source gives 5), since Monday is
    the range's source-only boundary date. The single-night range here is
    preferred because it isolates exactly one crossing, so the discriminating
    assertion requires no addition and there is no risk of an unrelated
    off-by-one in the test coincidentally reproducing the wrong answer.
    """

    model = _model(swap_long_points_per_day=Decimal("-1"), triple_swap_weekday=0)
    contract = _contract(point_size=Decimal("0.00001"), value_per_price_increment=Decimal(1))

    cost = swap_cost(
        model=model,
        side=Side.BUY,
        lots=Decimal(1),
        contract=contract,
        opened_at=datetime(2026, 9, 21, 9, 0, tzinfo=UTC),  # Monday
        closed_at=datetime(2026, 9, 22, 9, 0, tzinfo=UTC),  # Tuesday
    )

    assert cost == Decimal("-1")


def test_swap_converts_points_through_point_size_not_price_increment() -> None:
    """`slippage_price_offset` turns points into a price distance through
    `contract.point_size` (costs.py:57); `swap_cost` must convert its points
    rate the same way rather than assuming one swap point equals one
    `price_increment`. `InstrumentContract.point_size`'s own docstring warns
    against exactly that assumption: it is "Equal to price_increment on most
    FX symbols and NOT the same field". A contract where they differ -- a
    shape MT5 genuinely produces -- is the only fixture that can catch the
    two functions disagreeing about what a "point" is; the module's other
    tests all use `point_size == price_increment`, where the bug is
    invisible."""

    model = _model(swap_long_points_per_day=Decimal("-1"), triple_swap_weekday=2)
    contract = _contract(
        point_size=Decimal("0.00001"),
        price_increment=Decimal("0.001"),
        value_per_price_increment=Decimal("1"),
    )

    cost = swap_cost(
        model=model,
        side=Side.BUY,
        lots=Decimal(1),
        contract=contract,
        opened_at=datetime(2026, 9, 21, 9, 0, tzinfo=UTC),  # Monday
        closed_at=datetime(2026, 9, 22, 9, 0, tzinfo=UTC),  # Tuesday, one crossing
    )

    # -1 point/day * 1 day * 1 lot, converted through point_size/price_increment
    # (0.00001 / 0.001 = 0.01) rather than taken as one price_increment.
    assert cost == Decimal("-0.01")


@pytest.mark.parametrize("weekday", [-1, 7])
def test_triple_swap_weekday_rejects_out_of_range_values(weekday: int) -> None:
    """`Field(ge=0, le=6)` (costs.py:38) only closes the loop on "0=Monday ..
    6=Sunday" if the two edges just outside it actually raise."""

    with pytest.raises(ValidationError):
        _model(triple_swap_weekday=weekday)


def test_a_position_closed_the_same_day_pays_no_swap() -> None:
    """Swap accrues at the daily rollover. An intraday strategy never pays it,
    and charging it anyway would penalise exactly the horizons this phase can
    simulate best."""

    model = _model(swap_long_points_per_day=Decimal("-1"))
    cost = swap_cost(
        model=model,
        side=Side.BUY,
        lots=Decimal(1),
        contract=_contract(),
        opened_at=datetime(2026, 9, 21, 9, 0, tzinfo=UTC),
        closed_at=datetime(2026, 9, 21, 17, 0, tzinfo=UTC),
    )

    assert cost == Decimal(0)
