"""Sample moments of a daily return series (umbrella 7.4).

``skew`` and ``kurt`` are the raw central-moment ratios exactly as the umbrella words
them, so they use the population ``m2``; the Sharpe uses the sample standard deviation
(``ddof=1``). A sample these cannot describe is returned as a ``NoMoments`` with its
reason, not as a number: a constant series has no Sharpe, and a value that overflows
has no finite moment.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

from trading_house.research.validation.series import refusal


@dataclass(frozen=True, slots=True)
class Moments:
    n: int
    mean: float
    std: float
    sharpe: float
    """Per-day Sharpe ``mean / std`` (R-2): the figure the PSR formula's ``n`` requires."""
    skew: float
    kurt: float


@dataclass(frozen=True, slots=True)
class NoMoments:
    reason: str


def moments(values: npt.NDArray[np.float64]) -> Moments | NoMoments:
    """The moments of a finite one-dimensional ``float64`` sample, or why there are none."""

    if not isinstance(values, np.ndarray) or values.ndim != 1 or values.dtype != np.float64:
        raise refusal("the moments need a one-dimensional float64 array")
    if not bool(np.isfinite(values).all()):
        raise refusal("the moments need finite values")
    n = len(values)
    if n < 2:
        return NoMoments(f"the sample holds {n} values; at least 2 are required")
    if values.max() == values.min():
        return NoMoments("the sample is constant, so it has no variance")
    with np.errstate(all="ignore"):
        # numpy scalars throughout: an overflow is then an inf the check below refuses,
        # where a Python float would raise instead.
        mean = values.mean()
        dev = values - mean
        m2 = np.mean(dev**2)
        if m2 == 0.0:
            return NoMoments("the sample variance underflows to zero")
        std = np.sqrt(dev @ dev / (n - 1))
        found = (mean, std, mean / std, np.mean(dev**3) / m2**1.5, np.mean(dev**4) / m2**2)
    if not all(math.isfinite(x) for x in found):
        return NoMoments("a moment of the sample is not finite")
    return Moments(n, *(float(x) for x in found))
