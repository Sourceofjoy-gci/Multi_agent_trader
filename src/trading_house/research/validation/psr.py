"""The probabilistic and deflated Sharpe ratios (umbrella 7.4; spec R-2, R-5, S-9).

Measurements, not gates: nothing here compares a value to 0.95.

R-2: ``z`` uses the PER-DAY Sharpe, because the formula's ``n`` counts days. Annualising
it by ``sqrt(365)`` while ``n`` counts days would scale ``z`` by about 19 and read ~1
for almost any series. The annualised figure is reported separately, as a diagnostic.

R-5: the DSR benchmark is ``E[max Z]`` scaled by the estimator's own standard error,
``z = sqrt(n - 1) * sr_d / sqrt(variance_term) - E[max Z]``. That needs no
cross-sectional Sharpe variance (none exists for one candidate) and reduces to PSR at
``N = 1``. Horizon: only ``horizon_days == 1`` is implemented; any other is undefined.
"""

from __future__ import annotations

import math
from statistics import NormalDist, StatisticsError

from pydantic import NonNegativeInt

from trading_house.core.values import CanonicalModel, FiniteFloat, NonEmptyStr
from trading_house.research.trial_ledger import TrialCounters
from trading_house.research.validation.measurement import Measurement
from trading_house.research.validation.moments import Moments, NoMoments, moments
from trading_house.research.validation.series import ReturnSeries, refusal

ANNUALISATION_DAYS = 365
"""The series holds calendar days, weekends included (umbrella 7.4)."""

_NORMAL = NormalDist()


def phi(x: float) -> float:
    """The standard normal CDF."""

    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def variance_term(m: Moments) -> float:
    """``1 - skew*SR + ((kurt - 1)/4)*SR^2``; may be non-finite or non-positive."""

    return 1.0 - m.skew * m.sharpe + ((m.kurt - 1.0) / 4.0) * (m.sharpe * m.sharpe)


def scale_of(m: Moments) -> float | str:
    """``sqrt(n - 1) / sqrt(variance_term)``, or the reason it is undefined."""

    term = variance_term(m)
    if not math.isfinite(term):
        return "the variance term is not finite"
    if term <= 0.0:
        return "the variance term is not positive"
    return math.sqrt(m.n - 1) / math.sqrt(term)


def _measurement(name: str, series: ReturnSeries, outcome: float | str) -> Measurement:
    digests, grade = (series.evidence_sha256,), series.promotion_grade
    if isinstance(outcome, str):
        return Measurement.undefined(
            name, outcome, evidence_sha256=digests, basis_is_mark_to_market=grade
        )
    return Measurement.defined(
        name, outcome, evidence_sha256=digests, basis_is_mark_to_market=grade
    )


def _probability(z: float) -> float | str:
    return phi(z) if math.isfinite(z) else "the standardised statistic is not finite"


def sharpe_annualised(series: ReturnSeries) -> Measurement:
    """A diagnostic only (R-2): ``mean/std * sqrt(365)``. No statistic is computed from it."""

    m = moments(series.values)
    if isinstance(m, NoMoments):
        return _measurement("sharpe_annualised", series, m.reason)
    return _measurement("sharpe_annualised", series, m.sharpe * math.sqrt(ANNUALISATION_DAYS))


def psr(series: ReturnSeries, *, benchmark_sr_d: float = 0.0) -> Measurement:
    """``Phi(sqrt(n - 1) * (sr_d - benchmark) / sqrt(variance_term))`` with the per-day ``sr_d``."""

    if not math.isfinite(benchmark_sr_d):
        raise refusal("the PSR benchmark must be finite")
    m = moments(series.values)
    if isinstance(m, NoMoments):
        return _measurement("psr", series, m.reason)
    scale = scale_of(m)
    if isinstance(scale, str):
        return _measurement("psr", series, scale)
    return _measurement("psr", series, _probability(scale * (m.sharpe - benchmark_sr_d)))


def expected_max_z(trials: int) -> float | None:
    """``E[max Z]`` of ``trials`` independent standard normals; ``None`` for ``trials <= 0``.

    ``Phi^-1(1 - p)`` is computed as ``-Phi^-1(p)``: the same number, without the
    rounding of ``1 - p`` that would make a very large ``N`` unusable.
    """

    if trials <= 0:
        return None
    if trials == 1:
        return 0.0
    alpha = 1 / trials
    try:
        return (1.0 - alpha) * -_NORMAL.inv_cdf(alpha) + alpha * -_NORMAL.inv_cdf(alpha / math.e)
    except StatisticsError:
        return None  # N so large that 1/N is not a representable probability


class DsrResult(CanonicalModel):
    """The DSR measurement and the inputs that fixed its ``N`` (S-9)."""

    dsr: Measurement
    selection_lotteries: NonNegativeInt
    effective_specifications: NonNegativeInt
    trials: NonNegativeInt
    """``max(selection_lotteries, effective_specifications)``."""
    expected_max_z: FiniteFloat | None
    horizon_days: int
    chain_head_sha256: NonEmptyStr
    """The digest of the chain's last record when the counters were read."""


def dsr(
    series: ReturnSeries,
    *,
    trials: TrialCounters,
    horizon_days: int,
    chain_head_sha256: str,
) -> DsrResult:
    n = max(trials.selection_lotteries, trials.effective_specifications)
    emz = expected_max_z(n)

    def result(outcome: float | str) -> DsrResult:
        return DsrResult(
            dsr=_measurement("dsr", series, outcome),
            selection_lotteries=trials.selection_lotteries,
            effective_specifications=trials.effective_specifications,
            trials=n,
            expected_max_z=emz,
            horizon_days=horizon_days,
            chain_head_sha256=chain_head_sha256,
        )

    if emz is None:
        return result(f"the trial count N is {n}, which has no expected maximum")
    if horizon_days != 1:
        return result(
            f"horizon {horizon_days} days: multi-day non-overlap handling is not implemented"
        )
    m = moments(series.values)
    if isinstance(m, NoMoments):
        return result(m.reason)
    scale = scale_of(m)
    if isinstance(scale, str):
        return result(scale)
    return result(_probability(scale * m.sharpe - emz))
