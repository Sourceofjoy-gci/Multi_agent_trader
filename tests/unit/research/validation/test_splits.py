"""Phase 8C1: walk-forward folds and CPCV bookkeeping.

Every expected date and index below is written out from calendar arithmetic, not
computed by the code under test. Each case also asserts the private cause of a
refusal, so a deleted clause cannot be covered for by a neighbouring one.
"""

from __future__ import annotations

from datetime import date, timedelta
from itertools import pairwise

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from trading_house.core.errors import StatisticalInputError
from trading_house.research.validation.splits import (
    WFA_STEP_MONTHS,
    CpcvFold,
    WalkForwardFold,
    add_months,
    cpcv_folds,
    cpcv_paths,
    cpcv_splits,
    hours_to_days,
    walk_forward_folds,
)


def _span(first: date, last: date) -> tuple[date, ...]:
    return tuple(first + timedelta(days=i) for i in range((last - first).days + 1))


def _refused(error: pytest.ExceptionInfo[StatisticalInputError], fragment: str) -> None:
    assert str(error.value) == StatisticalInputError.public_message
    cause = error.value.__cause__
    assert isinstance(cause, ValueError)
    assert fragment in str(cause)


def _wfa(days: tuple[date, ...], *, purge: int = 0, train: int = 24) -> tuple[WalkForwardFold, ...]:
    return walk_forward_folds(
        days, train_months=train, validation_months=6, test_months=6, purge_days=purge
    )


def _fold(index: int, *dates: str) -> WalkForwardFold:
    """train start, end; validation start, end; test start, end -- as ISO dates."""

    ts, te, vs, ve, xs, xe = (date.fromisoformat(d) for d in dates)
    return WalkForwardFold(
        index=index,
        train_start=ts,
        train_end=te,
        validation_start=vs,
        validation_end=ve,
        test_start=xs,
        test_end=xe,
    )


# --- add_months ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("start", "months", "expected"),
    [
        (date(2021, 1, 31), 1, date(2021, 2, 28)),  # non-leap February
        (date(2020, 1, 31), 1, date(2020, 2, 29)),  # leap February
        (date(2020, 2, 29), 12, date(2021, 2, 28)),  # leap day into a non-leap year
        (date(2020, 2, 29), 48, date(2024, 2, 29)),  # leap day into a leap year
        (date(2020, 3, 31), 1, date(2020, 4, 30)),
        (date(2020, 12, 15), 1, date(2021, 1, 15)),  # year rollover
        (date(2020, 12, 15), 25, date(2023, 1, 15)),
        (date(2020, 3, 31), -1, date(2020, 2, 29)),
        (date(2020, 3, 15), 0, date(2020, 3, 15)),
    ],
)
def test_add_months_clamps_to_the_end_of_the_month(
    start: date, months: int, expected: date
) -> None:
    assert add_months(start, months) == expected


@pytest.mark.parametrize(
    ("hours", "days"), [(0, 0), (1, 1), (16, 1), (24, 1), (25, 2), (48, 2), (49, 3)]
)
def test_hours_round_up_to_whole_days(hours: int, days: int) -> None:
    assert hours_to_days(hours) == days


def test_negative_hours_are_refused() -> None:
    with pytest.raises(StatisticalInputError) as error:
        hours_to_days(-1)

    _refused(error, "cannot be negative")


# --- walk-forward -------------------------------------------------------------


def test_a_five_year_series_gives_five_expanding_folds() -> None:
    days = _span(date(2020, 1, 1), date(2024, 12, 31))

    folds = _wfa(days)

    assert WFA_STEP_MONTHS == 6
    assert folds == (
        _fold(
            0,
            "2020-01-01", "2021-12-31", "2022-01-01", "2022-06-30", "2022-07-01", "2022-12-31",
        ),
        _fold(
            1,
            "2020-01-01", "2022-06-30", "2022-07-01", "2022-12-31", "2023-01-01", "2023-06-30",
        ),
        _fold(
            2,
            "2020-01-01", "2022-12-31", "2023-01-01", "2023-06-30", "2023-07-01", "2023-12-31",
        ),
        _fold(
            3,
            "2020-01-01", "2023-06-30", "2023-07-01", "2023-12-31", "2024-01-01", "2024-06-30",
        ),
        _fold(
            4,
            "2020-01-01", "2023-12-31", "2024-01-01", "2024-06-30", "2024-07-01", "2024-12-31",
        ),
    )  # fmt: skip


def test_a_fold_whose_test_window_ends_one_day_past_the_series_is_not_created() -> None:
    last_day_short = _span(date(2020, 1, 1), date(2024, 12, 30))

    folds = _wfa(last_day_short)

    assert [fold.index for fold in folds] == [0, 1, 2, 3]
    assert folds[-1].test_end == date(2024, 6, 30)


def test_exactly_the_months_one_fold_needs_gives_one_fold_and_a_day_less_gives_none() -> None:
    exact = _wfa(_span(date(2020, 1, 1), date(2022, 12, 31)))
    short = _wfa(_span(date(2020, 1, 1), date(2022, 12, 30)))

    assert exact == (
        _fold(
            0,
            "2020-01-01", "2021-12-31", "2022-01-01", "2022-06-30", "2022-07-01", "2022-12-31",
        ),
    )  # fmt: skip
    assert short == ()


def test_a_twenty_month_series_has_no_folds_and_never_a_short_one() -> None:
    assert _wfa(_span(date(2020, 1, 1), date(2021, 8, 31))) == ()


def test_boundaries_come_from_the_start_so_month_end_clamping_does_not_drift() -> None:
    """Start 31 Aug: the validation boundary clamps to 28 Feb 2023. Stepping from that
    clamped date would put the test boundary at 28 Aug; from the start it is 31 Aug."""

    days = _span(date(2020, 8, 31), date(2023, 8, 30))

    assert _wfa(days) == (
        _fold(
            0,
            "2020-08-31", "2022-08-30", "2022-08-31", "2023-02-27", "2023-02-28", "2023-08-30",
        ),
    )  # fmt: skip


def test_a_clamped_train_boundary_does_not_move_the_later_ones() -> None:
    """25 months from 31 Aug 2020 is 30 Sep 2022 (clamped). The validation boundary is 31
    months from the start, 31 Mar 2023; stepping six months from the clamped 30 Sep would
    give 30 Mar. The test boundary is 37 months from the start, 30 Sep 2023."""

    days = _span(date(2020, 8, 31), date(2023, 9, 29))

    assert _wfa(days, train=25) == (
        _fold(
            0,
            "2020-08-31", "2022-09-29", "2022-09-30", "2023-03-30", "2023-03-31", "2023-09-29",
        ),
    )  # fmt: skip


def test_the_purge_pulls_back_the_train_and_validation_ends_and_leaves_the_rest() -> None:
    days = _span(date(2020, 1, 1), date(2022, 12, 31))

    assert _wfa(days, purge=2) == (
        _fold(
            0,
            "2020-01-01", "2021-12-29", "2022-01-01", "2022-06-28", "2022-07-01", "2022-12-31",
        ),
    )  # fmt: skip
    assert _wfa(days, purge=0)[0].train_end == date(2021, 12, 31)


def test_a_sixteen_hour_purge_is_one_day() -> None:
    days = _span(date(2020, 1, 1), date(2022, 12, 31))

    folds = walk_forward_folds(
        days,
        train_months=24,
        validation_months=6,
        test_months=6,
        purge_days=hours_to_days(16),
    )

    assert (folds[0].train_end, folds[0].validation_end) == (date(2021, 12, 30), date(2022, 6, 29))


def test_a_different_train_length_moves_the_first_boundary() -> None:
    days = _span(date(2020, 1, 1), date(2022, 12, 31))

    assert _wfa(days, train=18)[0] == _fold(
        0, "2020-01-01", "2021-06-30", "2021-07-01", "2021-12-31", "2022-01-01", "2022-06-30"
    )


def test_each_window_length_is_read_from_its_own_argument() -> None:
    """Validation 3 months and test 9: the boundaries are 24, 27 and 36 months from the start,
    and the step is still six, so five folds fit 2020-01-01 .. 2024-12-31 (fold 5 would end
    on 2025-06-30). Swapping the two lengths would put the first test window at 2022-07-01."""

    folds = walk_forward_folds(
        _span(date(2020, 1, 1), date(2024, 12, 31)),
        train_months=24,
        validation_months=3,
        test_months=9,
        purge_days=0,
    )

    assert [f.test_end.isoformat() for f in folds] == [
        "2022-12-31", "2023-06-30", "2023-12-31", "2024-06-30", "2024-12-31",
    ]  # fmt: skip
    assert folds[0] == _fold(
        0, "2020-01-01", "2021-12-31", "2022-01-01", "2022-03-31", "2022-04-01", "2022-12-31"
    )
    assert folds[4] == _fold(
        4, "2020-01-01", "2023-12-31", "2024-01-01", "2024-03-31", "2024-04-01", "2024-12-31"
    )


def test_a_walk_forward_with_a_huge_window_has_no_folds_rather_than_an_overflow() -> None:
    days = _span(date(2020, 1, 1), date(2022, 12, 31))

    assert _wfa(days, train=12 * 8000) == ()
    assert _wfa(days, train=10**30) == ()


def test_the_purge_may_leave_a_one_day_train_or_validation_window() -> None:
    days = _span(date(2020, 1, 1), date(2022, 12, 31))

    train = walk_forward_folds(
        days, train_months=1, validation_months=6, test_months=6, purge_days=30
    )
    validation = walk_forward_folds(
        days, train_months=24, validation_months=1, test_months=6, purge_days=30
    )

    # One month from 1 January 2020 is 31 days, so a 30 day purge leaves the first.
    assert train[0].train_start == train[0].train_end == date(2020, 1, 1)
    # January 2022 has 31 days, so a 30 day purge leaves the first day of the window.
    assert validation[0].validation_start == validation[0].validation_end == date(2022, 1, 1)


@pytest.mark.parametrize(("train", "validation"), [(1, 6), (24, 1)], ids=["train", "validation"])
def test_a_purge_that_consumes_a_window_is_refused(train: int, validation: int) -> None:
    days = _span(date(2020, 1, 1), date(2025, 12, 31))

    with pytest.raises(StatisticalInputError) as error:
        walk_forward_folds(
            days, train_months=train, validation_months=validation, test_months=6, purge_days=31
        )

    _refused(error, "consumes a whole")


def test_a_walk_forward_over_no_days_is_refused() -> None:
    with pytest.raises(StatisticalInputError) as error:
        _wfa(())

    _refused(error, "at least one day")


@pytest.mark.parametrize("window", ["train", "validation", "test"])
@pytest.mark.parametrize("months", [0, -1])
def test_a_window_of_less_than_a_month_is_refused(window: str, months: int) -> None:
    spans = {"train": 24, "validation": 6, "test": 6} | {window: months}

    with pytest.raises(StatisticalInputError) as error:
        walk_forward_folds(
            _span(date(2020, 1, 1), date(2024, 12, 31)),
            train_months=spans["train"],
            validation_months=spans["validation"],
            test_months=spans["test"],
            purge_days=0,
        )

    _refused(error, "at least one month")


def test_a_negative_purge_is_refused() -> None:
    with pytest.raises(StatisticalInputError) as error:
        _wfa(_span(date(2020, 1, 1), date(2024, 12, 31)), purge=-1)

    _refused(error, "purge cannot be negative")


# --- CPCV folds ---------------------------------------------------------------


def test_a_hundred_days_in_six_folds_gives_the_remainder_to_the_earlier_folds() -> None:
    days = _span(date(2024, 1, 1), date(2024, 4, 9))
    assert len(days) == 100

    folds = cpcv_folds(days, 6)

    assert folds == (
        CpcvFold(index=0, start=date(2024, 1, 1), end=date(2024, 1, 17), days=17),
        CpcvFold(index=1, start=date(2024, 1, 18), end=date(2024, 2, 3), days=17),
        CpcvFold(index=2, start=date(2024, 2, 4), end=date(2024, 2, 20), days=17),
        CpcvFold(index=3, start=date(2024, 2, 21), end=date(2024, 3, 8), days=17),
        CpcvFold(index=4, start=date(2024, 3, 9), end=date(2024, 3, 24), days=16),
        CpcvFold(index=5, start=date(2024, 3, 25), end=date(2024, 4, 9), days=16),
    )
    assert sum(fold.days for fold in folds) == 100


def test_a_series_that_divides_evenly_gives_equal_folds() -> None:
    folds = cpcv_folds(_span(date(2024, 1, 1), date(2024, 1, 30)), 6)

    assert [(f.start.day, f.end.day, f.days) for f in folds] == [
        (1, 5, 5), (6, 10, 5), (11, 15, 5), (16, 20, 5), (21, 25, 5), (26, 30, 5),
    ]  # fmt: skip


def test_as_many_folds_as_days_is_one_day_each_and_more_is_refused() -> None:
    three = _span(date(2024, 1, 1), date(2024, 1, 3))

    assert [f.days for f in cpcv_folds(three, 3)] == [1, 1, 1]
    with pytest.raises(StatisticalInputError) as error:
        cpcv_folds(three[:2], 3)
    _refused(error, "more folds than days")


@pytest.mark.parametrize("n_folds", [2, 1, 0, -1])
def test_fewer_than_three_folds_is_refused_everywhere(n_folds: int) -> None:
    days = _span(date(2024, 1, 1), date(2024, 3, 1))

    for call in (
        lambda: cpcv_folds(days, n_folds),
        lambda: cpcv_splits(n_folds),
        lambda: cpcv_paths(n_folds),
    ):
        with pytest.raises(StatisticalInputError) as error:
            call()
        _refused(error, "at least 3 folds")


@settings(deadline=None, max_examples=60)
@given(length=st.integers(min_value=3, max_value=400), data=st.data())
def test_fold_sizes_sum_to_the_series_and_the_folds_are_consecutive(
    length: int, data: st.DataObject
) -> None:
    n_folds = data.draw(st.integers(min_value=3, max_value=min(12, length)))
    days = _span(date(2024, 1, 1), date(2024, 1, 1) + timedelta(days=length - 1))

    folds = cpcv_folds(days, n_folds)

    assert sum(fold.days for fold in folds) == length
    assert folds[0].start == days[0]
    assert folds[-1].end == days[-1]
    assert all(b.start == a.end + timedelta(days=1) for a, b in pairwise(folds))
    assert max(f.days for f in folds) - min(f.days for f in folds) <= 1
    assert [f.days for f in folds] == sorted((f.days for f in folds), reverse=True)


# --- CPCV splits and paths ----------------------------------------------------


def test_six_folds_give_the_fifteen_pairs_in_lexicographic_order() -> None:
    splits = cpcv_splits(6)

    assert [s.index for s in splits] == list(range(15))
    assert [s.test_folds for s in splits] == [
        (0, 1), (0, 2), (0, 3), (0, 4), (0, 5),
        (1, 2), (1, 3), (1, 4), (1, 5),
        (2, 3), (2, 4), (2, 5),
        (3, 4), (3, 5),
        (4, 5),
    ]  # fmt: skip


def test_three_folds_give_three_pairs() -> None:
    assert [s.test_folds for s in cpcv_splits(3)] == [(0, 1), (0, 2), (1, 2)]


def test_six_folds_give_five_paths_that_take_the_pth_split_holding_each_fold() -> None:
    """Splits holding each fold, in order: f0 [0,1,2,3,4]; f1 [0,5,6,7,8]; f2 [1,5,9,10,11];
    f3 [2,6,9,12,13]; f4 [3,7,10,12,14]; f5 [4,8,11,13,14]. Path p takes the p-th of each."""

    paths = cpcv_paths(6)

    assert [p.index for p in paths] == [0, 1, 2, 3, 4]
    assert [[a.split_index for a in p.assignments] for p in paths] == [
        [0, 0, 1, 2, 3, 4],
        [1, 5, 5, 6, 7, 8],
        [2, 6, 9, 9, 10, 11],
        [3, 7, 10, 12, 12, 13],
        [4, 8, 11, 13, 14, 14],
    ]
    assert all([a.fold_index for a in p.assignments] == [0, 1, 2, 3, 4, 5] for p in paths)


def test_three_folds_give_two_paths() -> None:
    paths = cpcv_paths(3)

    assert [[(a.fold_index, a.split_index) for a in p.assignments] for p in paths] == [
        [(0, 0), (1, 0), (2, 1)],
        [(0, 1), (1, 2), (2, 2)],
    ]


@settings(deadline=None, max_examples=20)
@given(n_folds=st.integers(min_value=3, max_value=12))
def test_every_path_covers_each_fold_once_from_a_split_that_tests_it(n_folds: int) -> None:
    splits, paths = cpcv_splits(n_folds), cpcv_paths(n_folds)

    assert len(paths) == n_folds - 1
    for path in paths:
        assert sorted(a.fold_index for a in path.assignments) == list(range(n_folds))
        assert all(a.fold_index in splits[a.split_index].test_folds for a in path.assignments)
    assert len({tuple(a.split_index for a in p.assignments) for p in paths}) == n_folds - 1
