"""Phase 8C3: maximum drawdown.

Every expected value is worked out here by hand, from peaks and troughs on tiny curves.
The values are dyadic where a decimal fraction would make float equality accidental.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import numpy as np
import pytest

from trading_house.core.errors import StatisticalInputError
from trading_house.research.backtest.mark import EquityObservation, EquitySeries
from trading_house.research.trial_ledger import ReturnSeriesBasis
from trading_house.research.validation.drawdown import (
    NOT_MARKED,
    equity_drawdown,
    max_drawdown_fraction,
    path_drawdown,
)
from trading_house.research.validation.series import ReturnSeries

DIGEST = "d" * 64


def _refused(error: pytest.ExceptionInfo[StatisticalInputError], fragment: str) -> None:
    assert str(error.value) == StatisticalInputError.public_message
    cause = error.value.__cause__
    assert isinstance(cause, ValueError)
    assert fragment in str(cause)


def _curve(*points: float) -> np.ndarray:
    return np.array(points, dtype=np.float64)


# --- the fraction ----------------------------------------------------------------


def test_the_largest_fall_from_the_running_peak_is_the_drawdown() -> None:
    # 1, 2, 1, 3, 1.5: the peaks are 1, 2, 2, 3, 3 and the falls 0, 0, 1/2, 0, 1/2.
    assert max_drawdown_fraction(_curve(1, 2, 1, 3, 1.5)) == 0.5
    # 100, 90, 95, 80: peak 100 throughout; the falls are 10%, 5% and 20%, and 20% is the max.
    assert max_drawdown_fraction(_curve(100, 90, 95, 80)) == pytest.approx(0.2)


def test_the_peak_is_the_running_maximum_and_not_the_first_point() -> None:
    # 1, 2, 1: measured from the first point (1) there is no fall; from the peak (2) it is 1/2.
    assert max_drawdown_fraction(_curve(1, 2, 1)) == 0.5


def test_the_trough_is_the_deepest_and_not_the_last_point() -> None:
    # 4, 1, 4: it falls 3/4 and recovers; a last-point reading would say 0.
    assert max_drawdown_fraction(_curve(4, 1, 4)) == 0.75


def test_a_curve_that_never_falls_has_no_drawdown_and_a_single_point_too() -> None:
    assert max_drawdown_fraction(_curve(1, 2, 3)) == 0.0
    assert max_drawdown_fraction(_curve(5)) == 0.0


@pytest.mark.parametrize(
    ("equity", "fragment"),
    [
        ([1.0, 2.0], "numpy"),  # not an ndarray
        (np.ones((2, 2)), "one-dimensional"),
        (np.array([1, 2], dtype=np.int64), "float64"),
        (np.array([], dtype=np.float64), "at least one"),
        (np.array([1.0, np.nan]), "finite"),
        (np.array([1.0, np.inf]), "finite"),
        (np.array([1.0, 0.0]), "strictly positive"),
        (np.array([1.0, -3.0]), "strictly positive"),
    ],
)
def test_a_malformed_curve_is_refused_with_its_own_reason(equity: object, fragment: str) -> None:
    with pytest.raises(StatisticalInputError) as error:
        max_drawdown_fraction(equity)  # type: ignore[arg-type]

    _refused(error, fragment)


# --- mark-to-market equity -----------------------------------------------------------


def _at(hour: int) -> datetime:
    return datetime(2026, 3, 2, hour, tzinfo=UTC)


def _obs(hour: int, realized: str, unrealized: str, open_positions: int) -> EquityObservation:
    return EquityObservation(
        marked_at=_at(hour),
        equity=Decimal(1000) + Decimal(realized) + Decimal(unrealized),
        cumulative_realized_pnl=Decimal(realized),
        unrealized_pnl=Decimal(unrealized),
        open_positions=open_positions,
    )


def _series(*observations: EquityObservation, firm: str = "1000") -> EquitySeries:
    return EquitySeries(firm_equity=Decimal(firm), observations=observations)


def test_an_open_position_that_dips_and_recovers_before_its_exit_has_a_marked_drawdown() -> None:
    """The 7.7 / 11.3 clause. Firm equity 1000. A position opens (nothing yet unrealized),
    is marked 80 under water (equity 920), recovers to its entry (1000) and exits for +50
    (equity 1050, flat).

    Trades only: the book's realized equity is 1000 until the exit and 1050 after, so
    its curve is 1000, 1000, 1050 and its drawdown 0. Marked to market the peak is 1000 and
    the trough 920: (1000 - 920) / 1000 = 0.08.
    """

    series = _series(
        _obs(9, "0", "0", 1),
        _obs(10, "0", "-80", 1),
        _obs(11, "0", "0", 1),
        _obs(12, "50", "0", 0),
    )

    marked = equity_drawdown(series, name="d", evidence_sha256=DIGEST, basis_is_mark_to_market=True)

    assert marked.value == 0.08
    assert marked.evidence_sha256 == (DIGEST,)
    assert marked.basis_is_mark_to_market is True
    assert max_drawdown_fraction(_curve(1000, 1000, 1050)) == 0.0  # the trades-only curve


def test_the_initial_equity_is_a_point_of_the_curve() -> None:
    # Firm equity 1000, then the first mark is 900: without the starting point the curve is
    # 900, 1000 and falls 0; with it the peak is 1000 and the fall is 100 / 1000 = 0.1.
    series = _series(_obs(9, "0", "-100", 1), _obs(10, "0", "0", 1))

    result = equity_drawdown(series, name="d", evidence_sha256=DIGEST, basis_is_mark_to_market=True)

    assert result.value == 0.1


def test_the_name_the_digest_and_the_basis_are_the_callers() -> None:
    series = _series(_obs(9, "0", "0", 0))

    result = equity_drawdown(
        series, name="max_drawdown_x", evidence_sha256="f" * 64, basis_is_mark_to_market=True
    )

    assert (result.name, result.evidence_sha256, result.basis_is_mark_to_market) == (
        "max_drawdown_x",
        ("f" * 64,),
        True,
    )
    assert result.value == 0.0


def test_a_series_on_a_closed_trades_basis_has_no_drawdown_even_though_it_is_present() -> None:
    series = _series(_obs(9, "0", "-100", 1))  # a 10% dip, were it read

    result = equity_drawdown(
        series, name="max_drawdown_x", evidence_sha256="f" * 64, basis_is_mark_to_market=False
    )

    assert result.value is None
    assert result.undefined_reason == NOT_MARKED
    assert (result.name, result.evidence_sha256, result.basis_is_mark_to_market) == (
        "max_drawdown_x",
        ("f" * 64,),
        False,
    )


def test_a_bundle_without_an_equity_series_has_no_drawdown_and_never_a_trades_one() -> None:
    result = equity_drawdown(
        None, name="max_drawdown_baseline", evidence_sha256=DIGEST, basis_is_mark_to_market=True
    )

    assert result.value is None
    assert result.undefined_reason == "the bundle carries no mark-to-market equity series"
    assert result.name == "max_drawdown_baseline"
    assert result.evidence_sha256 == (DIGEST,)
    assert result.basis_is_mark_to_market is True


def test_a_series_whose_equity_is_not_positive_is_refused() -> None:
    ruined = _series(_obs(9, "-1000", "0", 0))  # equity 1000 - 1000 = 0

    with pytest.raises(StatisticalInputError) as error:
        equity_drawdown(ruined, name="d", evidence_sha256=DIGEST, basis_is_mark_to_market=True)

    _refused(error, "strictly positive")


# --- compounded daily returns -------------------------------------------------------


def _returns(values: list[float], basis: ReturnSeriesBasis = ReturnSeriesBasis.MARK_TO_MARKET):
    first = date(2024, 1, 1)
    return ReturnSeries(
        days=tuple(first + timedelta(days=i) for i in range(len(values))),
        values=np.array(values, dtype=np.float64),
        basis=basis,
        evidence_sha256=DIGEST,
    )


def _padded(*head: float) -> list[float]:
    return [*head, *([0.0] * (30 - len(head)))]


def test_flat_returns_have_no_drawdown() -> None:
    result = path_drawdown(_returns(_padded()), name="max_drawdown_cpcv_path_0")

    assert result.value == 0.0
    assert result.name == "max_drawdown_cpcv_path_0"


def test_returns_compound_from_one_so_an_opening_fall_is_a_drawdown() -> None:
    # 1 -> 0.5 -> 0.5 ...: from the path's own start of 1.0 the fall is 1/2. Without the
    # starting 1.0 the curve is constant 0.5 and the drawdown would be 0.
    assert path_drawdown(_returns(_padded(-0.5)), name="n").value == 0.5


def test_returns_compound_and_the_deepest_fall_from_the_running_peak_is_taken() -> None:
    # +100% then -50%: 1 -> 2 -> 1, a fall of 1/2 from the peak 2.
    assert path_drawdown(_returns(_padded(1.0, -0.5)), name="n").value == 0.5
    # -50% then +100%: 1 -> 0.5 -> 1, again 1/2, and the recovery does not erase it.
    assert path_drawdown(_returns(_padded(-0.5, 1.0)), name="n").value == 0.5
    # -25% twice: 1 -> 0.75 -> 0.5625, a fall of 0.4375 = 7/16, exact in binary.
    assert path_drawdown(_returns(_padded(-0.25, -0.25)), name="n").value == 0.4375


def test_a_path_on_a_closed_trades_basis_has_no_drawdown() -> None:
    realized = path_drawdown(
        _returns(_padded(-0.5), ReturnSeriesBasis.REALIZED_CLOSED_TRADES), name="n"
    )

    assert realized.value is None
    assert realized.undefined_reason == NOT_MARKED
    assert realized.evidence_sha256 == (DIGEST,)
    assert realized.basis_is_mark_to_market is False


def test_evidence_and_basis_ride_along() -> None:
    assert path_drawdown(_returns(_padded()), name="n").basis_is_mark_to_market is True


def test_compounding_that_reaches_zero_is_undefined_with_its_reason() -> None:
    # A -100% day takes equity to 0: there is no positive equity to take a fraction of.
    result = path_drawdown(_returns(_padded(-1.0)), name="n")

    assert result.value is None
    assert "reach zero or below" in (result.undefined_reason or "")
    assert result.evidence_sha256 == (DIGEST,)


def test_compounding_that_overflows_is_undefined() -> None:
    result = path_drawdown(_returns([1e300] * 30), name="n")

    assert result.value is None
    assert "overflow" in (result.undefined_reason or "")
