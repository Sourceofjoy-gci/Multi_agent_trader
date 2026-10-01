"""Net expectancy per trade: the CPCV 5th percentile, out-of-sample coverage, a scenario.

Phase 8C3 (umbrella 7.3, 7.7, 7.8). Measurements only; none is compared with anything.

The CPCV 5th-percentile measurement is ALWAYS computed (spec section 7, amended
2026-10-01). It is not blocked when ``paths_differ`` is false: for an intraday strategy
no trade straddles a fold start, the paths are then identical, and each path's expectancy
is the sample's own net expectancy, a real quantity (is out-of-sample net expectancy
positive?). ``paths_differ`` is carried beside it as a plain fact, so a reader of the
number knows whether it came from one path or from a spread of them.

``expectancy`` everywhere is the mean net P&L per closed trade, the one primary metric.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from decimal import Decimal

import numpy as np
from pydantic import NonNegativeInt, PositiveInt

from trading_house.core.values import CanonicalModel, FiniteFloat, NonEmptyStr
from trading_house.features.sessions import Session
from trading_house.research.validation.measurement import Measurement
from trading_house.research.validation.policy import CPCV_P5_QUANTILE
from trading_house.research.validation.series import TradeSample, refusal


def net_expectancy(sample: TradeSample) -> float | None:
    """The mean net P&L per closed trade; ``None`` for a sample with no trades."""

    if len(sample) == 0:
        return None
    with np.errstate(all="ignore"):
        mean = float(sample.net_pnl.mean())
    if not math.isfinite(mean):
        raise refusal("the net expectancy of a trade sample is not finite")
    return mean


def trade_subset(sample: TradeSample, indices: Sequence[int]) -> TradeSample:
    """The trades at ``indices`` (in the order given), as a sample of their own."""

    picked = list(indices)
    return TradeSample(
        entry_day=tuple(sample.entry_day[i] for i in picked),
        exit_day=tuple(sample.exit_day[i] for i in picked),
        entry_at=tuple(sample.entry_at[i] for i in picked),
        exit_at=tuple(sample.exit_at[i] for i in picked),
        net_pnl=sample.net_pnl[picked],
        session=tuple(sample.session[i] for i in picked),
    )


def _measurement(
    name: str,
    outcome: float | str,
    evidence_sha256: Sequence[str],
    basis_is_mark_to_market: bool,
) -> Measurement:
    if isinstance(outcome, str):
        return Measurement.undefined(
            name,
            outcome,
            evidence_sha256=evidence_sha256,
            basis_is_mark_to_market=basis_is_mark_to_market,
        )
    return Measurement.defined(
        name,
        outcome,
        evidence_sha256=evidence_sha256,
        basis_is_mark_to_market=basis_is_mark_to_market,
    )


class PathExpectancy(CanonicalModel):
    index: NonNegativeInt
    trades_kept: NonNegativeInt
    net_expectancy: FiniteFloat | None
    """``None`` when the path kept no trade."""


class CpcvP5Result(CanonicalModel):
    p5: Measurement
    quantile: FiniteFloat
    rank: PositiveInt | None
    """The 1-based nearest rank read from the ascending expectancies; ``None`` if undefined."""
    paths_differ: bool
    """Whether the paths kept different trade sets. When false the paths are one path and
    ``p5`` is that path's expectancy, the aggregate out-of-sample net expectancy."""
    paths: tuple[PathExpectancy, ...]


def cpcv_p5(
    paths: Sequence[TradeSample],
    *,
    paths_differ: bool,
    evidence_sha256: Sequence[str],
    basis_is_mark_to_market: bool,
) -> CpcvP5Result:
    """The nearest-rank value at ``ceil(CPCV_P5_QUANTILE * n_paths)`` of the path expectancies.

    ``paths`` holds each CPCV path's KEPT trades. Nearest rank over the ascending
    expectancies is conservative when the path count is small: 5 paths read the lowest.
    A path with no kept trade has no expectancy, so the measurement is undefined naming it.
    (``0.05 * n`` is exact for every integer product: the double 0.05 exceeds 0.05 by a
    relative 2**-54, below half a unit in the last place of any such product.)
    """

    expectancies = [net_expectancy(sample) for sample in paths]
    rows = tuple(
        PathExpectancy(index=i, trades_kept=len(sample), net_expectancy=value)
        for i, (sample, value) in enumerate(zip(paths, expectancies, strict=True))
    )

    def result(outcome: float | str, rank: int | None = None) -> CpcvP5Result:
        return CpcvP5Result(
            p5=_measurement("cpcv_p5", outcome, evidence_sha256, basis_is_mark_to_market),
            quantile=CPCV_P5_QUANTILE,
            rank=rank,
            paths_differ=paths_differ,
            paths=rows,
        )

    if not paths:
        return result("there are no CPCV paths")
    empty = [i for i, value in enumerate(expectancies) if value is None]
    if empty:
        return result(f"CPCV path {empty[0]} kept no closed trade, so it has no expectancy")
    rank = math.ceil(CPCV_P5_QUANTILE * len(paths))
    ascending = sorted(value for value in expectancies if value is not None)
    return result(ascending[rank - 1], rank)


class CoverageResult(CanonicalModel):
    oos_trades: Measurement
    regimes_represented: Measurement
    declared_labels: tuple[NonEmptyStr, ...]
    oos_path_index: NonNegativeInt | None
    """The path whose kept trades are the out-of-sample sample: the one with the fewest."""


def oos_coverage(
    paths: Sequence[TradeSample],
    declared_labels: Sequence[str],
    *,
    evidence_sha256: Sequence[str],
    basis_is_mark_to_market: bool,
) -> CoverageResult:
    """Trade count and regimes represented in the out-of-sample sample.

    The sample is the path with the FEWEST kept trades (the lowest index among ties):
    conservative, since coverage that holds there holds on every path. Regimes are
    defined only when every declared label is a session name (the one classifier this
    repository has) and the sample holds trades; ``regimes_represented`` is the number of
    declared labels with at least one trade in it.
    """

    labels = tuple(declared_labels)

    def result(trades: float | str, regimes: float | str, index: int | None) -> CoverageResult:
        return CoverageResult(
            oos_trades=_measurement("oos_trades", trades, evidence_sha256, basis_is_mark_to_market),
            regimes_represented=_measurement(
                "regimes_represented", regimes, evidence_sha256, basis_is_mark_to_market
            ),
            declared_labels=labels,
            oos_path_index=index,
        )

    if not paths:
        reason = "there are no CPCV paths"
        return result(reason, reason, None)
    index = min(range(len(paths)), key=lambda i: len(paths[i]))
    sample = paths[index]
    count = float(len(sample))
    names = {session.value for session in Session}
    if not labels:
        return result(count, "the protocol declares no regime labels", index)
    foreign = [label for label in labels if label not in names]
    if foreign:
        return result(
            count,
            f"regime label {foreign[0]!r} is not a session name, the only classifier available",
            index,
        )
    if len(sample) == 0:
        return result(count, "the out-of-sample sample holds no trades", index)
    present = set(sample.session)
    return result(count, float(len({label for label in labels if label in present})), index)


def scenario_expectancy(
    sample: TradeSample | None,
    multiplier: Decimal,
    *,
    evidence_sha256: Sequence[str],
    basis_is_mark_to_market: bool,
) -> Measurement:
    """Net expectancy over the FULL sealed sample of a stressed-cost run.

    This is the sealed sample's expectancy, not a locked out-of-sample one: no holdout
    exists, and what that means for a gate is 8D's question. ``None`` is a level with no
    sealed run, which is undefined and not zero.
    """

    name = f"scenario_expectancy_{multiplier:.1f}"
    if sample is None:
        return _measurement(
            name,
            f"no constant-notional run is sealed at {multiplier:.1f}x",
            evidence_sha256,
            basis_is_mark_to_market,
        )
    value = net_expectancy(sample)
    if value is None:
        return _measurement(
            name,
            "the stressed run closed no trade, so its sealed sample has no expectancy",
            evidence_sha256,
            basis_is_mark_to_market,
        )
    return _measurement(name, value, evidence_sha256, basis_is_mark_to_market)
