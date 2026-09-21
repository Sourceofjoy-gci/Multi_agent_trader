"""Section 11.2's cost equation: commission, slippage, swap, and the stress
multiplier that implements the promotion gate "a strategy must remain
profitable at 1.5x-2x expected costs" (section 12).

The repo has no ``commission`` field anywhere and ``InstrumentContract``
carries no swap rate -- ``FinancingModel`` is an enum tag, not a rate table.
So every cost here is a declared input on ``CostModel``, not something this
module can derive from the contract. A run that omits one must fail rather
than default to zero: the classic flattering backtest is one that silently
assumed zero commission (D-5).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Final

from pydantic import Field

from trading_house.core.instruments import InstrumentContract
from trading_house.core.schemas import Side
from trading_house.core.values import CanonicalModel

_ROUND_TRIP_SIDES: Final[Decimal] = Decimal(2)


class CostModel(CanonicalModel):
    """The declared costs a backtest charges. Every field but
    ``stress_multiplier`` is required -- commission, slippage and both swap
    rates are costs, not defaults, and ``stress_multiplier`` is the only
    scenario knob among them (section 12's 1.5x-2x sensitivity gate)."""

    commission_per_lot_per_side: Decimal  # account currency
    slippage_points_per_side: Decimal
    swap_long_points_per_day: Decimal  # signed; negative is a charge
    swap_short_points_per_day: Decimal
    triple_swap_weekday: int = Field(ge=0, le=6)  # 0=Monday .. 6=Sunday
    stress_multiplier: Decimal = Decimal(1)


def commission_cost(*, model: CostModel, lots: Decimal) -> Decimal:
    """Commission on both sides of a round trip. Charging once understates
    cost by half -- exactly the size of error that turns a losing strategy
    into a winner."""

    return model.commission_per_lot_per_side * lots * _ROUND_TRIP_SIDES * model.stress_multiplier


def slippage_price_offset(
    *, model: CostModel, side: Side, contract: InstrumentContract, opening: bool
) -> Decimal:
    """The price offset slippage applies, always against the trade: a buy
    slips up on entry and down on exit, and a sell the other way."""

    points = model.slippage_points_per_side * model.stress_multiplier
    magnitude = points * contract.point_size
    against_the_trade = (side is Side.BUY) == opening
    return magnitude if against_the_trade else -magnitude


def swap_cost(
    *,
    model: CostModel,
    side: Side,
    lots: Decimal,
    contract: InstrumentContract,
    opened_at: datetime,
    closed_at: datetime,
) -> Decimal:
    """Swap accrues once per rollover crossing, not once per elapsed day.

    A crossing is one date strictly after ``opened_at.date()`` up to and
    including ``closed_at.date()`` -- the day being rolled *into*. A position
    opened Monday and closed Tuesday has exactly one crossing, dated Tuesday
    (its destination day, not Monday, its source day). Whichever crossing's
    destination day falls on ``triple_swap_weekday`` is charged three days
    instead of one, covering MT5's weekend rollover. A position closed the
    same calendar day it was opened has zero crossings and pays no swap.

    ``swap_long_points_per_day`` / ``swap_short_points_per_day`` are MT5
    points -- the same unit ``slippage_points_per_side`` is in, and *not* the
    same unit as ``contract.value_per_price_increment`` (money per
    ``price_increment``). ``InstrumentContract.point_size``'s own docstring
    warns that it is "NOT the same field" as ``price_increment``, so swap
    converts points to money through ``point_size`` exactly as
    ``slippage_price_offset`` does: one point is worth
    ``point_size / price_increment`` price increments, so
    ``point_size * value_per_price_increment / price_increment`` in money.
    Every exact factor (rate, day count, lots, ``point_size``,
    ``value_per_price_increment``) is multiplied together first; the division
    by ``price_increment`` runs last, on that already-exact numerator, so it
    is the only rounding boundary in the expression.

    That division is not guaranteed to terminate: when ``price_increment``'s
    reduced-fraction denominator has prime factors other than 2 and 5 (i.e.
    ``point_size / price_increment`` is not expressible as a power of ten),
    the quotient repeats and Decimal's default context rounds it to 28
    significant digits with ROUND_HALF_EVEN. This function does not quantise
    the result any further, deliberately: ROUND_HALF_EVEN is unbiased across
    many trades, where ROUND_HALF_UP or ROUND_DOWN would drift every
    non-terminating contract's simulated cost the same direction on every
    charge; and no other function in this module rounds its output either --
    quantising money to the account currency's minor unit is the ledger's
    job downstream, not this cost model's.
    """

    rate = model.swap_long_points_per_day if side is Side.BUY else model.swap_short_points_per_day
    day_count = _rollover_day_count(
        opened_at=opened_at, closed_at=closed_at, triple_swap_weekday=model.triple_swap_weekday
    )
    return (
        rate
        * Decimal(day_count)
        * lots
        * contract.point_size
        * contract.value_per_price_increment
        / contract.price_increment
        * model.stress_multiplier
    )


def _rollover_day_count(
    *, opened_at: datetime, closed_at: datetime, triple_swap_weekday: int
) -> int:
    """One per crossing, dated by its destination day; three on the triple
    weekday. See ``swap_cost`` for the crossing's exact definition."""

    open_date = opened_at.date()
    close_date = closed_at.date()
    one_day = timedelta(days=1)
    triple_nights: Final[int] = 3

    total = 0
    destination = open_date + one_day
    while destination <= close_date:
        total += triple_nights if destination.weekday() == triple_swap_weekday else 1
        destination += one_day
    return total
