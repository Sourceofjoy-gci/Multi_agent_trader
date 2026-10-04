"""Bollinger bands over closes. Pure: closes in, a Decimal out.

These feed only a strategy's entry test and its invalidation price, which the
risk engine then turns into a stop distance -- so like ``volatility.py`` the
correctness bar is the execution path's. Population deviation (divide by n),
because that is Bollinger's own definition and the strategy spec fixes it.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from trading_house.core.errors import InsufficientHistoryError


def simple_mean(values: Sequence[Decimal]) -> Decimal:
    if not values:
        raise InsufficientHistoryError
    return sum(values, start=Decimal(0)) / len(values)


def _population_variance(values: Sequence[Decimal]) -> Decimal:
    mean = simple_mean(values)
    return sum(((value - mean) ** 2 for value in values), start=Decimal(0)) / len(values)


def population_stdev(values: Sequence[Decimal]) -> Decimal:
    return _population_variance(values).sqrt()


def squared_bandwidth(values: Sequence[Decimal], width: Decimal) -> Decimal:
    """``bandwidth`` squared, computed without a square root.

    The squeeze test compares 135 bandwidths per bar to find a minimum. The
    ordering of non-negative numbers survives squaring, so the comparison can
    skip ``sqrt`` entirely -- and is deterministic (same inputs, same digits,
    every time), not exact: the final ``Decimal`` division still rounds to 28
    significant digits.
    """

    mean = simple_mean(values)
    return 4 * width * width * _population_variance(values) / (mean * mean)
