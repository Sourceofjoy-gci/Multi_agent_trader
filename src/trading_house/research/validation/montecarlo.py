"""Bootstrap Monte Carlo of the drawdown halt and of a loss (umbrella 7.7; R-4, S-10).

Each replicate resamples the series' daily returns with the stationary bootstrap (the
same per-step index stream as ``bootstrap.py``) to the series' own length, the horizon
(R-4: the protocol's declared data window, in days), and compounds them from 1.0.
Per replicate it tracks the running peak and the largest drawdown streaming over the
time steps, so memory is ``O(replicates)`` and never ``O(replicates x n)``.

``p_halt`` is the fraction of replicates whose maximum drawdown is **at or beyond**
``MAX_DRAWDOWN`` (``>=``). ``p_loss`` is the fraction whose FINAL equity is **below**
1.0, strictly: ending exactly where it began is not a loss. Neither is compared with
anything here.

A series holding a daily return of -100% or worse ends the compounded path (equity at
or below zero); the Monte Carlo is then undefined rather than reporting a drawdown of a
curve that no longer has a positive peak. Likewise if a replicate's equity overflows.
"""

from __future__ import annotations

from collections.abc import Iterable

import numpy as np
import numpy.typing as npt
from pydantic import NonNegativeInt, PositiveInt

from trading_house.core.values import CanonicalModel, FiniteFloat, NonEmptyStr
from trading_house.research.validation.bootstrap import _index_stream, bootstrap_seed
from trading_house.research.validation.measurement import Measurement
from trading_house.research.validation.policy import MAX_DRAWDOWN, MC_POLICY_VERSION
from trading_house.research.validation.series import ReturnSeries


class McResult(CanonicalModel):
    seed: NonNegativeInt
    policy_version: NonEmptyStr
    block_length: PositiveInt
    replicates: PositiveInt
    horizon_days: PositiveInt
    """The series length in days (R-4)."""
    halt_level: FiniteFloat
    """The drawdown fraction at or beyond which a replicate counts as halted."""
    p_halt: Measurement
    p_loss: Measurement
    """The fraction of replicates whose final equity is below 1.0."""


def mc_seed(spec_sha256: str, attempt_id: str) -> int:
    """The Monte Carlo's seed: the bootstrap's recipe under this module's policy version."""

    return bootstrap_seed(spec_sha256, attempt_id, MC_POLICY_VERSION)


def _simulate(
    values: npt.NDArray[np.float64], steps: Iterable[npt.NDArray[np.int64]], replicates: int
) -> tuple[npt.NDArray[np.float64], npt.NDArray[np.float64]]:
    """The largest drawdown and the final equity of each replicate, streaming over ``steps``.

    Each step is the index (into ``values``) every replicate draws at that time step.
    """

    equity = np.ones(replicates, dtype=np.float64)
    peak = np.ones(replicates, dtype=np.float64)
    worst = np.zeros(replicates, dtype=np.float64)
    with np.errstate(all="ignore"):
        for index in steps:
            equity *= 1.0 + values[index]
            np.maximum(peak, equity, out=peak)
            np.maximum(worst, (peak - equity) / peak, out=worst)
    return worst, equity


def _fractions(
    worst: npt.NDArray[np.float64], final: npt.NDArray[np.float64]
) -> tuple[float, float]:
    """``(p_halt, p_loss)``: drawdown at or beyond the level, and final equity below 1.0."""

    return float((worst >= MAX_DRAWDOWN).mean()), float((final < 1.0).mean())


def drawdown_loss_probabilities(
    series: ReturnSeries, *, replicates: int, block_length: int, seed: int
) -> McResult:
    """``p_halt`` and ``p_loss`` over ``replicates`` stationary-bootstrap paths of the series."""

    digests, grade = (series.evidence_sha256,), series.promotion_grade
    horizon = len(series.values)

    def result(p_halt: float | str, p_loss: float | str) -> McResult:
        def measurement(name: str, outcome: float | str) -> Measurement:
            if isinstance(outcome, str):
                return Measurement.undefined(
                    name, outcome, evidence_sha256=digests, basis_is_mark_to_market=grade
                )
            return Measurement.defined(
                name, outcome, evidence_sha256=digests, basis_is_mark_to_market=grade
            )

        return McResult(
            seed=seed,
            policy_version=MC_POLICY_VERSION,
            block_length=block_length,
            replicates=replicates,
            horizon_days=horizon,
            halt_level=MAX_DRAWDOWN,
            p_halt=measurement("p_halt", p_halt),
            p_loss=measurement("p_loss", p_loss),
        )

    # Simulated first, so a bad replicate count or block length is refused even for a
    # series that is then reported undefined.
    worst, final = _simulate(
        series.values, _index_stream(horizon, replicates, block_length, seed), replicates
    )
    if float(series.values.min()) <= -1.0:
        reason = "a daily return of -100% or worse ends the compounded equity path"
        return result(reason, reason)
    # An equity that overflows makes its peak and equity both infinite, so its drawdown is
    # NaN: checking ``worst`` covers ``final`` too.
    if not bool(np.isfinite(worst).all()):
        reason = "a replicate's compounded equity is not finite"
        return result(reason, reason)
    return result(*_fractions(worst, final))
