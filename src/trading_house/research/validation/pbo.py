"""The probability of backtest overfitting (umbrella 7.5; spec R-1, S-7, S-8).

Measured per CPCV split over every result-producing candidate. In each split the
candidate(s) best in sample are located, and their out-of-sample standing is a
relative rank ``lambda``. The split is an overfit case when ``lambda <= 1/2``
(``logit <= 0``). ``PBO`` is the fraction of such splits. No threshold is applied.

R-1: OOS ranks run ascending (1 = worst OOS) and ``lambda = (rank - 1) / (N - 1)``, so
an in-sample best that is out-of-sample best has ``lambda = 1`` (not overfit) and one
that is out-of-sample worst has ``lambda = 0`` (overfit). Ties take the average rank,
and the in-sample best is the whole tied set, its ``lambda`` their mean. The test is
done on doubled integer ranks, so ``lambda == 1/2`` is decided exactly, and no logarithm
(and no infinity) is ever evaluated. In-sample ties are exact float equality.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date

from pydantic import NonNegativeInt

from trading_house.core.values import CanonicalModel, NonEmptyStr, Probability
from trading_house.research.validation.measurement import Measurement
from trading_house.research.validation.sampling import split_samples
from trading_house.research.validation.series import TradeSample, refusal
from trading_house.research.validation.splits import CpcvFold, CpcvSplit

PRIMARY_METRIC = "net_expectancy"
"""The only metric implemented (S-7): mean net P&L per closed trade."""


class PboSplit(CanonicalModel):
    index: NonNegativeInt
    is_best: tuple[NonEmptyStr, ...]
    """The candidate ids tied for the best in-sample metric, sorted."""
    oos_lambda: Probability
    overfit: bool
    """``lambda <= 1/2``, i.e. ``logit <= 0``."""


class PboResult(CanonicalModel):
    pbo: Measurement
    splits: tuple[PboSplit, ...]
    candidates: tuple[NonEmptyStr, ...]
    """The ranked candidate ids, sorted."""
    excluded: tuple[NonEmptyStr, ...]
    """Result-producing candidates with no sealed baseline, listed and never ranked."""


def pbo(
    candidates: Mapping[str, TradeSample],
    excluded: Sequence[str],
    days: Sequence[date],
    folds: Sequence[CpcvFold],
    splits: Sequence[CpcvSplit],
    *,
    purge_days: int,
    embargo_days: int,
    primary_metric: str,
    evidence_sha256: Sequence[str],
    basis_is_mark_to_market: bool,
) -> PboResult:
    if not evidence_sha256:
        raise refusal("PBO needs the digests of the evidence it ranks")
    if not splits:
        raise refusal("PBO needs at least one split")
    ids = tuple(sorted(candidates))
    left_out = tuple(sorted(excluded))
    if set(ids) & set(left_out):
        raise refusal("a candidate cannot be both ranked and excluded")

    def undefined(reason: str) -> PboResult:
        return PboResult(
            pbo=Measurement.undefined(
                "pbo",
                reason,
                evidence_sha256=evidence_sha256,
                basis_is_mark_to_market=basis_is_mark_to_market,
            ),
            splits=(),
            candidates=ids,
            excluded=left_out,
        )

    if primary_metric != PRIMARY_METRIC:
        return undefined(f"the primary metric {primary_metric!r} is not implemented")
    if len(ids) < 2:
        return undefined(f"{len(ids)} candidate(s) can be ranked; at least 2 are required")

    results: list[PboSplit] = []
    for split in splits:
        in_sample: list[float] = []
        out_of_sample: list[float] = []
        for name in ids:
            trades = candidates[name]
            train, test = split_samples(
                trades,
                days,
                folds,
                split.test_folds,
                purge_days=purge_days,
                embargo_days=embargo_days,
            )
            if len(train) == 0:
                return undefined(f"candidate {name} has no in-sample trade in split {split.index}")
            if len(test) == 0:
                return undefined(
                    f"candidate {name} has no out-of-sample trade in split {split.index}"
                )
            in_sample.append(float(trades.net_pnl[train].mean()))
            out_of_sample.append(float(trades.net_pnl[test].mean()))
        best = max(in_sample)
        winners = [i for i, value in enumerate(in_sample) if value == best]
        # 2 * (ascending average rank - 1), an integer: 2 * (candidates beaten) + (ties) - 1.
        doubled = sum(
            2 * sum(o < out_of_sample[i] for o in out_of_sample)
            + sum(o == out_of_sample[i] for o in out_of_sample)
            - 1
            for i in winners
        )
        top = len(ids) - 1
        results.append(
            PboSplit(
                index=split.index,
                is_best=tuple(ids[i] for i in winners),
                oos_lambda=doubled / (2 * len(winners) * top),
                overfit=doubled <= len(winners) * top,
            )
        )
    return PboResult(
        pbo=Measurement.defined(
            "pbo",
            sum(item.overfit for item in results) / len(results),
            evidence_sha256=evidence_sha256,
            basis_is_mark_to_market=basis_is_mark_to_market,
        ),
        splits=tuple(results),
        candidates=ids,
        excluded=left_out,
    )
