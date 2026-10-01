"""Phase 8C2: PSR, DSR and E[max Z] against an independent reference.

Every expected number below was produced by this standalone script, which imports
nothing from this repository (``math`` and ``statistics.NormalDist`` only). Re-run it
to check the literals; it was not derived by calling the code under test.

```python
import math
from statistics import NormalDist

N01 = NormalDist()


def series(n, drift, scale):
    return [drift + scale * (math.sin(1.7 * i) + 0.5 * math.cos(0.23 * i * i)) for i in range(n)]


def skewed(n, shift, scale):
    return [scale * (math.exp(math.sin(1.7 * i) + 0.5 * math.cos(0.23 * i * i)) - shift)
            for i in range(n)]


def stats(x):
    n = len(x)
    mean = sum(x) / n
    m2 = sum((v - mean) ** 2 for v in x) / n
    m3 = sum((v - mean) ** 3 for v in x) / n
    m4 = sum((v - mean) ** 4 for v in x) / n
    std = math.sqrt(sum((v - mean) ** 2 for v in x) / (n - 1))
    sr = mean / std                       # per-day Sharpe (R-2)
    skew = m3 / m2 ** 1.5
    kurt = m4 / m2 ** 2
    vt = 1 - skew * sr + (kurt - 1) / 4 * sr * sr
    return n, mean, std, sr, skew, kurt, vt


def emz(n):
    if n == 1:
        return 0.0
    a = 1 / n
    return (1 - a) * N01.inv_cdf(1 - 1 / n) + a * N01.inv_cdf(1 - 1 / (n * math.e))


def psr(x, bench=0.0):
    n, mean, std, sr, skew, kurt, vt = stats(x)
    return N01.cdf(math.sqrt(n - 1) * (sr - bench) / math.sqrt(vt))


def dsr(x, trials):       # R-5: E[max Z] subtracted on the standardised scale
    n, mean, std, sr, skew, kurt, vt = stats(x)
    return N01.cdf(math.sqrt(n - 1) * sr / math.sqrt(vt) - emz(trials))


CASES = (("A", series(500, 0.0004, 0.004)),
         ("C", series(500, 0.0001, 0.004)),
         ("D", skewed(250, 1.25, 0.002)))
```

The literal-annualised PSR (``sr * sqrt(365)`` inside ``z``) of series C is 0.99965; the
per-day PSR is 0.57038. That gap is the R-2 guard below.
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from itertools import pairwise

import numpy as np
import pytest

from trading_house.core.errors import StatisticalInputError
from trading_house.research.trial_ledger import ReturnSeriesBasis, TrialCounters
from trading_house.research.validation.moments import Moments
from trading_house.research.validation.psr import (
    dsr,
    expected_max_z,
    phi,
    psr,
    scale_of,
    sharpe_annualised,
    variance_term,
)
from trading_house.research.validation.series import ReturnSeries

DIGEST = "e" * 64
HEAD = "f" * 64
EXACT = 1e-9


def _series(
    values: list[float],
    basis: ReturnSeriesBasis = ReturnSeriesBasis.MARK_TO_MARKET,
) -> ReturnSeries:
    first = date(2024, 1, 1)
    return ReturnSeries(
        days=tuple(first + timedelta(days=i) for i in range(len(values))),
        values=np.array(values, dtype=np.float64),
        basis=basis,
        evidence_sha256=DIGEST,
    )


def _wave(n: int, drift: float, scale: float) -> list[float]:
    return [drift + scale * (math.sin(1.7 * i) + 0.5 * math.cos(0.23 * i * i)) for i in range(n)]


def _skewed(n: int, shift: float, scale: float) -> list[float]:
    return [
        scale * (math.exp(math.sin(1.7 * i) + 0.5 * math.cos(0.23 * i * i)) - shift)
        for i in range(n)
    ]


A = _series(_wave(500, 0.0004, 0.004))
C = _series(_wave(500, 0.0001, 0.004))
D = _series(_skewed(250, 1.25, 0.002))


def _counters(lotteries: int, effective: int, audit: int = 0) -> TrialCounters:
    return TrialCounters(
        audit_attempts=audit, selection_lotteries=lotteries, effective_specifications=effective
    )


def _value(measurement: object) -> float:
    value = getattr(measurement, "value")  # noqa: B009
    assert isinstance(value, float)
    return value


def _dsr(series: ReturnSeries, lotteries: int, effective: int, **kwargs: object) -> float:
    result = dsr(
        series,
        trials=_counters(lotteries, effective),
        horizon_days=1,
        chain_head_sha256=HEAD,
        **kwargs,  # type: ignore[arg-type]
    )
    return _value(result.dsr)


def test_phi_is_the_standard_normal_cdf() -> None:
    assert phi(0.0) == 0.5
    assert phi(1.0) == pytest.approx(0.8413447460685429, rel=1e-14)
    assert phi(-1.96) == pytest.approx(0.024997895148220435, rel=1e-12)


def test_the_variance_term_is_the_umbrella_expression() -> None:
    m = Moments(n=100, mean=0.0, std=1.0, sharpe=0.5, skew=2.0, kurt=7.0)

    # 1 - 2.0 * 0.5 + (7 - 1) / 4 * 0.25 = 1 - 1 + 0.375
    assert variance_term(m) == pytest.approx(0.375, rel=1e-14)


# --- PSR -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("series", "expected"),
    [(A, 0.9894070330303317), (C, 0.5703830736648368), (D, 0.8742416096561596)],
)
def test_psr_matches_the_independent_reference(series: ReturnSeries, expected: float) -> None:
    assert _value(psr(series)) == pytest.approx(expected, rel=EXACT)


def test_psr_against_a_nonzero_benchmark_matches_the_reference() -> None:
    assert _value(psr(A, benchmark_sr_d=0.02)) == pytest.approx(0.9684666299491733, rel=EXACT)
    assert _value(psr(C, benchmark_sr_d=0.02)) == pytest.approx(0.3938133991605599, rel=EXACT)
    assert _value(psr(D, benchmark_sr_d=0.02)) == pytest.approx(0.7935405689133047, rel=EXACT)


def test_r2_psr_of_a_modest_edge_500_day_series_is_clearly_below_one() -> None:
    """The R-2 guard. Series C has a per-day Sharpe of 0.0079 (0.15 annualised).

    Annualising ``sr`` while ``n`` counts days gives z about 19 times larger and
    PSR = 0.99965 -- 'near certain' for an edge this thin. The per-day figure is 0.570.
    """

    value = _value(psr(C))

    assert value < 0.6
    assert value == pytest.approx(0.5703830736648368, rel=EXACT)
    assert abs(value - 0.9996483095337845) > 0.4  # the literal annualised formula's answer


def test_the_annualised_sharpe_is_reported_as_a_diagnostic_and_not_used() -> None:
    assert _value(sharpe_annualised(C)) == pytest.approx(0.1516870700941914, rel=EXACT)
    assert _value(sharpe_annualised(A)) == pytest.approx(1.9747489378702343, rel=EXACT)
    assert sharpe_annualised(C).name == "sharpe_annualised"


def test_psr_rises_with_the_mean() -> None:
    drifts = (-0.0004, 0.0, 0.0001, 0.0004, 0.001)
    values = [_value(psr(_series(_wave(500, drift, 0.004)))) for drift in drifts]

    assert values == sorted(values)
    assert len(set(values)) == len(values)


def test_psr_of_a_zero_mean_series_is_exactly_one_half() -> None:
    assert _value(psr(_series([0.01, -0.01] * 15))) == 0.5


def test_a_measurement_carries_the_evidence_digest_and_the_basis_grade() -> None:
    graded = psr(A)
    legacy = psr(_series(_wave(500, 0.0004, 0.004), ReturnSeriesBasis.REALIZED_CLOSED_TRADES))

    assert graded.name == "psr"
    assert graded.evidence_sha256 == (DIGEST,)
    assert graded.basis_is_mark_to_market is True
    assert legacy.basis_is_mark_to_market is False
    assert legacy.value == graded.value  # the basis labels a value, it does not change it


def test_a_constant_series_has_no_psr_dsr_or_annualised_sharpe() -> None:
    flat = _series([0.001] * 40)

    for item in (
        psr(flat),
        sharpe_annualised(flat),
        dsr(flat, trials=_counters(3, 3), horizon_days=1, chain_head_sha256=HEAD).dsr,
    ):
        assert item.value is None
        assert item.undefined_reason is not None
        assert "constant" in item.undefined_reason
        assert item.evidence_sha256 == (DIGEST,)


def test_a_standardised_statistic_that_overflows_is_undefined_not_infinite() -> None:
    item = psr(A, benchmark_sr_d=1e308)

    assert item.value is None
    assert item.undefined_reason == "the standardised statistic is not finite"


def test_a_non_finite_benchmark_is_refused() -> None:
    with pytest.raises(StatisticalInputError) as error:
        psr(A, benchmark_sr_d=math.nan)

    assert "benchmark must be finite" in str(error.value.__cause__)


# --- the variance term ----------------------------------------------------------------


def test_a_non_positive_variance_term_is_undefined() -> None:
    # 1 - 2 * 1 + (2 - 1) / 4 * 4 = 0: the boundary is undefined, not infinite
    zero = Moments(n=100, mean=0.1, std=1.0, sharpe=2.0, skew=1.0, kurt=2.0)
    # 1 - 2 * 1 + (1 - 1) / 4 * 4 = -1
    negative = Moments(n=100, mean=0.1, std=1.0, sharpe=2.0, skew=1.0, kurt=1.0)

    assert variance_term(zero) == 0.0
    assert scale_of(zero) == "the variance term is not positive"
    assert scale_of(negative) == "the variance term is not positive"


def test_psr_and_dsr_are_undefined_when_the_data_give_a_non_positive_variance_term(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The term cannot reach zero from a real sample (it is at least (1 - k)^2 for the
    ddof factor k < 1) except by rounding, so the moments are substituted."""

    boundary = Moments(n=100, mean=0.1, std=1.0, sharpe=2.0, skew=1.0, kurt=2.0)
    monkeypatch.setattr("trading_house.research.validation.psr.moments", lambda values: boundary)

    graded = psr(A)
    deflated = dsr(A, trials=_counters(3, 3), horizon_days=1, chain_head_sha256=HEAD)

    assert graded.value is None
    assert graded.undefined_reason == "the variance term is not positive"
    assert deflated.dsr.value is None
    assert deflated.dsr.undefined_reason == "the variance term is not positive"
    assert deflated.expected_max_z is not None  # the trial count was fine; the sample was not


def test_a_non_finite_variance_term_is_undefined() -> None:
    huge = Moments(n=100, mean=0.1, std=1.0, sharpe=1e200, skew=1.0, kurt=3.0)
    nan = Moments(n=100, mean=0.1, std=1.0, sharpe=1e200, skew=1e200, kurt=3.0)

    assert scale_of(huge) == "the variance term is not finite"
    assert scale_of(nan) == "the variance term is not finite"


def test_the_scale_is_the_root_of_n_minus_one_over_the_root_of_the_variance_term() -> None:
    m = Moments(n=101, mean=0.0, std=1.0, sharpe=0.5, skew=2.0, kurt=7.0)

    assert scale_of(m) == pytest.approx(math.sqrt(100) / math.sqrt(0.375), rel=1e-14)


# --- E[max Z] -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("trials", "expected"),
    [
        (1, 0.0),
        (2, 0.45022629831889516),
        (3, 0.6744705091678826),
        (10, 1.3323205854483036),
        (100, 2.3298864997501014),
        (1000, 3.090517969252707),
    ],
)
def test_expected_max_z_matches_the_independent_reference(trials: int, expected: float) -> None:
    value = expected_max_z(trials)

    assert value is not None
    assert value == pytest.approx(expected, rel=1e-11, abs=1e-15)


@pytest.mark.parametrize("trials", [0, -1, 10**400])
def test_expected_max_z_is_undefined_outside_its_domain(trials: int) -> None:
    assert expected_max_z(trials) is None


# --- DSR ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("series", "trials", "expected"),
    [
        (A, 2, 0.9681610347106413),
        (A, 10, 0.834557758710663),
        (A, 100, 0.4899349359339036),
        (C, 2, 0.39247397034819487),
        (C, 10, 0.12405113860728795),
        (C, 100, 0.0156775431542312),
        (D, 2, 0.756925447372904),
        (D, 10, 0.426360579280324),
        (D, 100, 0.11836227815567679),
    ],
)
def test_dsr_matches_the_independent_reference(
    series: ReturnSeries, trials: int, expected: float
) -> None:
    assert _dsr(series, trials, trials) == pytest.approx(expected, rel=1e-8)


@pytest.mark.parametrize("series", [A, C, D])
def test_dsr_with_one_trial_is_the_psr_at_benchmark_zero(series: ReturnSeries) -> None:
    assert _dsr(series, 1, 1) == pytest.approx(_value(psr(series)), rel=1e-14)


@pytest.mark.parametrize("series", [A, C, D])
def test_dsr_strictly_decreases_as_the_trial_count_grows(series: ReturnSeries) -> None:
    values = [_dsr(series, n, n) for n in (1, 2, 3, 10, 100, 1000)]

    assert all(later < earlier for earlier, later in pairwise(values))


def test_dsr_divides_by_the_larger_of_the_two_counters_whichever_it_is() -> None:
    reference = 0.834557758710663  # series A at N = 10

    more_lotteries = dsr(
        A, trials=_counters(10, 3, audit=99), horizon_days=1, chain_head_sha256=HEAD
    )
    more_specifications = dsr(
        A, trials=_counters(3, 10, audit=99), horizon_days=1, chain_head_sha256=HEAD
    )
    equal = dsr(A, trials=_counters(10, 10), horizon_days=1, chain_head_sha256=HEAD)

    for result in (more_lotteries, more_specifications, equal):
        assert result.trials == 10
        assert _value(result.dsr) == pytest.approx(reference, rel=1e-8)
        assert result.expected_max_z == pytest.approx(1.3323205854483036, rel=1e-11)
    # The audit count is a governance diagnostic and never the denominator.
    assert (more_lotteries.selection_lotteries, more_lotteries.effective_specifications) == (10, 3)
    assert (
        more_specifications.selection_lotteries,
        more_specifications.effective_specifications,
    ) == (3, 10)


def test_dsr_records_the_chain_head_and_the_horizon_it_was_read_at() -> None:
    result = dsr(A, trials=_counters(3, 3), horizon_days=1, chain_head_sha256=HEAD)

    assert result.chain_head_sha256 == HEAD
    assert result.horizon_days == 1
    assert result.dsr.name == "dsr"
    assert result.dsr.evidence_sha256 == (DIGEST,)
    assert result.dsr.basis_is_mark_to_market is True


def test_dsr_with_no_trials_is_undefined() -> None:
    result = dsr(A, trials=_counters(0, 0, audit=5), horizon_days=1, chain_head_sha256=HEAD)

    assert result.dsr.value is None
    assert result.dsr.undefined_reason is not None
    assert "N is 0" in result.dsr.undefined_reason
    assert (result.trials, result.expected_max_z) == (0, None)


@pytest.mark.parametrize("horizon", [0, 2, 5, -1])
def test_dsr_over_a_multi_day_horizon_is_undefined_and_says_why(horizon: int) -> None:
    result = dsr(A, trials=_counters(3, 3), horizon_days=horizon, chain_head_sha256=HEAD)

    assert result.dsr.value is None
    assert result.dsr.undefined_reason is not None
    assert "multi-day non-overlap handling is not implemented" in result.dsr.undefined_reason
    assert result.horizon_days == horizon


def test_dsr_with_an_unrepresentable_trial_count_is_undefined() -> None:
    result = dsr(A, trials=_counters(10**400, 1), horizon_days=1, chain_head_sha256=HEAD)

    assert result.dsr.value is None
    assert result.expected_max_z is None
