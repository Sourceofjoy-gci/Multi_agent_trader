"""The Politis-Romano stationary bootstrap of the mean daily return (umbrella 7.6; S-10, R-6).

A measurement: the report states the seed, block length, replicates, the mean of the
replicate means and its 5th and 95th percentiles; ``bootstrap_lower_bound`` is the 5th.
Nothing here asks whether it is above zero.

Each replicate is ``n`` draws. The first index is uniform; every later one restarts at a
uniform index with probability ``1 / L`` and otherwise takes the next index, wrapping
past the end. All replicates advance together, one time step at a time, from one
``Generator(PCG64(seed))``, drawing at each step first the restart uniforms and then the
fresh indices. The indices stream, so 10,000 replicates over years of days never hold
the full index matrix.

R-6: ``p5`` is ``np.percentile(..., method="lower")`` and ``p95`` is ``method="higher"``,
the neighbouring replicate means that make the interval wider, never narrower.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Iterator

import numpy as np
import numpy.typing as npt
from pydantic import NonNegativeInt, PositiveInt

from trading_house.core.values import CanonicalModel, FiniteFloat, NonEmptyStr
from trading_house.research.validation.measurement import Measurement
from trading_house.research.validation.series import ReturnSeries, refusal

POLICY_VERSION = "8c-sb-1"


def bootstrap_seed(spec_sha256: str, attempt_id: str, policy_version: str) -> int:
    """First 8 bytes, big-endian, of ``sha256("spec|attempt|policy")``; never ``hash()``."""

    parts = (spec_sha256, attempt_id, policy_version)
    if any("|" in part for part in parts):
        raise refusal("a seed input may not contain the | separator")
    digest = hashlib.sha256("|".join(parts).encode()).digest()
    return int.from_bytes(digest[:8], "big")


def block_length(n: int, c: float) -> int:
    """``min(n, max(2, round(c * (n / 3) ** (1 / 3))))``; ``round`` is Python's (half to even)."""

    if n < 2:
        raise refusal(f"a block length needs at least 2 observations, not {n}")
    if not math.isfinite(c) or c <= 0:
        raise refusal("the block length constant must be positive and finite")
    return int(min(n, max(2, round(c * (n / 3) ** (1 / 3)))))


def _index_stream(
    n: int, replicates: int, length: int, seed: int
) -> Iterator[npt.NDArray[np.int64]]:
    if n < 2:
        raise refusal(f"a bootstrap needs at least 2 observations, not {n}")
    if replicates < 1:
        raise refusal("a bootstrap needs at least 1 replicate")
    if not 1 <= length <= n:
        raise refusal("the block length must be between 1 and the series length")
    rng = np.random.Generator(np.random.PCG64(seed))
    restart = 1.0 / length
    index = rng.integers(0, n, size=replicates)
    yield index
    for _ in range(n - 1):
        restarts = rng.random(replicates) < restart
        fresh = rng.integers(0, n, size=replicates)
        index = np.where(restarts, fresh, (index + 1) % n)
        yield index


def stationary_bootstrap_indices(
    n: int, *, replicates: int, block_length: int, seed: int
) -> npt.NDArray[np.int64]:
    """The ``replicates x n`` resample indices. Small cases only: the means never build this."""

    return np.stack(list(_index_stream(n, replicates, block_length, seed)), axis=1)


def stationary_bootstrap_means(
    values: npt.NDArray[np.float64], *, replicates: int, block_length: int, seed: int
) -> npt.NDArray[np.float64]:
    """The mean of each replicate resample of ``values``."""

    if not isinstance(values, np.ndarray) or values.ndim != 1 or values.dtype != np.float64:
        raise refusal("a bootstrap needs a one-dimensional float64 array")
    if not bool(np.isfinite(values).all()):
        raise refusal("a bootstrap needs finite values")
    total = np.zeros(replicates, dtype=np.float64)
    with np.errstate(over="ignore"):  # an overflow is an inf the caller refuses
        for index in _index_stream(len(values), replicates, block_length, seed):
            total += values[index]
    return total / len(values)


def percentile_bounds(means: npt.NDArray[np.float64]) -> tuple[float, float]:
    """The 5th (``lower``) and 95th (``higher``) percentiles, conservatively (R-6)."""

    return (
        float(np.percentile(means, 5, method="lower")),
        float(np.percentile(means, 95, method="higher")),
    )


class BootstrapResult(CanonicalModel):
    seed: NonNegativeInt
    policy_version: NonEmptyStr
    block_length: PositiveInt
    replicates: PositiveInt
    mean: FiniteFloat
    p5: FiniteFloat
    p95: FiniteFloat
    bootstrap_lower_bound: Measurement
    """The 5th percentile, as a measurement carrying the evidence digest."""


def bootstrap_mean(
    series: ReturnSeries,
    *,
    spec_sha256: str,
    attempt_id: str,
    replicates: int,
    c: float,
    policy_version: str = POLICY_VERSION,
) -> BootstrapResult:
    seed = bootstrap_seed(spec_sha256, attempt_id, policy_version)
    length = block_length(len(series.values), c)
    means = stationary_bootstrap_means(
        series.values, replicates=replicates, block_length=length, seed=seed
    )
    if not bool(np.isfinite(means).all()):
        raise refusal("a replicate mean is not finite")
    p5, p95 = percentile_bounds(means)
    return BootstrapResult(
        seed=seed,
        policy_version=policy_version,
        block_length=length,
        replicates=replicates,
        mean=float(means.mean()),
        p5=p5,
        p95=p95,
        bootstrap_lower_bound=Measurement.defined(
            "bootstrap_lower_bound",
            p5,
            evidence_sha256=(series.evidence_sha256,),
            basis_is_mark_to_market=series.promotion_grade,
        ),
    )
