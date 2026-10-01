"""The walk-forward folds and CPCV splits and paths of one sealed candidate.

Phase 8C1. A read over one sealed constant-notional 1.0x bundle and the protocol
that registered it. It states the calendar arithmetic of the protocol's declared
validation policy over the bundle's own series, and how many closed trades each
CPCV path keeps and drops under the declared purge and embargo. It computes no
statistic and states no decision: whether any of this is good enough is not a
question this module asks.

A series too short for one walk-forward fold is reported as undefined, with the
reason, and never as one short fold.
"""

from __future__ import annotations

from datetime import date
from typing import Self

from pydantic import NonNegativeInt, PositiveInt, model_validator

from trading_house.core.values import CanonicalModel, NonEmptyStr
from trading_house.ops.scenarios import refuse_reportable
from trading_house.research.evidence import EvidenceBundle
from trading_house.research.trial_ledger import ReturnSeriesBasis, TrialProtocol
from trading_house.research.validation.sampling import path_trade_sample
from trading_house.research.validation.series import ReturnSeries, TradeSample
from trading_house.research.validation.splits import (
    CpcvAssignment,
    CpcvFold,
    CpcvSplit,
    WalkForwardFold,
    cpcv_folds,
    cpcv_paths,
    cpcv_splits,
    hours_to_days,
    walk_forward_folds,
)


class SeriesSummary(CanonicalModel):
    """The series the splits were cut from. ``promotion_grade`` states the basis: it is
    true only for a mark-to-market series and describes the input, not a candidate."""

    first_day: date
    last_day: date
    days: PositiveInt
    basis: ReturnSeriesBasis
    promotion_grade: bool


class WalkForwardReport(CanonicalModel):
    """The folds, or the reason there are none. Exactly one of the two is present."""

    folds: tuple[WalkForwardFold, ...]
    undefined_reason: NonEmptyStr | None

    @model_validator(mode="after")
    def folds_or_a_reason_and_never_both_or_neither(self) -> Self:
        if bool(self.folds) == (self.undefined_reason is not None):
            raise ValueError("a walk-forward report holds folds or a reason, and exactly one")
        return self


class PathReport(CanonicalModel):
    """One CPCV path: which split supplies each fold, and the closed trades its test
    samples kept and dropped (the two add up to every closed trade)."""

    index: NonNegativeInt
    assignments: tuple[CpcvAssignment, ...]
    trades_kept: NonNegativeInt
    trades_excluded: NonNegativeInt


class CpcvReport(CanonicalModel):
    folds: tuple[CpcvFold, ...]
    splits: tuple[CpcvSplit, ...]
    paths: tuple[PathReport, ...]


class SplitsReport(CanonicalModel):
    trial_id: NonEmptyStr
    spec_sha256: NonEmptyStr
    evidence_sha256: NonEmptyStr
    series: SeriesSummary
    closed_trades: NonNegativeInt
    purge_days: NonNegativeInt
    embargo_days: NonNegativeInt
    wfa: WalkForwardReport
    cpcv: CpcvReport


def splits_report(
    *, trial_id: str, protocol: TrialProtocol, sealed: tuple[str, EvidenceBundle]
) -> SplitsReport:
    """The folds, splits and paths the protocol's policy cuts out of one sealed run.

    ``sealed`` pairs the bundle with the digest the chain names for it. Raises
    ``StatisticalInputError`` when the bundle is not a usable series or the policy
    cannot be applied to it (for example more CPCV folds than days).
    """

    refuse_reportable(trial_id, [sealed])
    digest, bundle = sealed
    series = ReturnSeries.from_bundle(bundle, digest)
    trades = TradeSample.from_bundle(bundle)
    policy = protocol.validation
    purge_days, embargo_days = (
        hours_to_days(policy.purge_hours),
        hours_to_days(policy.embargo_hours),
    )

    wfa_folds = walk_forward_folds(
        series.days,
        train_months=policy.wfa_train_months,
        validation_months=policy.wfa_validation_months,
        test_months=policy.wfa_test_months,
        purge_days=purge_days,
    )
    months = policy.wfa_train_months + policy.wfa_validation_months + policy.wfa_test_months
    reason = None
    if not wfa_folds:
        reason = (
            f"the series spans {len(series.days)} days and no fold of {policy.wfa_train_months} "
            f"training, {policy.wfa_validation_months} validation and {policy.wfa_test_months} "
            f"test months ({months} in all) fits inside it"
        )

    folds = cpcv_folds(series.days, policy.cpcv_folds)
    splits = cpcv_splits(policy.cpcv_folds)
    paths: list[PathReport] = []
    for path in cpcv_paths(policy.cpcv_folds):
        kept, excluded = path_trade_sample(
            trades,
            series.days,
            folds,
            splits,
            path,
            purge_days=purge_days,
            embargo_days=embargo_days,
        )
        paths.append(
            PathReport(
                index=path.index,
                assignments=path.assignments,
                trades_kept=len(kept),
                trades_excluded=len(excluded),
            )
        )

    return SplitsReport(
        trial_id=trial_id,
        spec_sha256=bundle.spec_sha256,
        evidence_sha256=digest,
        series=SeriesSummary(
            first_day=series.days[0],
            last_day=series.days[-1],
            days=len(series.days),
            basis=series.basis,
            promotion_grade=series.promotion_grade,
        ),
        closed_trades=len(trades),
        purge_days=purge_days,
        embargo_days=embargo_days,
        wfa=WalkForwardReport(folds=wfa_folds, undefined_reason=reason),
        cpcv=CpcvReport(folds=folds, splits=splits, paths=tuple(paths)),
    )
