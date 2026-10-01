"""Phase 8C1: purge and embargo sampling (R-3) and the CPCV paths built on it.

The series is January 2024 in six five-day folds: f0 = 1-5, f1 = 6-10, f2 = 11-15,
f3 = 16-20, f4 = 21-25, f5 = 26-30. Every expected index below is worked out from
those dates and the three rules in ``split_samples``, not taken from the code.
Each trade is written ``(entry day, exit day)``, both in January unless a month is
named, and belongs to the fold containing its exit day.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import numpy as np
import pytest

from trading_house.core.errors import StatisticalInputError
from trading_house.research.trial_ledger import ReturnSeriesBasis
from trading_house.research.validation.sampling import (
    MAX_ZONE_DAYS,
    path_return_series,
    path_trade_sample,
    split_samples,
)
from trading_house.research.validation.series import ReturnSeries, TradeSample
from trading_house.research.validation.splits import (
    CpcvAssignment,
    CpcvFold,
    CpcvPath,
    cpcv_paths,
    cpcv_splits,
)

DAYS = tuple(date(2024, 1, 1) + timedelta(days=i) for i in range(30))


def _fold(index: int, first: int, last: int) -> CpcvFold:
    return CpcvFold(
        index=index, start=date(2024, 1, first), end=date(2024, 1, last), days=last - first + 1
    )


FOLDS = tuple(_fold(i, 5 * i + 1, 5 * i + 5) for i in range(6))


def _sample(*spans: tuple[date | int, date | int]) -> TradeSample:
    entries = [d if isinstance(d, date) else date(2024, 1, d) for d, _ in spans]
    exits = [d if isinstance(d, date) else date(2024, 1, d) for _, d in spans]
    entry_at = tuple(datetime(d.year, d.month, d.day, 12, tzinfo=UTC) for d in entries)
    exit_at = tuple(datetime(d.year, d.month, d.day, 13, tzinfo=UTC) for d in exits)
    return TradeSample(
        entry_day=tuple(entries),
        exit_day=tuple(exits),
        entry_at=entry_at,
        exit_at=exit_at,
        net_pnl=np.arange(len(spans), dtype=np.float64),
        session=("london",) * len(spans),
    )


def _refused(error: pytest.ExceptionInfo[StatisticalInputError], fragment: str) -> None:
    assert str(error.value) == StatisticalInputError.public_message
    cause = error.value.__cause__
    assert isinstance(cause, ValueError)
    assert fragment in str(cause)


def _split(
    sample: TradeSample, tests: set[int], *, purge: int, embargo: int
) -> tuple[list[int], list[int]]:
    train, test = split_samples(sample, DAYS, FOLDS, tests, purge_days=purge, embargo_days=embargo)
    return train.tolist(), test.tolist()


# One trade per case of a fold-2 test (11-15) with a fold-1 predecessor (6-10):
#  0 (1,2)   f0 well before every zone
#  1 (8,9)   f1, ends the day before the purge zone of purge 1, inside it at purge 3
#  2 (9,10)  f1, exits on the last day of the purge zone at purge 1
#  3 (10,10) f1, lies entirely inside the purge zone
#  4 (9,12)  f2, enters before the test fold, spans its start
#  5 (11,12) f2, enters on the test fold's first day
#  6 (10,11) f2, enters on the day before the test fold: exactly the boundary
#  7 (14,15) f2, wholly inside
#  8 (15,16) f3, enters on the test fold's last day
#  9 (16,16) f3, enters on the day the embargo (1) reaches
# 10 (17,18) f3, enters the day after the embargo
# 11 (12,17) f3, enters inside the test fold and exits after it
# 12 (20,21) f4, far after
# 13 (11,15) f2, wholly inside, exactly the fold
STRADDLING = _sample(
    (1, 2), (8, 9), (9, 10), (10, 10), (9, 12), (11, 12), (10, 11),
    (14, 15), (15, 16), (16, 16), (17, 18), (12, 17), (20, 21), (11, 15),
)  # fmt: skip


def test_each_exclusion_zone_removes_exactly_the_trades_that_touch_it() -> None:
    train, test = _split(STRADDLING, {2}, purge=1, embargo=1)

    # Test: 4 spans the boundary (entry 9 <= 10) and 6 enters on day 10 itself.
    # Train: 2 and 3 reach the purge zone (exit >= 11 - 1), 8, 9 and 11 the embargo
    # zone (entry <= 15 + 1).
    assert test == [5, 7, 13]
    assert train == [0, 1, 10, 12]


def test_without_a_purge_no_test_trade_is_dropped_and_the_train_zone_starts_at_the_fold() -> None:
    train, test = _split(STRADDLING, {2}, purge=0, embargo=1)

    assert test == [4, 5, 6, 7, 13]
    assert train == [0, 1, 2, 3, 10, 12]


def test_a_longer_purge_widens_the_train_zone_backwards() -> None:
    train, test = _split(STRADDLING, {2}, purge=3, embargo=1)

    assert test == [5, 7, 13]
    assert train == [0, 10, 12]  # exit >= 11 - 3 now also removes 1 (exit 9)


def test_the_embargo_widens_the_train_zone_forwards_and_zero_is_the_fold_end() -> None:
    assert _split(STRADDLING, {2}, purge=1, embargo=0)[0] == [0, 1, 9, 10, 12]
    assert _split(STRADDLING, {2}, purge=1, embargo=3)[0] == [0, 1, 12]


def test_a_test_fold_after_a_test_fold_has_no_boundary_between_them() -> None:
    """Folds 1 and 2 held out together: only fold 1 has a non-test predecessor (f0, whose
    last day is 5), and no trade enters by day 5. Trades 1, 2, 3, 4 and 6 enter by day 10 but
    cross no boundary, because fold 2's predecessor is itself a test fold."""

    train, test = _split(STRADDLING, {1, 2}, purge=1, embargo=1)

    assert test == [1, 2, 3, 4, 5, 6, 7, 13]
    assert train == [0, 10, 12]


def test_the_first_fold_has_no_predecessor_so_a_trade_entering_before_the_series_is_kept() -> None:
    december = date(2023, 12, 31)
    sample = _sample((december, 2), (1, 2), (6, 7), (7, 8))

    train, test = _split(sample, {0}, purge=1, embargo=1)

    # Test: both exit in f0; nothing precedes it, so nothing is purged.
    # Train: (6,7) enters on day 6 = 5 + 1, inside the embargo; (7,8) enters on day 7, outside.
    assert test == [0, 1]
    assert train == [3]


def test_as_written_r3_drops_an_earlier_test_folds_trades_at_a_later_isolated_boundary() -> None:
    """The rule as specified, pinned so a change to it is a decision and not an accident.

    Held out {0, 2}: fold 2 has a non-test predecessor (f1), and the test-side clause is
    ``entry_day <= s - 1`` for *every* test trade, so a trade wholly inside fold 0 is dropped
    because of fold 2's boundary even though it is nowhere near it. Flagged in the 8C1 report.
    """

    _, test = _split(_sample((2, 3), (12, 13)), {0, 2}, purge=1, embargo=0)

    assert test == [1]


def test_an_empty_sample_splits_into_two_empty_index_arrays() -> None:
    train, test = split_samples(_sample(), DAYS, FOLDS, {2}, purge_days=1, embargo_days=1)

    assert train.tolist() == []
    assert test.tolist() == []
    assert train.dtype == test.dtype == np.intp


def test_a_trade_belongs_to_the_fold_containing_its_exit_day() -> None:
    sample = _sample((1, 10), (1, 11), (1, 5), (1, 6), (1, 30), (1, 1))

    # With no purge and no embargo and enter-day 1 for all, only exit decides the side.
    train, test = _split(sample, {2}, purge=0, embargo=0)

    assert test == [1]  # exits on day 11, the first day of f2; (1, 10) exits a day earlier
    assert train == [0, 2, 3, 5]  # only (1, 30) reaches the zone [11, 15 + 0]


@pytest.mark.parametrize("tests", [set(), {-1}, {6}, {0, 6}])
def test_test_folds_must_be_a_non_empty_subset_of_the_folds(tests: set[int]) -> None:
    with pytest.raises(StatisticalInputError) as error:
        _split(_sample((1, 2)), tests, purge=1, embargo=1)

    _refused(error, "non-empty subset")


def test_every_fold_may_be_held_out_and_leaves_no_train_sample() -> None:
    train, test = _split(_sample((1, 2), (14, 30)), set(range(6)), purge=0, embargo=0)

    assert (train, test) == ([], [0, 1])


@pytest.mark.parametrize(
    ("purge", "embargo"), [(-1, 0), (0, -1), (MAX_ZONE_DAYS + 1, 0), (0, MAX_ZONE_DAYS + 1)]
)
def test_a_negative_or_absurd_zone_is_refused(purge: int, embargo: int) -> None:
    with pytest.raises(StatisticalInputError) as error:
        _split(_sample((1, 2)), {2}, purge=purge, embargo=embargo)

    _refused(error, "between 0 and")


def test_a_zone_of_exactly_the_ceiling_is_accepted() -> None:
    train, test = _split(_sample((1, 2), (12, 12)), {2}, purge=MAX_ZONE_DAYS, embargo=MAX_ZONE_DAYS)

    assert (train, test) == ([], [1])


def test_a_trade_that_exits_outside_the_series_is_refused_either_side() -> None:
    with pytest.raises(StatisticalInputError) as before:
        _split(_sample((date(2023, 12, 30), date(2023, 12, 31))), {2}, purge=1, embargo=1)
    _refused(before, "before the series starts")
    with pytest.raises(StatisticalInputError) as after:
        _split(_sample((30, date(2024, 1, 31))), {2}, purge=1, embargo=1)
    _refused(after, "after the series ends")


def test_a_trade_exiting_on_the_first_or_last_day_of_the_series_is_placed() -> None:
    train, _ = _split(_sample((1, 1), (30, 30)), {2}, purge=1, embargo=1)

    assert train == [0, 1]


def test_folds_that_are_not_the_series_cut_into_blocks_are_refused() -> None:
    sample = _sample((1, 2))
    late_start = (_fold(0, 2, 5), *FOLDS[1:])
    early_end = (*FOLDS[:-1], _fold(5, 26, 29))
    gap = (*FOLDS[:2], _fold(2, 12, 15), *FOLDS[3:])

    for folds, fragment in (
        (late_start, "does not start with the series"),
        (early_end, "does not end with the series"),
        (gap, "not consecutive"),
    ):
        with pytest.raises(StatisticalInputError) as error:
            split_samples(sample, DAYS, folds, {2}, purge_days=1, embargo_days=1)
        _refused(error, fragment)


@pytest.mark.parametrize(("days", "folds"), [((), FOLDS), (DAYS, ())], ids=["no days", "no folds"])
def test_no_days_or_no_folds_is_refused(
    days: tuple[date, ...], folds: tuple[CpcvFold, ...]
) -> None:
    with pytest.raises(StatisticalInputError) as error:
        split_samples(_sample(), days, folds, {0}, purge_days=1, embargo_days=1)

    _refused(error, "no days or no folds")


# --- paths --------------------------------------------------------------------

# One trade per fold interior and one straddling each boundary: T0 (2,3) f0, T1 (4,6) f1,
# T2 (8,8) f1, T3 (9,11) f2, T4 (13,13) f2, T5 (15,16) f3, T6 (18,18) f3, T7 (20,21) f4,
# T8 (23,23) f4, T9 (25,26) f5, T10 (28,28) f5.
#
# In a split, a test trade is dropped when its entry day is <= 5t for some test fold t >= 1
# whose predecessor is not a test fold (5t is the day before fold t starts). So each split
# has one threshold M, the largest such 5t, and drops the trades with entry <= M:
#   split: 0:none 1:10 2:15 3:20 4:25 5:5 6:15 7:20 8:25 9:10 10:20 11:25 12:15 13:25 14:20
# Path p takes for fold j the p-th split holding j (see test_splits), so for purge 1:
#   p0 splits [0,0,1,2,3,4]   p1 [1,5,5,6,7,8]   p2 [2,6,9,9,10,11]
#   p3 [3,7,10,12,12,13]      p4 [4,8,11,13,14,14]
TRADES = _sample(
    (2, 3), (4, 6), (8, 8), (9, 11), (13, 13), (15, 16),
    (18, 18), (20, 21), (23, 23), (25, 26), (28, 28),
)  # fmt: skip

PATH_KEPT = [
    [0, 1, 2, 4, 6, 8, 10],
    [2, 3, 4, 6, 8, 10],
    [4, 5, 6, 8, 10],
    [6, 7, 8, 10],
    [8, 9, 10],
]
PATH_EXCLUDED = [
    [3, 5, 7, 9],
    [0, 1, 5, 7, 9],
    [0, 1, 2, 3, 7, 9],
    [0, 1, 2, 3, 4, 5, 9],
    [0, 1, 2, 3, 4, 5, 6, 7],
]


def test_each_path_keeps_the_trades_its_assigned_splits_do_not_purge() -> None:
    splits = cpcv_splits(6)

    for path, kept, excluded in zip(cpcv_paths(6), PATH_KEPT, PATH_EXCLUDED, strict=True):
        got_kept, got_excluded = path_trade_sample(
            TRADES, DAYS, FOLDS, splits, path, purge_days=1, embargo_days=0
        )
        assert (got_kept.tolist(), got_excluded.tolist()) == (kept, excluded), path.index
        assert len(got_kept) + len(got_excluded) == len(TRADES)


def test_the_paths_differ_only_because_of_the_purge() -> None:
    splits = cpcv_splits(6)

    kept = {
        purge: [
            path_trade_sample(TRADES, DAYS, FOLDS, splits, path, purge_days=purge, embargo_days=0)[
                0
            ].tolist()
            for path in cpcv_paths(6)
        ]
        for purge in (0, 1)
    }

    assert kept[0] == [list(range(11))] * 5
    assert kept[1] == PATH_KEPT


def test_a_path_must_hold_out_every_fold_exactly_once() -> None:
    splits = cpcv_splits(6)
    twice = CpcvPath(
        index=0,
        assignments=tuple(CpcvAssignment(split_index=0, fold_index=f) for f in (0, 1, 1, 2, 3, 4)),
    )
    short = CpcvPath(index=0, assignments=cpcv_paths(6)[0].assignments[:5])

    for path in (twice, short):
        with pytest.raises(StatisticalInputError) as error:
            path_trade_sample(TRADES, DAYS, FOLDS, splits, path, purge_days=1, embargo_days=0)
        _refused(error, "every fold exactly once")


@pytest.mark.parametrize("split_index", [1, 99], ids=["not testing it", "beyond"])
def test_a_path_must_take_a_fold_from_a_split_that_tests_it(split_index: int) -> None:
    assignments = list(cpcv_paths(6)[0].assignments)
    assignments[1] = CpcvAssignment(split_index=split_index, fold_index=1)  # split 1 = (0, 2)
    path = CpcvPath(index=0, assignments=tuple(assignments))

    with pytest.raises(StatisticalInputError) as error:
        path_trade_sample(TRADES, DAYS, FOLDS, cpcv_splits(6), path, purge_days=1, embargo_days=0)

    _refused(error, "does not test it")


# --- return series of a path --------------------------------------------------


def _series(count: int) -> ReturnSeries:
    first = date(2024, 1, 1)
    values = np.arange(count, dtype=np.float64) / 100
    return ReturnSeries(
        days=tuple(first + timedelta(days=i) for i in range(count)),
        values=values,
        basis=ReturnSeriesBasis.MARK_TO_MARKET,
        evidence_sha256="a" * 64,
    )


def test_a_paths_return_series_is_the_folds_own_values_with_no_inserted_day() -> None:
    series = _series(33)
    # 33 days in uneven folds: Jan 1-6, 7-12, 13-18, 19-23, 24-28, 29-Feb 2.
    folds = (
        CpcvFold(index=0, start=date(2024, 1, 1), end=date(2024, 1, 6), days=6),
        CpcvFold(index=1, start=date(2024, 1, 7), end=date(2024, 1, 12), days=6),
        CpcvFold(index=2, start=date(2024, 1, 13), end=date(2024, 1, 18), days=6),
        CpcvFold(index=3, start=date(2024, 1, 19), end=date(2024, 1, 23), days=5),
        CpcvFold(index=4, start=date(2024, 1, 24), end=date(2024, 1, 28), days=5),
        CpcvFold(index=5, start=date(2024, 1, 29), end=date(2024, 2, 2), days=5),
    )

    for path in cpcv_paths(6):
        result = path_return_series(series, folds, path)

        assert result.values.tolist() == [n / 100 for n in range(33)]
        assert result.days == series.days
        assert len(result.values) == sum(fold.days for fold in folds) == 33
        assert result.basis is ReturnSeriesBasis.MARK_TO_MARKET
        assert result.evidence_sha256 == "a" * 64
        assert result.values is not series.values
        assert not result.values.flags.writeable


def test_a_paths_return_series_refuses_folds_that_do_not_tile_the_series() -> None:
    series = _series(30)

    with pytest.raises(StatisticalInputError) as error:
        path_return_series(series, (_fold(0, 2, 5), *FOLDS[1:]), cpcv_paths(6)[0])
    _refused(error, "does not start with the series")


def test_a_paths_return_series_refuses_a_path_that_skips_a_fold() -> None:
    skipping = CpcvPath(index=0, assignments=cpcv_paths(6)[0].assignments[:4])

    with pytest.raises(StatisticalInputError) as error:
        path_return_series(_series(30), FOLDS, skipping)

    _refused(error, "every fold exactly once")
