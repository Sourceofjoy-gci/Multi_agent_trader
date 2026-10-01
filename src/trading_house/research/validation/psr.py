"""The probabilistic and deflated Sharpe ratios (umbrella 7.4; spec R-2, R-5, S-9).

Measurements, not gates: nothing here compares a value to 0.95.

R-2: ``z`` uses the PER-DAY Sharpe, because the formula's ``n`` counts days. Annualising
it by ``sqrt(365)`` while ``n`` counts days would scale ``z`` by about 19 and read ~1
for almost any series. The annualised figure is reported separately, as a diagnostic.

R-5: the DSR benchmark is ``SR0 = E[max Z] * max(SE, cross-section)``: the estimator's
own standard error ``SE = sqrt(variance_term / (n - 1))`` or, when at least two candidate
Sharpes are supplied, their sample standard deviation if that is larger. With the
standard error alone this is ``z = sqrt(n - 1) * sr_d / sqrt(variance_term) - E[max Z]``.
It reduces to PSR at ``N = 1`` (``E[max Z] = 0``).

R-7: ``E[max Z]`` uses the published Euler-Mascheroni weights, not the umbrella's 1/N.

Horizon: only ``horizon_days == 1`` is implemented; any other, or an unknown one, is undefined.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from statistics import NormalDist, StatisticsError, stdev
from typing import Literal, Self

from pydantic import NonNegativeInt, model_validator

from trading_house.core.values import (
    CanonicalModel,
    FiniteFloat,
    NonEmptyStr,
    NonNegativeFiniteFloat,
)
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


EULER_MASCHERONI = 0.5772156649015329
"""The weight of the second term of ``E[max Z]`` in Bailey-Lopez de Prado (R-7)."""


def expected_max_z(trials: int) -> float | None:
    """``E[max Z]`` of ``trials`` independent standard normals; ``None`` for ``trials <= 0``.

    ``(1 - gamma) * Phi^-1(1 - 1/N) + gamma * Phi^-1(1 - 1/(N e))`` with ``gamma`` the
    Euler-Mascheroni constant (R-7). ``Phi^-1(1 - p)`` is computed as ``-Phi^-1(p)``: the
    same number, without the rounding of ``1 - p`` that would make a very large ``N``
    unusable.
    """

    if trials <= 0:
        return None
    if trials == 1:
        return 0.0
    first = 1 / trials
    try:
        return (1.0 - EULER_MASCHERONI) * -_NORMAL.inv_cdf(
            first
        ) + EULER_MASCHERONI * -_NORMAL.inv_cdf(first / math.e)
    except StatisticsError:
        return None  # N so large that 1/N is not a representable probability


class DsrResult(CanonicalModel):
    """The DSR measurement and the inputs that fixed its ``N`` and its benchmark (S-9, R-5)."""

    dsr: Measurement
    selection_lotteries: NonNegativeInt
    effective_specifications: NonNegativeInt
    trials: NonNegativeInt
    """``max(selection_lotteries, effective_specifications)``."""
    expected_max_z: FiniteFloat | None
    horizon_days: int | None
    """The declared holding horizon in days; ``None`` when it could not be read."""
    chain_head_sha256: NonEmptyStr
    """The digest of the chain's last record when the counters were read."""
    standard_error: NonNegativeFiniteFloat | None
    """The estimator's own standard error ``sqrt(variance_term / (n - 1))``, when computable."""
    cross_section: NonNegativeFiniteFloat | None
    """The sample standard deviation of the candidates' per-day Sharpes, when 2+ were given."""
    cross_section_count: NonNegativeInt
    """How many candidate Sharpes entered the cross-section (a candidate with no defined
    Sharpe, such as a constant series, is not counted but is still ranked by PBO)."""
    dispersion_used: Literal["standard_error", "cross_section"] | None
    """Which of the two scaled ``E[max Z]``: the larger; ``None`` when no benchmark was formed."""

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.trials != max(self.selection_lotteries, self.effective_specifications):
            raise ValueError("trials must be the larger of the two counters")
        if (self.dispersion_used is None) != (self.standard_error is None):
            raise ValueError("a dispersion is used exactly when a standard error was computed")
        if self.dispersion_used == "cross_section" and (
            self.cross_section is None
            or self.standard_error is None
            or self.cross_section <= self.standard_error
        ):
            raise ValueError("the cross-section is used only when it exceeds the standard error")
        if self.dispersion_used == "standard_error" and (
            self.standard_error is None
            or (self.cross_section is not None and self.cross_section > self.standard_error)
        ):
            raise ValueError("the standard error is used unless the cross-section exceeds it")
        if (self.cross_section is not None) != (self.cross_section_count >= 2):
            raise ValueError("a cross-section exists exactly when two or more Sharpes entered it")
        return self


def dsr(
    series: ReturnSeries,
    *,
    trials: TrialCounters,
    horizon_days: int | None,
    chain_head_sha256: str,
    candidate_sharpes: Sequence[float] = (),
    horizon_reason: str | None = None,
) -> DsrResult:
    """``Phi(sqrt(n - 1) * (sr_d - SR0) / sqrt(variance_term))``, ``SR0 = E[max Z] * spread``.

    ``spread`` is the larger of the estimator's standard error and, when at least two
    ``candidate_sharpes`` (per-day, finite) are given, their sample standard deviation.
    """

    if not all(math.isfinite(x) for x in candidate_sharpes):
        raise refusal("the candidate Sharpes must be finite")
    n = max(trials.selection_lotteries, trials.effective_specifications)
    emz = expected_max_z(n)
    cross = stdev(candidate_sharpes) if len(candidate_sharpes) >= 2 else None

    def result(outcome: float | str, se: float | None = None, used: str | None = None) -> DsrResult:
        return DsrResult(
            dsr=_measurement("dsr", series, outcome),
            selection_lotteries=trials.selection_lotteries,
            effective_specifications=trials.effective_specifications,
            trials=n,
            expected_max_z=emz,
            horizon_days=horizon_days,
            chain_head_sha256=chain_head_sha256,
            standard_error=se,
            cross_section=cross,
            cross_section_count=len(candidate_sharpes),
            dispersion_used=used,  # type: ignore[arg-type]
        )

    if emz is None:
        return result(f"the trial count N is {n}, which has no expected maximum")
    if horizon_days is None:
        return result(
            "the declared holding horizon is not known"
            + (f": {horizon_reason}" if horizon_reason else ", so DSR cannot be formed")
        )
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
    error = 1.0 / scale
    use_cross = cross is not None and cross > error
    spread = cross if use_cross and cross is not None else error
    z = scale * (m.sharpe - emz * spread)
    return result(_probability(z), error, "cross_section" if use_cross else "standard_error")
