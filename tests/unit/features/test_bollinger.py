from decimal import Decimal

import pytest

from trading_house.core.errors import InsufficientHistoryError
from trading_house.features.indicators.bollinger import (
    population_stdev,
    simple_mean,
    squared_bandwidth,
)

TWO = Decimal(2)
ALTERNATING = [Decimal("1.1000"), Decimal("1.1002")] * 10
"""Twenty closes alternating 0.0001 either side of 1.1001: population sigma is
exactly 0.0001, so every expected value below is exact rather than rounded."""


def test_simple_mean_is_the_arithmetic_mean() -> None:
    assert simple_mean([Decimal(1), Decimal(2), Decimal(3), Decimal(4)]) == Decimal("2.5")


def test_population_stdev_divides_by_n_not_n_minus_one() -> None:
    """The textbook population example: mean 5, variance 4. Sample deviation
    (n - 1) would give 2.138..., so this pins which one Bollinger's bands use."""

    values = [Decimal(v) for v in (2, 4, 4, 4, 5, 5, 7, 9)]
    assert population_stdev(values) == Decimal(2)


def test_squared_bandwidth_is_bandwidth_squared_without_a_root() -> None:
    assert squared_bandwidth(ALTERNATING, TWO) == Decimal("0.00000016") / (
        Decimal("1.1001") * Decimal("1.1001")
    )


@pytest.mark.parametrize("function", [simple_mean, population_stdev])
def test_an_empty_series_is_insufficient_history(function: object) -> None:
    with pytest.raises(InsufficientHistoryError):
        function([])  # type: ignore[operator]


@pytest.mark.parametrize("function", [squared_bandwidth])
def test_an_empty_band_is_insufficient_history(function: object) -> None:
    with pytest.raises(InsufficientHistoryError):
        function([], TWO)  # type: ignore[operator]
