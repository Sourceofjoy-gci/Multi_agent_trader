"""Maximum drawdown, on marks and on compounded daily returns (umbrella 7.7).

A drawdown here is the largest peak-to-trough fall as a fraction of the running peak.
It is measured on mark-to-market equity, open-position marks included, and never on
closed trades: a position that is under water for a week and recovers before its exit
has a drawdown, and a closed-trade curve would not show it. A bundle with no equity
series therefore has no drawdown, and says so rather than falling back to trades.

A measurement, not a gate: nothing here compares the figure with anything.
"""

from __future__ import annotations

import numpy as np
import numpy.typing as npt

from trading_house.research.backtest.mark import EquitySeries
from trading_house.research.validation.measurement import Measurement
from trading_house.research.validation.series import ReturnSeries, _floats, refusal


def max_drawdown_fraction(equity: npt.NDArray[np.float64]) -> float:
    """``max((peak - equity) / peak)`` over a strictly positive, finite equity curve."""

    if not isinstance(equity, np.ndarray):
        raise refusal("a drawdown needs a numpy equity curve")
    if equity.ndim != 1:
        raise refusal("a drawdown needs a one-dimensional equity curve")
    if equity.dtype != np.float64:
        raise refusal("a drawdown needs a float64 equity curve")
    if len(equity) == 0:
        raise refusal("a drawdown needs at least one equity point")
    if not bool(np.isfinite(equity).all()):
        raise refusal("a drawdown needs finite equity")
    if not bool((equity > 0.0).all()):
        raise refusal("a drawdown needs strictly positive equity")
    peak = np.maximum.accumulate(equity)
    return float(((peak - equity) / peak).max())


def equity_drawdown(
    series: EquitySeries | None,
    *,
    name: str,
    evidence_sha256: str,
    basis_is_mark_to_market: bool,
) -> Measurement:
    """The drawdown of the initial equity followed by every bar's marked equity."""

    digests = (evidence_sha256,)
    if series is None:
        return Measurement.undefined(
            name,
            "the bundle carries no mark-to-market equity series",
            evidence_sha256=digests,
            basis_is_mark_to_market=basis_is_mark_to_market,
        )
    curve = _floats([series.firm_equity, *(point.equity for point in series.observations)])
    return Measurement.defined(
        name,
        max_drawdown_fraction(curve),
        evidence_sha256=digests,
        basis_is_mark_to_market=basis_is_mark_to_market,
    )


def path_drawdown(path_returns: ReturnSeries, *, name: str) -> Measurement:
    """The drawdown of the path's daily returns compounded from 1.0.

    The path's own start is 1.0; the absolute P&L path is not reset between paths (7.3).
    Undefined when compounding reaches zero or below, or overflows: there is then no
    positive equity to measure a fraction of.
    """

    digests, grade = (path_returns.evidence_sha256,), path_returns.promotion_grade
    with np.errstate(all="ignore"):
        curve = np.concatenate(([1.0], np.cumprod(1.0 + path_returns.values)))
    if not bool(np.isfinite(curve).all()) or not bool((curve > 0.0).all()):
        return Measurement.undefined(
            name,
            "the compounded daily returns reach zero or below, or overflow",
            evidence_sha256=digests,
            basis_is_mark_to_market=grade,
        )
    return Measurement.defined(
        name,
        max_drawdown_fraction(curve),
        evidence_sha256=digests,
        basis_is_mark_to_market=grade,
    )
