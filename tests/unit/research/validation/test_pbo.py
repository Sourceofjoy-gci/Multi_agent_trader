"""Phase 8C2: PBO, including R-1 in both directions.

January 2024 in six five-day folds, as in ``test_sampling.py``: f0 = 1-5, f1 = 6-10, ...,
f5 = 26-30. Each candidate here has one closed trade per fold (entry and exit on the
fold's middle day), so a fold's net P&L is the one number written for it, and a split's
in-sample metric is the mean over the four folds outside the test pair and its
out-of-sample metric the mean over the two inside. Splits are numbered as
``cpcv_splits(6)`` lists them: 0 = (0,1), 1 = (0,2), 2 = (0,3), 3 = (0,4), 4 = (0,5),
5 = (1,2), ..., 14 = (4,5). With purge and embargo of 0 no trade is ever excluded.

Ranks are ascending (1 = worst out of sample) and ``lambda = (rank - 1) / (N - 1)``;
overfit means ``lambda <= 1/2``. Every expectation is worked out in the comment above it.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import numpy as np
import pytest

from trading_house.core.errors import StatisticalInputError
from trading_house.research.validation.pbo import PRIMARY_METRIC, PboResult, PboSplit, pbo
from trading_house.research.validation.series import TradeSample
from trading_house.research.validation.splits import CpcvFold, CpcvSplit, cpcv_splits

DIGESTS = ("a" * 64, "b" * 64)
DAYS = tuple(date(2024, 1, 1) + timedelta(days=i) for i in range(30))
FOLDS = tuple(
    CpcvFold(index=i, start=date(2024, 1, 5 * i + 1), end=date(2024, 1, 5 * i + 5), days=5)
    for i in range(6)
)
SPLITS = cpcv_splits(6)


def _trades(spans: list[tuple[int, int, float]]) -> TradeSample:
    """``(entry day, exit day, net pnl)`` in January 2024."""

    entry = [date(2024, 1, e) for e, _, _ in spans]
    exit_ = [date(2024, 1, x) for _, x, _ in spans]
    return TradeSample(
        entry_day=tuple(entry),
        exit_day=tuple(exit_),
        entry_at=tuple(datetime(d.year, d.month, d.day, 12, tzinfo=UTC) for d in entry),
        exit_at=tuple(datetime(d.year, d.month, d.day, 13, tzinfo=UTC) for d in exit_),
        net_pnl=np.array([p for _, _, p in spans], dtype=np.float64),
        session=("london",) * len(spans),
    )


def _per_fold(pnl: list[float]) -> TradeSample:
    return _trades([(5 * f + 3, 5 * f + 3, p) for f, p in enumerate(pnl)])


def _run(
    candidates: dict[str, TradeSample],
    splits: list[CpcvSplit] | tuple[CpcvSplit, ...] = SPLITS,
    *,
    excluded: tuple[str, ...] = (),
    purge: int = 0,
    embargo: int = 0,
    metric: str = PRIMARY_METRIC,
    grade: bool = True,
) -> PboResult:
    return pbo(
        candidates,
        excluded,
        DAYS,
        FOLDS,
        splits,
        purge_days=purge,
        embargo_days=embargo,
        primary_metric=metric,
        evidence_sha256=DIGESTS,
        basis_is_mark_to_market=grade,
    )


def _refused(error: pytest.ExceptionInfo[StatisticalInputError], fragment: str) -> None:
    assert str(error.value) == StatisticalInputError.public_message
    cause = error.value.__cause__
    assert isinstance(cause, ValueError)
    assert fragment in str(cause)


def _pbo(result: PboResult) -> float | None:
    return result.pbo.value


# --- R-1, both ways -------------------------------------------------------------------


def test_r1_an_in_sample_best_that_is_out_of_sample_best_is_not_overfit() -> None:
    """A = 3 in every fold, B = 2, C = 1: A is in-sample best and out-of-sample best in
    every one of the C(6,2) = 15 splits. Ascending rank of A is 3, so lambda = (3-1)/(3-1) = 1,
    logit = +inf, not overfit; PBO = 0. (A descending rank would flip this to 1.)"""

    result = _run({"A": _per_fold([3] * 6), "B": _per_fold([2] * 6), "C": _per_fold([1] * 6)})

    assert len(result.splits) == 15
    assert _pbo(result) == 0.0
    assert all(s.is_best == ("A",) and s.oos_lambda == 1.0 and not s.overfit for s in result.splits)


def test_r1_an_in_sample_best_that_is_out_of_sample_worst_is_overfit() -> None:
    """X = -10 in fold 0 and 9 elsewhere; Y = 5; Z = 4. Splits 0, 1, 2 all test fold 0.

    In sample X averages 9 over the four train folds (Y 5, Z 4): X is IS-best. Out of sample
    X averages (-10 + 9) / 2 = -0.5, below Z (4) and Y (5): rank 1, lambda = 0, logit = -inf,
    overfit in all three; PBO = 1."""

    result = _run(
        {"X": _per_fold([-10, 9, 9, 9, 9, 9]), "Y": _per_fold([5] * 6), "Z": _per_fold([4] * 6)},
        SPLITS[:3],
    )

    assert _pbo(result) == 1.0
    assert all(s.is_best == ("X",) and s.oos_lambda == 0.0 and s.overfit for s in result.splits)


def test_a_candidate_in_the_middle_is_exactly_on_the_boundary_and_counts_as_overfit() -> None:
    """X = 0 in fold 0, 9 elsewhere: split 0 has X IS-best (9) and OOS (0 + 9)/2 = 4.5, between
    Z (4) and Y (5). Rank 2 of 3, lambda = 1/2, logit = 0: counted overfit (the umbrella's `<=`)."""

    result = _run(
        {"X": _per_fold([0, 9, 9, 9, 9, 9]), "Y": _per_fold([5] * 6), "Z": _per_fold([4] * 6)},
        SPLITS[:1],
    )

    assert result.splits[0].oos_lambda == 0.5
    assert result.splits[0].overfit is True
    assert _pbo(result) == 1.0


def test_pbo_is_the_fraction_of_overfit_splits() -> None:
    """A = [10,10,10,10,1,0], B = 5 everywhere; two splits, (0,1) and (4,5).

    (0,1): train folds 2-5, A = (10+10+1+0)/4 = 5.25 > 5, A is IS-best; OOS A = 10 > 5, rank 2 of 2,
    lambda = 1: not overfit.  (4,5): train folds 0-3, A = 10 > 5; OOS A = 0.5 < 5, rank 1,
    lambda = 0: overfit.  PBO = 1/2."""

    result = _run(
        {"A": _per_fold([10, 10, 10, 10, 1, 0]), "B": _per_fold([5] * 6)},
        [SPLITS[0], SPLITS[14]],
    )

    assert _pbo(result) == 0.5
    assert [(s.index, s.oos_lambda, s.overfit) for s in result.splits] == [
        (0, 1.0, False),
        (14, 0.0, True),
    ]


# --- ties ----------------------------------------------------------------------------


def test_out_of_sample_ties_take_the_average_rank() -> None:
    """A = B = 3 everywhere, C = 1; split 0. Both A and B are IS-best (tie at 3).

    OOS: C = 1 (rank 1), A = B = 3 tie for ranks 2 and 3, average 2.5. lambda = (2.5-1)/(3-1) = 0.75
    for each, so the split's lambda is 0.75 and it is not overfit. (Minimum-rank ties would give
    rank 2, lambda 0.5, overfit.)"""

    result = _run(
        {"A": _per_fold([3] * 6), "B": _per_fold([3] * 6), "C": _per_fold([1] * 6)}, SPLITS[:1]
    )

    assert result.splits[0].is_best == ("A", "B")
    assert result.splits[0].oos_lambda == 0.75
    assert _pbo(result) == 0.0


def test_the_in_sample_best_is_the_whole_tied_set_and_lambda_is_its_mean() -> None:
    """A = [10,10,4,4,4,4], B = [0,0,4,4,4,4], C = 1; split 0 tests folds 0 and 1.

    In sample A = B = 4 > C = 1: the IS-best set is {A, B}. OOS A = 10, B = 0, C = 1: ranks
    A 3, C 2, B 1, so lambda_A = 1 and lambda_B = 0. The set's lambda is their mean, 1/2: overfit.
    Taking only the first of the tied set would give 1 (not overfit)."""

    result = _run(
        {
            "A": _per_fold([10, 10, 4, 4, 4, 4]),
            "B": _per_fold([0, 0, 4, 4, 4, 4]),
            "C": _per_fold([1] * 6),
        },
        SPLITS[:1],
    )

    assert result.splits[0].is_best == ("A", "B")
    assert result.splits[0].oos_lambda == 0.5
    assert result.splits[0].overfit is True
    assert _pbo(result) == 1.0


# --- refusals and undefined ------------------------------------------------------------


def test_one_candidate_is_undefined_and_the_excluded_are_listed() -> None:
    result = _run({"A": _per_fold([3] * 6)}, excluded=("Z", "Y"))

    assert result.pbo.value is None
    assert result.pbo.undefined_reason == "1 candidate(s) can be ranked; at least 2 are required"
    assert (result.candidates, result.excluded, result.splits) == (("A",), ("Y", "Z"), ())


def test_an_excluded_candidate_is_listed_and_never_ranked() -> None:
    result = _run({"A": _per_fold([3] * 6), "B": _per_fold([2] * 6)}, excluded=("Z",))

    assert result.excluded == ("Z",)
    assert result.candidates == ("A", "B")
    assert _pbo(result) == 0.0
    assert all("Z" not in s.is_best for s in result.splits)


def test_a_candidate_cannot_be_ranked_and_excluded() -> None:
    with pytest.raises(StatisticalInputError) as error:
        _run({"A": _per_fold([3] * 6), "B": _per_fold([2] * 6)}, excluded=("B",))

    _refused(error, "both ranked and excluded")


def test_a_primary_metric_other_than_net_expectancy_is_undefined() -> None:
    result = _run({"A": _per_fold([3] * 6), "B": _per_fold([2] * 6)}, metric="sharpe")

    assert result.pbo.value is None
    assert result.pbo.undefined_reason == "the primary metric 'sharpe' is not implemented"


def test_a_candidate_with_no_out_of_sample_trade_makes_pbo_undefined_and_is_named() -> None:
    """B trades only in folds 2-5, so split 0 (test folds 0, 1) leaves it nothing out of sample."""

    result = _run(
        {"A": _per_fold([3] * 6), "B": _trades([(13, 13, 1.0), (18, 18, 1.0)])}, SPLITS[:1]
    )

    assert result.pbo.value is None
    assert result.pbo.undefined_reason == "candidate B has no out-of-sample trade in split 0"


def test_a_candidate_with_no_in_sample_trade_makes_pbo_undefined_and_is_named() -> None:
    """B trades only in folds 0 and 1: split 0 tests exactly those folds."""

    result = _run({"A": _per_fold([3] * 6), "B": _trades([(3, 3, 1.0), (8, 8, 1.0)])}, SPLITS[:1])

    assert result.pbo.value is None
    assert result.pbo.undefined_reason == "candidate B has no in-sample trade in split 0"


def test_the_embargo_reaches_the_in_sample_sample() -> None:
    """Split 0 tests folds 0-1 (days 1-10). B's only train trade is on day 11, the day after.

    Embargo 0 leaves it in sample; embargo 1 removes it: entry 11 <= 10 + 1, exit 11 >= 6 - purge.
    """

    candidates = {"A": _per_fold([3] * 6), "B": _trades([(3, 3, 1.0), (11, 11, 1.0)])}

    assert _pbo(_run(candidates, SPLITS[:1], embargo=0)) is not None
    held = _run(candidates, SPLITS[:1], embargo=1)
    assert held.pbo.undefined_reason == "candidate B has no in-sample trade in split 0"


def test_the_purge_reaches_the_in_sample_sample() -> None:
    """Split 5 tests folds 1-2 (days 6-15). B's train trade is on day 5, the day before.

    Purge 0 keeps it (exit 5 < 6); purge 1 removes it (exit 5 >= 6 - 1)."""

    candidates = {"A": _per_fold([3] * 6), "B": _trades([(5, 5, 1.0), (8, 8, 1.0)])}

    assert _pbo(_run(candidates, SPLITS[5:6], purge=0)) is not None
    held = _run(candidates, SPLITS[5:6], purge=1)
    assert held.pbo.undefined_reason == "candidate B has no in-sample trade in split 5"


def test_pbo_needs_evidence_digests_and_at_least_one_split() -> None:
    candidates = {"A": _per_fold([3] * 6), "B": _per_fold([2] * 6)}
    with pytest.raises(StatisticalInputError) as no_evidence:
        pbo(
            candidates, (), DAYS, FOLDS, SPLITS,
            purge_days=0, embargo_days=0, primary_metric=PRIMARY_METRIC,
            evidence_sha256=(), basis_is_mark_to_market=True,
        )  # fmt: skip
    with pytest.raises(StatisticalInputError) as no_splits:
        _run(candidates, [])

    _refused(no_evidence, "digests of the evidence")
    _refused(no_splits, "at least one split")


def test_a_measurement_carries_every_digest_and_the_basis_grade() -> None:
    candidates = {"A": _per_fold([3] * 6), "B": _per_fold([2] * 6)}

    graded = _run(candidates)
    legacy = _run(candidates, grade=False)
    undefined = _run({"A": _per_fold([3] * 6)}, grade=False)

    assert graded.pbo.name == "pbo"
    assert graded.pbo.evidence_sha256 == DIGESTS
    assert graded.pbo.basis_is_mark_to_market is True
    assert legacy.pbo.basis_is_mark_to_market is False
    assert (undefined.pbo.evidence_sha256, undefined.pbo.basis_is_mark_to_market) == (
        DIGESTS,
        False,
    )


def test_the_result_never_holds_a_non_finite_float() -> None:
    """The logit's infinities are a count (``overfit``), never an emitted float."""

    result = _run({"A": _per_fold([3] * 6), "B": _per_fold([2] * 6)})

    for s in result.splits:
        assert 0.0 <= s.oos_lambda <= 1.0
    assert np.isfinite(result.pbo.value)  # type: ignore[arg-type]


def test_a_split_whose_overfit_flag_contradicts_its_lambda_is_refused() -> None:
    def split(lam: float, overfit: bool) -> PboSplit:
        return PboSplit(index=0, is_best=("A",), oos_lambda=lam, overfit=overfit)

    assert split(0.5, True).overfit is True  # the boundary is overfit
    assert split(0.0, True).overfit is True
    assert split(0.75, False).overfit is False
    with pytest.raises(ValueError, match="exactly lambda <= 1/2"):
        split(0.5, False)
    with pytest.raises(ValueError, match="exactly lambda <= 1/2"):
        split(0.9, True)
    with pytest.raises(ValueError, match="exactly lambda <= 1/2"):
        split(0.1, False)
