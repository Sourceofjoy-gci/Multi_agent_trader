from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from trading_house.core.errors import EquityEvidenceError
from trading_house.research.backtest.mark import (
    EquityObservation,
    EquitySeries,
    derive_daily_returns,
)

_FIRM = Decimal("100000")
_BASE = datetime(2024, 1, 1, tzinfo=UTC)
# Bounded so a generated equity can never reach zero: 100,000 - 2*40,000 stays
# positive, which keeps the refusal case out of the identity and rectangular
# properties. The refusal has its own test below, where it is the subject.
_MONEY = st.decimals(min_value=-40000, max_value=40000, places=2, allow_nan=False)


def _series_from(pairs: list[tuple[int, Decimal, Decimal]]) -> EquitySeries:
    return EquitySeries(
        firm_equity=_FIRM,
        observations=tuple(
            EquityObservation(
                marked_at=_BASE + timedelta(minutes=index),
                equity=_FIRM + realized + unrealized,
                cumulative_realized_pnl=realized,
                unrealized_pnl=unrealized,
                # An open position is what makes an unrealized term possible, and
                # the series refuses the two disagreeing. Generated from the
                # unrealized rather than fixed so both sides get built.
                open_positions=0 if unrealized == 0 else 1,
            )
            for index, realized, unrealized in pairs
        ),
    )


@given(
    st.lists(st.tuples(st.integers(0, 400), _MONEY, _MONEY), min_size=1, max_size=25),
    st.integers(0, 24),
)
@settings(max_examples=50)
def test_the_identity_holds_for_every_point_and_the_validator_can_say_no(
    pairs: list[tuple[int, Decimal, Decimal]], target: int
) -> None:
    series = _series_from(
        [(index, realized, unrealized) for index, (_, realized, unrealized) in enumerate(pairs)]
    )

    for point in series.observations:
        assert point.equity == _FIRM + point.cumulative_realized_pnl + point.unrealized_pnl

    # The assertion above would hold with the identity rule deleted, since the
    # builder computes equity from the other two. This half is the test of the
    # rule: perturb one point's equity by a cent the identity cannot absorb and
    # the same series must be refused. A validator nobody can see fail is not
    # evidence of anything.
    index = target % len(series.observations)
    perturbed = list(series.observations)
    perturbed[index] = perturbed[index].model_copy(
        update={"equity": perturbed[index].equity + Decimal("0.01")}
    )

    with pytest.raises(ValidationError, match="firm equity"):
        EquitySeries(firm_equity=_FIRM, observations=tuple(perturbed))


@given(
    st.integers(0, 200),
    st.integers(0, 200),
    st.lists(st.tuples(st.integers(0, 2000), _MONEY, _MONEY), min_size=1, max_size=20),
)
@settings(max_examples=50)
def test_the_daily_series_is_always_rectangular(
    offset: int, span: int, pairs: list[tuple[int, Decimal, Decimal]]
) -> None:
    series = _series_from(
        [(index, realized, unrealized) for index, (_, realized, unrealized) in enumerate(pairs)]
    )
    first = date(2024, 1, 1) + timedelta(days=offset)

    points = derive_daily_returns(series, first_day=first, last_day=first + timedelta(days=span))

    assert len(points) == span + 1
    assert [point.day for point in points] == [first + timedelta(days=n) for n in range(span + 1)]


def test_a_series_driven_to_zero_is_refused_rather_than_divided_by() -> None:
    # firm_equity plus a realized loss of exactly -firm_equity is a close of
    # zero. The first day's denominator is still firm equity, so the refusal
    # lands on the second day, whose denominator is that close.
    series = _series_from([(0, -_FIRM, Decimal(0))])

    with pytest.raises(EquityEvidenceError):
        derive_daily_returns(
            series,
            first_day=_BASE.date(),
            last_day=_BASE.date() + timedelta(days=1),
        )
