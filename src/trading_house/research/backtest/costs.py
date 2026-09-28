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
_TWO: Final[Decimal] = Decimal(2)


class CostModel(CanonicalModel):
    """The declared costs a backtest charges. Every field but
    ``stress_multiplier`` is required -- commission, slippage and both swap
    rates are costs, not defaults, and ``stress_multiplier`` is the only
    scenario knob among them (section 12's 1.5x-2x sensitivity gate).

    ``stress_multiplier`` is bounded strictly above zero, and that bound is
    load-bearing rather than tidy. At zero every term in the cost equation
    vanishes and at a negative value every cost becomes a credit -- which is
    the zero-commission backtest D-5 forbids, reached through the one field
    D-5's own guard deliberately exempts.

    **The gate is 1.5x-2x** (section 12): a strategy must stay profitable
    there. Values below 1 are allowed because "what if costs are lower than
    assumed" is a legitimate sensitivity probe in the other direction, but a
    result produced below 1 flatters the strategy and is not evidence it
    passes anything.

    ``swap_*_points_per_day`` is signed, so section 6.4's multiplier is adverse
    rather than uniform: a charge becomes ``m`` times more negative, while a
    credit is reduced by ``(m - 1) * abs(rate)`` and so can never grow. The
    rule lives in this module's ``_stressed_rate``, applied to the rate before
    the rest of the product, and it is total over the whole ``gt=0`` domain --
    both branches return the rate unchanged at ``m = 1``, a credit grows below
    1, is zero at 2, and becomes a charge above 2.

    It is not stated here as an accepted consequence of section 7.1's "one
    multiplier over the whole model" because a stress that is not adverse is
    not a stress: the unconditional ``rate * m`` it replaced handed a
    positive-carry strategy more profit at 2x than at 1x, so that strategy
    cleared the section 12 gate more easily the harder its costs were stressed.
    """

    commission_per_lot_per_side: Decimal  # account currency
    slippage_points_per_side: Decimal = Field(ge=0)
    swap_long_points_per_day: Decimal  # signed; negative is a charge
    swap_short_points_per_day: Decimal
    triple_swap_weekday: int = Field(ge=0, le=6)  # 0=Monday .. 6=Sunday
    stress_multiplier: Decimal = Field(default=Decimal(1), gt=0)


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
    Every exact factor (the stressed rate, day count, lots, ``point_size`` and
    ``value_per_price_increment``) is multiplied together first; the division
    by ``price_increment`` runs last, on that already-exact numerator, so it is
    the only rounding boundary in the expression. ``stress_multiplier`` reaches
    that numerator through ``_stressed_rate``, which runs first, and must:
    ``_stressed_rate`` is a product, and on the credit branch a subtraction, but
    no division, so it stays exact. Applying the multiplier *after* the division
    instead would round a second time, and with ``point_size=0.00001``,
    ``price_increment=0.00003`` and a stress of 3 the two orders differ by one
    ulp (``-0.9999999999999999999999999999`` against ``-1``). ``digest()``
    hashes a ``Decimal``'s string form, so that ulp would reach the digest
    Phase 8's trial ledger stores.

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
        _stressed_rate(rate, model.stress_multiplier)
        * Decimal(day_count)
        * lots
        * contract.point_size
        * contract.value_per_price_increment
        / contract.price_increment
    )


def _stressed_rate(rate: Decimal, multiplier: Decimal) -> Decimal:
    """Section 6.4's adverse multiplier, which is piecewise in the rate's sign.

    A charge becomes ``m`` times more negative. A credit is reduced by
    ``(m - 1) * abs(rate)``, so stress can never *increase* carry -- which is
    the defect this replaces. The previous unconditional ``rate * m`` handed a
    positive-carry strategy more profit at 2x than at 1x, and this module's own
    docstring had to record that as an accepted consequence rather than fix it.

    At ``m = 1`` both branches return the rate unchanged, which is §6.4's "at
    m = 1 every component exactly matches baseline" and is what makes this a
    stress rather than a redefinition of the baseline. Below 1 a credit grows,
    which is the legitimate "what if costs are lower than assumed" probe
    ``CostModel`` already invites; at 2 it is zero; above 2 it becomes a charge.
    """

    if rate < 0:
        return rate * multiplier
    return rate * (_TWO - multiplier)


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
