"""Phase 8C2: the stationary bootstrap.

The resample indices for a tiny case are pinned against this reference, which uses only
``numpy.random`` and ``hashlib`` and imports nothing from this repository. It draws in the
order the module's docstring states (the first index, then per step the restart uniforms
and the fresh indices for every replicate) and decides each replicate in a plain loop.

```python
import hashlib
import numpy as np


def seed_of(spec, attempt, policy):
    return int.from_bytes(hashlib.sha256(f"{spec}|{attempt}|{policy}".encode()).digest()[:8], "big")


def reference(n, reps, length, seed):
    rng = np.random.Generator(np.random.PCG64(seed))
    rows = [[int(i)] for i in rng.integers(0, n, size=reps)]
    for _ in range(n - 1):
        u = rng.random(reps)
        fresh = rng.integers(0, n, size=reps)
        for r in range(reps):
            if u[r] < 1.0 / length:
                rows[r].append(int(fresh[r]))
            else:
                rows[r].append((rows[r][-1] + 1) % n)
    return rows
```

``reference(8, 4, 2, 0)`` is ``REFERENCE_ROWS`` below. Seed 0 was chosen by scanning for a
case that contains a wrap-around and restart uniforms inside ``[1/3, 1/2)`` and
``[1/2, 0.6)``, so a wrong restart probability or a missing ``% n`` changes it.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pytest

from trading_house.core.errors import StatisticalInputError
from trading_house.research.trial_ledger import ReturnSeriesBasis
from trading_house.research.validation.bootstrap import (
    POLICY_VERSION,
    BootstrapResult,
    block_length,
    bootstrap_mean,
    bootstrap_seed,
    percentile_bounds,
    stationary_bootstrap_indices,
    stationary_bootstrap_means,
)
from trading_house.research.validation.series import ReturnSeries

PROJECT_ROOT = Path(__file__).resolve().parents[4]
DIGEST = "e" * 64
REFERENCE_ROWS = [
    [6, 4, 5, 6, 2, 3, 3, 4],
    [5, 4, 5, 2, 4, 5, 6, 4],
    [4, 5, 6, 7, 0, 1, 2, 3],
    [2, 3, 0, 1, 2, 3, 7, 2],
]


def _refused(error: pytest.ExceptionInfo[StatisticalInputError], fragment: str) -> None:
    assert str(error.value) == StatisticalInputError.public_message
    cause = error.value.__cause__
    assert isinstance(cause, ValueError)
    assert fragment in str(cause)


def _series(
    values: list[float], basis: ReturnSeriesBasis = ReturnSeriesBasis.MARK_TO_MARKET
) -> ReturnSeries:
    first = date(2024, 1, 1)
    return ReturnSeries(
        days=tuple(first + timedelta(days=i) for i in range(len(values))),
        values=np.array(values, dtype=np.float64),
        basis=basis,
        evidence_sha256=DIGEST,
    )


def _wave(n: int) -> list[float]:
    return [0.0004 + 0.004 * (math.sin(1.7 * i) + 0.5 * math.cos(0.23 * i * i)) for i in range(n)]


# --- the seed ------------------------------------------------------------------------


def test_the_seed_is_the_first_eight_bytes_of_the_sha256_big_endian() -> None:
    # hashlib.sha256(b"spec|attempt|8c-sb-1").digest()[:8] read big-endian, computed outside
    assert bootstrap_seed("spec", "attempt", "8c-sb-1") == 17572605998880585980


def test_every_one_of_the_three_seed_inputs_changes_the_seed() -> None:
    base = bootstrap_seed("spec", "attempt", "8c-sb-1")

    assert bootstrap_seed("spec2", "attempt", "8c-sb-1") == 13698992997523543746
    assert bootstrap_seed("spec", "attempt2", "8c-sb-1") == 2329578073141903296
    assert bootstrap_seed("spec", "attempt", "8c-sb-2") == 17082911093591165786
    assert len({base, 13698992997523543746, 2329578073141903296, 17082911093591165786}) == 4
    assert bootstrap_seed("spec", "attempt", "8c-sb-1") == base


def test_a_separator_inside_a_seed_input_is_refused_because_it_would_collide() -> None:
    with pytest.raises(StatisticalInputError) as error:
        bootstrap_seed("a|b", "c", "d")

    _refused(error, "separator")


def test_the_policy_version_is_pinned() -> None:
    assert POLICY_VERSION == "8c-sb-1"


# --- the block length ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("n", "c", "expected"),
    [
        (100, 6.7, 22),  # 6.7 * (100/3)^(1/3) = 21.56 -> 22 (truncation would give 21)
        (30, 6.7, 14),  # 14.43 -> 14 (ceiling would give 15)
        (500, 6.7, 37),  # 36.87
        (1827, 6.7, 57),  # 56.79
        (36, 6.7, 15),  # 15.34
        (24, 1.25, 2),  # (24/3)^(1/3) = 2 exactly, so 2.5: half to even is 2 (half up: 3)
        (24, 1.75, 4),  # 3.5 -> 4
        (24, 2.25, 4),  # 4.5 -> 4 (half up: 5)
        (2, 6.7, 2),  # 5.85 -> 6, capped at n = 2
        (5, 6.7, 5),  # 7.94 -> 8, capped at n = 5
        (30, 0.1, 2),  # 0.215 -> 0, floored at 2
        (3, 0.01, 2),
    ],
)
def test_block_length_known_answers(n: int, c: float, expected: int) -> None:
    assert block_length(n, c) == expected


@pytest.mark.parametrize(
    ("n", "c", "fragment"),
    [
        (1, 6.7, "at least 2 observations"),
        (0, 6.7, "at least 2 observations"),
        (30, 0.0, "positive and finite"),
        (30, -1.0, "positive and finite"),
        (30, math.nan, "positive and finite"),
        (30, math.inf, "positive and finite"),
    ],
)
def test_block_length_refuses_what_it_cannot_define(n: int, c: float, fragment: str) -> None:
    with pytest.raises(StatisticalInputError) as error:
        block_length(n, c)

    _refused(error, fragment)


# --- the resample ----------------------------------------------------------------------


def test_the_first_resample_indices_match_the_independent_reference() -> None:
    indices = stationary_bootstrap_indices(8, replicates=4, block_length=2, seed=0)

    assert indices.tolist() == REFERENCE_ROWS


def test_indices_wrap_around_and_never_leave_the_series() -> None:
    indices = stationary_bootstrap_indices(8, replicates=4, block_length=2, seed=0)

    assert indices.min() >= 0
    assert indices.max() <= 7
    # Row 2 steps 7 -> 0: the continuation past the last observation wraps to the first.
    assert indices[2].tolist()[3:5] == [7, 0]


@pytest.mark.parametrize(("length", "expected"), [(4, 0.75), (8, 0.875)])
def test_a_block_continues_with_probability_one_minus_one_over_l(
    length: int, expected: float
) -> None:
    """A step continues the block (index + 1 mod n) unless it restarts, and a restart lands
    on index + 1 only by chance 1/n. So the continue rate is (1 - 1/L) + 1/(L n)."""

    n = 2000
    indices = stationary_bootstrap_indices(n, replicates=100, block_length=length, seed=7)
    continued = (indices[:, 1:] == (indices[:, :-1] + 1) % n).mean()

    assert continued == pytest.approx(expected + (1 - expected) / n, abs=0.008)


def test_a_block_length_of_one_is_an_independent_resample() -> None:
    n = 2000
    indices = stationary_bootstrap_indices(n, replicates=100, block_length=1, seed=7)

    assert (indices[:, 1:] == (indices[:, :-1] + 1) % n).mean() < 0.01


def test_a_block_as_long_as_the_series_is_allowed_and_always_continues_but_for_the_start() -> None:
    indices = stationary_bootstrap_indices(8, replicates=50, block_length=8, seed=2)

    # restart probability 1/8: most steps continue, so the rows are mostly runs of +1 mod 8
    assert (indices[:, 1:] == (indices[:, :-1] + 1) % 8).mean() > 0.85


def test_the_means_are_the_means_of_the_indexed_values() -> None:
    values = np.array([0.5, -1.0, 2.0, 0.25, 3.0, -0.75, 1.5, 0.0])
    indices = stationary_bootstrap_indices(8, replicates=4, block_length=2, seed=0)

    means = stationary_bootstrap_means(values, replicates=4, block_length=2, seed=0)

    assert means == pytest.approx(values[indices].mean(axis=1), rel=1e-14)


def test_the_same_inputs_give_the_same_means_and_another_seed_does_not() -> None:
    values = np.array(_wave(60))

    first = stationary_bootstrap_means(values, replicates=50, block_length=5, seed=3)
    again = stationary_bootstrap_means(values, replicates=50, block_length=5, seed=3)
    other = stationary_bootstrap_means(values, replicates=50, block_length=5, seed=4)

    assert first.tolist() == again.tolist()
    assert first.tolist() != other.tolist()


def test_the_mean_of_the_replicate_means_is_close_to_the_sample_mean() -> None:
    values = np.array(_wave(400))

    means = stationary_bootstrap_means(values, replicates=3000, block_length=10, seed=11)

    # circular blocks are unbiased for the sample mean; the standard error of 3000 averaged
    # replicates is ~1e-5 against a sample mean of 3e-4 and a std of 3e-3
    assert float(means.mean()) == pytest.approx(float(values.mean()), abs=5e-5)


@pytest.mark.parametrize(
    ("kwargs", "fragment"),
    [
        ({"replicates": 0, "block_length": 2}, "at least 1 replicate"),
        ({"replicates": 5, "block_length": 0}, "between 1 and the series length"),
        ({"replicates": 5, "block_length": 9}, "between 1 and the series length"),
    ],
)
def test_the_resample_refuses_a_bad_shape(kwargs: dict[str, int], fragment: str) -> None:
    with pytest.raises(StatisticalInputError) as error:
        stationary_bootstrap_means(np.arange(8.0), seed=0, **kwargs)

    _refused(error, fragment)


def test_the_resample_refuses_fewer_than_two_observations_and_bad_arrays() -> None:
    with pytest.raises(StatisticalInputError) as short:
        stationary_bootstrap_means(np.array([1.0]), replicates=2, block_length=1, seed=0)
    with pytest.raises(StatisticalInputError) as typed:
        stationary_bootstrap_means([1.0, 2.0, 3.0], replicates=2, block_length=2, seed=0)  # type: ignore[arg-type]
    with pytest.raises(StatisticalInputError) as nan:
        stationary_bootstrap_means(
            np.array([1.0, np.nan, 3.0]), replicates=2, block_length=2, seed=0
        )

    _refused(short, "at least 2 observations")
    _refused(typed, "one-dimensional float64 array")
    _refused(nan, "finite values")


# --- the percentiles -------------------------------------------------------------------


def test_the_percentiles_are_lower_for_p5_and_higher_for_p95() -> None:
    """Twenty means 1..20. The 5th percentile sits at fractional index 0.05 * 19 = 0.95, so
    'lower' is the first (1.0, where linear would say 1.95); the 95th at 18.05, so 'higher'
    is the twentieth (20.0, where linear would say 19.05)."""

    p5, p95 = percentile_bounds(np.arange(1.0, 21.0))

    assert (p5, p95) == (1.0, 20.0)


# --- the report ------------------------------------------------------------------------


def test_the_report_states_the_seed_block_length_replicates_and_the_interval() -> None:
    series = _series(_wave(36))

    result = bootstrap_mean(series, spec_sha256="spec", attempt_id="attempt", replicates=200, c=6.7)

    assert isinstance(result, BootstrapResult)
    assert result.seed == 17572605998880585980
    assert result.policy_version == "8c-sb-1"
    assert result.block_length == 15  # 6.7 * (36/3)^(1/3) = 15.34
    assert result.replicates == 200
    assert result.p5 <= result.mean <= result.p95
    lower = result.bootstrap_lower_bound
    assert (lower.name, lower.value, lower.evidence_sha256) == (
        "bootstrap_lower_bound",
        result.p5,
        (DIGEST,),
    )
    assert lower.basis_is_mark_to_market is True


def test_the_report_uses_the_protocols_constant_for_the_block_length() -> None:
    series = _series(_wave(36))

    result = bootstrap_mean(series, spec_sha256="spec", attempt_id="attempt", replicates=20, c=2.0)

    assert result.block_length == 5  # 2.0 * (36/3)^(1/3) = 4.58 -> 5, against 15 for c = 6.7


def test_the_report_reproduces_from_its_own_seed() -> None:
    series = _series(_wave(60))
    result = bootstrap_mean(series, spec_sha256="spec", attempt_id="a", replicates=100, c=6.7)

    means = stationary_bootstrap_means(
        series.values, replicates=100, block_length=result.block_length, seed=result.seed
    )

    assert result.mean == float(means.mean())
    assert (result.p5, result.p95) == percentile_bounds(means)


def test_a_constant_series_bootstraps_to_that_constant() -> None:
    result = bootstrap_mean(
        _series([0.001] * 40, ReturnSeriesBasis.REALIZED_CLOSED_TRADES),
        spec_sha256="s", attempt_id="a", replicates=50, c=6.7,
    )  # fmt: skip

    assert result.mean == pytest.approx(0.001, rel=1e-12)
    assert result.p5 == result.p95
    assert result.bootstrap_lower_bound.basis_is_mark_to_market is False


def test_a_sum_that_overflows_is_refused_and_not_reported() -> None:
    with pytest.raises(StatisticalInputError) as error:
        bootstrap_mean(
            _series([1e308, 1e308] * 20), spec_sha256="s", attempt_id="a", replicates=5, c=6.7
        )

    _refused(error, "replicate mean is not finite")


_PROGRAM = """
import json, math
from datetime import date, timedelta
import numpy as np
from trading_house.research.trial_ledger import ReturnSeriesBasis
from trading_house.research.validation.bootstrap import bootstrap_mean
from trading_house.research.validation.series import ReturnSeries

n = 60
values = [0.0004 + 0.004 * (math.sin(1.7 * i) + 0.5 * math.cos(0.23 * i * i)) for i in range(n)]
series = ReturnSeries(
    days=tuple(date(2024, 1, 1) + timedelta(days=i) for i in range(n)),
    values=np.array(values, dtype=np.float64),
    basis=ReturnSeriesBasis.MARK_TO_MARKET,
    evidence_sha256="e" * 64,
)
result = bootstrap_mean(series, spec_sha256="spec", attempt_id="attempt", replicates=300, c=6.7)
print(json.dumps(result.model_dump(mode="json"), sort_keys=True))
"""


def _run_in_subprocess(hash_seed: str) -> str:
    completed = subprocess.run(  # noqa: S603
        [sys.executable, "-c", _PROGRAM],
        env={**os.environ, "PYTHONHASHSEED": hash_seed},
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout


def test_two_processes_under_different_hash_seeds_bootstrap_identically() -> None:
    """Two SUBPROCESSES under different PYTHONHASHSEED, as the engine determinism test does.

    The seed is a sha256 and not ``hash()``, so nothing hash-randomised may reach the report;
    the in-process result is compared too, so the pin is to the code and not just to a
    pair of children that agree with each other."""

    first = _run_in_subprocess("0")
    second = _run_in_subprocess("12345")

    assert first == second
    payload = json.loads(first)
    assert payload["seed"] == 17572605998880585980
    assert payload["replicates"] == 300
    local = bootstrap_mean(
        _series(_wave(60)), spec_sha256="spec", attempt_id="attempt", replicates=300, c=6.7
    )
    assert payload == json.loads(local.model_dump_json())
