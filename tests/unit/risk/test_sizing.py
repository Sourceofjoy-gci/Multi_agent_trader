"""Pure sizing arithmetic. Every expected value here is worked longhand in a
comment so a reader can verify it without running the code."""

from decimal import Decimal

import pytest

from tests.unit.risk.conftest import _contract
from trading_house.core.schemas import Side
from trading_house.risk.sizing import (
    compute_stop_distance,
    compute_volume,
    quantise_down,
    quantise_up,
    stop_price,
)


@pytest.mark.parametrize(
    ("value", "increment", "expected"),
    [
        ("0.00013", "0.00001", "0.00013"),  # already on the grid, unchanged
        ("0.000131", "0.00001", "0.00014"),  # rounds up to the next tick
        ("0.000139", "0.00001", "0.00014"),
        ("0", "0.00001", "0"),
    ],
)
def test_quantise_up_never_lands_below_its_input(value: str, increment: str, expected: str) -> None:
    result = quantise_up(Decimal(value), Decimal(increment))

    assert result == Decimal(expected)
    assert result >= Decimal(value)


@pytest.mark.parametrize(
    ("value", "increment", "expected"),
    [
        ("0.37", "0.01", "0.37"),
        ("0.379", "0.01", "0.37"),
        ("0.371", "0.01", "0.37"),
        ("0.009", "0.01", "0"),
    ],
)
def test_quantise_down_never_lands_above_its_input(
    value: str, increment: str, expected: str
) -> None:
    result = quantise_down(Decimal(value), Decimal(increment))

    assert result == Decimal(expected)
    assert result <= Decimal(value)


def test_quantisers_refuse_a_non_positive_increment() -> None:
    with pytest.raises(ValueError, match="increment"):
        quantise_up(Decimal("1"), Decimal("0"))
    with pytest.raises(ValueError, match="increment"):
        quantise_down(Decimal("1"), Decimal("-0.01"))


def test_quantisers_refuse_a_negative_value() -> None:
    """Decimal's divmod truncates toward zero rather than flooring, so a
    negative input would round the wrong way silently."""

    with pytest.raises(ValueError, match="negative"):
        quantise_down(Decimal("-1"), Decimal("0.01"))


def test_the_volatility_term_wins_when_it_is_largest() -> None:
    """k_sigma 2.0 x ATR 0.00050 = 0.00100, above every other term."""

    distance = compute_stop_distance(
        atr=Decimal("0.00050"),
        median_spread_points=Decimal("10"),  # cost: 2.0 x 10 x 0.00001 = 0.00020
        entry_price_ref=Decimal("1.10000"),
        invalidation_price=Decimal("1.09970"),  # structural: 0.00030
        contract=_contract(),  # floor: 0.00001
        k_sigma=Decimal("2.0"),
        k_spread=Decimal("2.0"),
    )

    assert distance == Decimal("0.00100")


def test_the_cost_term_wins_when_the_spread_is_wide() -> None:
    """k_spread 2.0 x 60 points x 0.00001 = 0.00120, above the 0.00040
    volatility term and the 0.00030 structural term."""

    distance = compute_stop_distance(
        atr=Decimal("0.00020"),
        median_spread_points=Decimal("60"),
        entry_price_ref=Decimal("1.10000"),
        invalidation_price=Decimal("1.09970"),
        contract=_contract(),
        k_sigma=Decimal("2.0"),
        k_spread=Decimal("2.0"),
    )

    assert distance == Decimal("0.00120")


def test_the_structural_term_wins_when_invalidation_is_far() -> None:
    """|1.10000 - 1.09250| = 0.00750, above the 0.00040 volatility term."""

    distance = compute_stop_distance(
        atr=Decimal("0.00020"),
        median_spread_points=Decimal("10"),
        entry_price_ref=Decimal("1.10000"),
        invalidation_price=Decimal("1.09250"),
        contract=_contract(),
        k_sigma=Decimal("2.0"),
        k_spread=Decimal("2.0"),
    )

    assert distance == Decimal("0.00750")


def test_the_broker_floor_wins_when_every_other_term_is_tiny() -> None:
    """A broker demanding 50 points of stop distance overrides a quiet market."""

    distance = compute_stop_distance(
        atr=Decimal("0.00001"),
        median_spread_points=Decimal("1"),
        entry_price_ref=Decimal("1.10000"),
        invalidation_price=Decimal("1.09999"),
        contract=_contract(min_stop_distance=Decimal("0.00050")),
        k_sigma=Decimal("2.0"),
        k_spread=Decimal("2.0"),
    )

    assert distance == Decimal("0.00050")


def test_the_floor_takes_freeze_distance_when_it_is_the_wider_of_the_two() -> None:
    """A stop inside the freeze band is legal to place and illegal to modify,
    which would hand the position guard an untouchable stop on a live
    position. The master spec's 8.2 code uses only stops_level; its comment
    says stops/freeze, and the comment is the correct reading."""

    distance = compute_stop_distance(
        atr=Decimal("0.00001"),
        median_spread_points=Decimal("1"),
        entry_price_ref=Decimal("1.10000"),
        invalidation_price=Decimal("1.09999"),
        contract=_contract(
            min_stop_distance=Decimal("0.00020"),
            freeze_distance=Decimal("0.00080"),
        ),
        k_sigma=Decimal("2.0"),
        k_spread=Decimal("2.0"),
    )

    assert distance == Decimal("0.00080")


def test_the_distance_is_quantised_up_to_the_tick_grid() -> None:
    """k_sigma 1.5 x ATR 0.000333 = 0.0004995, which is not on a 0.00001 grid.
    Rounding down would place the stop nearer than the risk model demanded."""

    distance = compute_stop_distance(
        atr=Decimal("0.000333"),
        median_spread_points=Decimal("1"),
        entry_price_ref=Decimal("1.10000"),
        invalidation_price=Decimal("1.09999"),
        contract=_contract(),
        k_sigma=Decimal("1.5"),
        k_spread=Decimal("2.0"),
    )

    assert distance == Decimal("0.00050")


def test_the_cost_term_uses_point_size_not_price_increment() -> None:
    """On a symbol where they differ, pricing the spread with price_increment
    inflates the cost term fivefold. 2.0 x 30 points x 0.001 = 0.06."""

    distance = compute_stop_distance(
        atr=Decimal("0.001"),
        median_spread_points=Decimal("30"),
        entry_price_ref=Decimal("2000.000"),
        invalidation_price=Decimal("1999.999"),
        contract=_contract(
            price_increment=Decimal("0.005"),
            point_size=Decimal("0.001"),
            min_stop_distance=Decimal("0.005"),
            freeze_distance=Decimal("0.005"),
        ),
        k_sigma=Decimal("2.0"),
        k_spread=Decimal("2.0"),
    )

    assert distance == Decimal("0.060")


def test_volume_against_a_hand_worked_example() -> None:
    """EURUSD, 1 lot moves $1.00 per 0.00001 of price.

        budget        = 10_000 x 0.25 / 100          = 25.00
        ticks         = 0.00100 / 0.00001            = 100
        money_per_lot = 100 x 1.00                   = 100.00
        raw volume    = 25.00 / 100.00               = 0.25
        on the grid   = floor(0.25 / 0.01) x 0.01    = 0.25

    Loss if the stop is hit: 100 ticks x $1.00 x 0.25 lots = $25.00, exactly
    the budget, because 0.25 lands on the lot grid.
    """

    volume = compute_volume(
        stop_distance=Decimal("0.00100"),
        book_equity=Decimal("10000"),
        risk_per_trade_pct=Decimal("0.25"),
        contract=_contract(),
    )

    assert volume == Decimal("0.25")


def test_volume_floors_to_the_lot_grid_and_never_rounds_up() -> None:
    """Same contract, an equity that does not divide evenly:

        budget        = 10_000 x 0.30 / 100          = 30.00
        money_per_lot = 100 x 1.00                   = 100.00
        raw volume    = 30.00 / 100.00               = 0.30
        ... now with a 0.10 lot step:
        on the grid   = floor(0.30 / 0.10) x 0.10    = 0.30

    and with an equity of 10_500 the raw volume is 0.315, which must floor to
    0.30 and not round to 0.32 or up to 0.40. Loss at the stop is then
    100 x 1.00 x 0.30 = $30.00 against a budget of $31.50 -- short by less
    than one lot step's worth, which is I-19's bound.
    """

    volume = compute_volume(
        stop_distance=Decimal("0.00100"),
        book_equity=Decimal("10500"),
        risk_per_trade_pct=Decimal("0.30"),
        contract=_contract(quantity_increment=Decimal("0.10"), quantity_min=Decimal("0.10")),
    )

    assert volume == Decimal("0.30")


def test_a_wider_stop_buys_a_smaller_position() -> None:
    """Doubling the stop distance must halve the volume, or the risk budget is
    not being respected at all.

    risk_per_trade_pct is 0.20 here, not the brief's 0.25: at 0.25 the raw
    volume for the doubled stop is 0.125, which floors to 0.12 on the 0.01
    lot grid and breaks the exact-halving assertion through no fault of
    compute_volume -- the quantisation is doing exactly what Step 3
    specifies. 0.20 puts both raw volumes (0.20 and 0.10) exactly on the
    grid so the test measures the halving property instead of a rounding
    artefact of its own example numbers. See task-2-report.md.
    """

    narrow = compute_volume(
        stop_distance=Decimal("0.00100"),
        book_equity=Decimal("10000"),
        risk_per_trade_pct=Decimal("0.20"),
        contract=_contract(),
    )
    wide = compute_volume(
        stop_distance=Decimal("0.00200"),
        book_equity=Decimal("10000"),
        risk_per_trade_pct=Decimal("0.20"),
        contract=_contract(),
    )

    assert wide == narrow / 2


def test_volume_refuses_a_non_positive_stop_distance() -> None:
    with pytest.raises(ValueError, match="stop distance"):
        compute_volume(
            stop_distance=Decimal("0"),
            book_equity=Decimal("10000"),
            risk_per_trade_pct=Decimal("0.25"),
            contract=_contract(),
        )


def test_volume_refuses_non_positive_equity() -> None:
    """A blown account must not produce a number the caller could act on."""

    with pytest.raises(ValueError, match="equity"):
        compute_volume(
            stop_distance=Decimal("0.00100"),
            book_equity=Decimal("0"),
            risk_per_trade_pct=Decimal("0.25"),
            contract=_contract(),
        )


def test_a_long_stop_sits_below_entry_and_a_short_stop_above() -> None:
    contract = _contract()

    assert stop_price(
        side=Side.BUY,
        entry_price_ref=Decimal("1.10000"),
        stop_distance=Decimal("0.00100"),
        contract=contract,
    ) == Decimal("1.09900")
    assert stop_price(
        side=Side.SELL,
        entry_price_ref=Decimal("1.10000"),
        stop_distance=Decimal("0.00100"),
        contract=contract,
    ) == Decimal("1.10100")


def test_the_stop_price_is_quantised_away_from_entry() -> None:
    """entry_price_ref is a strategy's reference and need not sit on the grid.
    1.100005 - 0.00100 = 1.099005; rounding toward entry would give 1.09901
    and narrow the distance the volume was computed from."""

    assert stop_price(
        side=Side.BUY,
        entry_price_ref=Decimal("1.100005"),
        stop_distance=Decimal("0.00100"),
        contract=_contract(),
    ) == Decimal("1.09900")
    assert stop_price(
        side=Side.SELL,
        entry_price_ref=Decimal("1.100005"),
        stop_distance=Decimal("0.00100"),
        contract=_contract(),
    ) == Decimal("1.10101")


def test_stop_price_refuses_a_long_stop_that_cannot_land_above_zero() -> None:
    """A distance at or beyond the entry price has no representable stop. The
    engine gates this before calling, so reaching here is a caller bug."""

    with pytest.raises(ValueError, match="stop price"):
        stop_price(
            side=Side.BUY,
            entry_price_ref=Decimal("0.00100"),
            stop_distance=Decimal("0.00100"),
            contract=_contract(),
        )
