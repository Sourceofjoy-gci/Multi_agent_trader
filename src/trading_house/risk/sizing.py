"""Stop-distance and volume arithmetic. Pure: no I/O, no clock, no constitution.

The order of rounding is the point of this module. The stop distance rounds UP
to the tick grid and the volume rounds DOWN to the lot grid, so both roundings
push the same way and realised risk is bounded above by the budget instead of
straddling it. Reversing either one lets a position exceed the signed risk
budget on roughly half of all trades.
"""

from __future__ import annotations

from decimal import Decimal

from trading_house.core.instruments import InstrumentContract
from trading_house.core.schemas import Side


def _checked(value: Decimal, increment: Decimal) -> None:
    if increment <= 0:
        raise ValueError("increment must be positive")
    if value < 0:
        raise ValueError("value must not be negative")


def quantise_up(value: Decimal, increment: Decimal) -> Decimal:
    """The smallest multiple of ``increment`` that is not below ``value``."""

    _checked(value, increment)
    whole, remainder = divmod(value, increment)
    return whole * increment if remainder == 0 else (whole + 1) * increment


def quantise_down(value: Decimal, increment: Decimal) -> Decimal:
    """The largest multiple of ``increment`` that is not above ``value``."""

    _checked(value, increment)
    whole, _ = divmod(value, increment)
    return whole * increment


def compute_stop_distance(
    *,
    atr: Decimal,
    median_spread_points: Decimal,
    entry_price_ref: Decimal,
    invalidation_price: Decimal,
    contract: InstrumentContract,
    k_sigma: Decimal,
    k_spread: Decimal,
) -> Decimal:
    """The master spec's 8.2 four-term maximum, rounded up to the tick grid.

    The cost term takes the MEDIAN spread, not the current one: a single
    spike must not widen the stop and therefore shrink the position. The
    current spread is a gate in the engine, not an input here.
    """

    volatility = k_sigma * atr
    cost = k_spread * median_spread_points * contract.point_size
    structural = abs(entry_price_ref - invalidation_price)
    broker_floor = max(contract.min_stop_distance, contract.freeze_distance)
    widest = max(volatility, cost, structural, broker_floor)
    return quantise_up(widest, contract.price_increment)


def compute_volume(
    *,
    stop_distance: Decimal,
    book_equity: Decimal,
    risk_per_trade_pct: Decimal,
    contract: InstrumentContract,
) -> Decimal:
    """Lots such that the loss at ``stop_distance`` does not exceed the budget.

    ``book_equity`` is the book's own slice of firm equity, not firm equity.
    The caller does that multiply; see the engine.
    """

    if stop_distance <= 0:
        raise ValueError("stop distance must be positive")
    if book_equity <= 0:
        raise ValueError("book equity must be positive")
    budget = book_equity * risk_per_trade_pct / Decimal(100)
    ticks = stop_distance / contract.price_increment
    money_per_lot = ticks * contract.value_per_price_increment
    if money_per_lot <= 0:
        raise ValueError("non-positive money per lot; instrument contract invalid")
    return quantise_down(budget / money_per_lot, contract.quantity_increment)


def stop_price(
    *,
    side: Side,
    entry_price_ref: Decimal,
    stop_distance: Decimal,
    contract: InstrumentContract,
) -> Decimal:
    """The protective stop, anchored on the proposal's reference price.

    Anchoring on the reference rather than on a live tick is what makes the
    decision replayable: the same proposal and the same stored bars must give
    the same stop in a backtest a year later, when no live quote exists. The
    gap between the reference and the actual fill is slippage, and that
    belongs to execution.
    """

    if side is Side.BUY:
        raw = entry_price_ref - stop_distance
        if raw < contract.price_increment:
            raise ValueError("stop price would not land on a positive tick")
        return quantise_down(raw, contract.price_increment)
    return quantise_up(entry_price_ref + stop_distance, contract.price_increment)
