"""Walk-forward folds and CPCV bookkeeping over a series of UTC days.

Phase 8C1, umbrella 7.2-7.3. Pure calendar arithmetic on dates: nothing here
reads a return, so nothing here can be tuned to one. The outputs are
``CanonicalModel``s so a command can print them as they are.

A walk-forward fold that does not fit the series is **not created**, and a
series that fits none returns an empty tuple. The caller reports that as
undefined; this module never fabricates a short fold to have something to show.
"""

from __future__ import annotations

import calendar
from collections.abc import Sequence
from datetime import date, timedelta
from itertools import combinations, pairwise

from pydantic import NonNegativeInt

from trading_house.core.values import CanonicalModel
from trading_house.research.validation.series import refusal

WFA_STEP_MONTHS = 6
"""The fold step, fixed by umbrella 7.2 and not a protocol field."""

MIN_CPCV_FOLDS = 3

MAX_ZONE_DAYS = 36_500
"""A purge or embargo longer than a century is a typo, not a policy."""


def require_zone(purge_days: int, embargo_days: int) -> None:
    """Refuse a purge or embargo outside ``0 .. MAX_ZONE_DAYS`` days, before any date arithmetic."""

    for days in (purge_days, embargo_days):
        if days < 0 or days > MAX_ZONE_DAYS:
            raise refusal(f"a purge or embargo must be between 0 and {MAX_ZONE_DAYS} days")


def add_months(day: date, months: int) -> date:
    """``day`` plus whole calendar months, the day clamped to the month's last day."""

    index = day.year * 12 + (day.month - 1) + months
    year, month_index = divmod(index, 12)
    month = month_index + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def hours_to_days(hours: int) -> int:
    """Whole days, rounded up: the series has no sub-day resolution to purge at (S-4)."""

    if hours < 0:
        raise refusal("a purge or embargo cannot be negative")
    return -(-hours // 24)


class WalkForwardFold(CanonicalModel):
    """One expanding-window fold. Every date is inclusive and post-purge."""

    index: NonNegativeInt
    train_start: date
    train_end: date
    validation_start: date
    validation_end: date
    test_start: date
    test_end: date


def walk_forward_folds(
    days: Sequence[date],
    *,
    train_months: int,
    validation_months: int,
    test_months: int,
    purge_days: int,
) -> tuple[WalkForwardFold, ...]:
    """The folds of an expanding walk-forward over ``days``, possibly none.

    Training starts at ``days[0]``. Fold ``k``'s training window ends one day
    before ``train_months + k * WFA_STEP_MONTHS`` months after the start; the
    validation and test windows follow with no gap. Boundaries are each computed
    from the start, never from the previous boundary, so month-end clamping cannot
    accumulate. A fold exists only if its test window ends on or before
    ``days[-1]``.

    The last ``purge_days`` of the training and of the validation window are
    dropped, and the fold reports those effective windows. A purge that would
    consume a whole window is refused rather than leaving an empty one.
    """

    if not days:
        raise refusal("a walk-forward needs at least one day")
    if min(train_months, validation_months, test_months) < 1:
        raise refusal("every walk-forward window must span at least one month")
    require_zone(purge_days, 0)
    if any(later - earlier != timedelta(days=1) for earlier, later in pairwise(days)):
        raise refusal("the walk-forward days must be contiguous and in order")
    start, last = days[0], days[-1]
    one = timedelta(days=1)
    purge = timedelta(days=purge_days)
    folds: list[WalkForwardFold] = []
    while True:
        offset = train_months + len(folds) * WFA_STEP_MONTHS
        try:
            train_boundary = add_months(start, offset)
            validation_boundary = add_months(start, offset + validation_months)
            test_boundary = add_months(start, offset + validation_months + test_months)
        except (ValueError, OverflowError):
            # Past the last representable date, so certainly past the series.
            break
        test_end = test_boundary - one
        if test_end > last:
            break
        train_end = train_boundary - one - purge
        validation_end = validation_boundary - one - purge
        if train_end < start or validation_end < train_boundary:
            raise refusal("the purge consumes a whole training or validation window")
        folds.append(
            WalkForwardFold(
                index=len(folds),
                train_start=start,
                train_end=train_end,
                validation_start=train_boundary,
                validation_end=validation_end,
                test_start=validation_boundary,
                test_end=test_end,
            )
        )
    return tuple(folds)


class CpcvFold(CanonicalModel):
    """One contiguous chronological block of the series, ``days`` long."""

    index: NonNegativeInt
    start: date
    end: date
    days: NonNegativeInt


class CpcvSplit(CanonicalModel):
    """One test pair; ``index`` is its place in lexicographic order."""

    index: NonNegativeInt
    test_folds: tuple[NonNegativeInt, NonNegativeInt]


class CpcvAssignment(CanonicalModel):
    """Fold ``fold_index`` of a path is taken from split ``split_index``."""

    split_index: NonNegativeInt
    fold_index: NonNegativeInt


class CpcvPath(CanonicalModel):
    index: NonNegativeInt
    assignments: tuple[CpcvAssignment, ...]


def _require_fold_count(n_folds: int) -> None:
    if n_folds < MIN_CPCV_FOLDS:
        raise refusal(f"CPCV needs at least {MIN_CPCV_FOLDS} folds")


def cpcv_folds(days: Sequence[date], n_folds: int) -> tuple[CpcvFold, ...]:
    """``n_folds`` contiguous blocks; sizes differ by at most a day, earlier ones larger."""

    _require_fold_count(n_folds)
    if n_folds > len(days):
        raise refusal("CPCV cannot have more folds than days")
    base, remainder = divmod(len(days), n_folds)
    folds: list[CpcvFold] = []
    position = 0
    for index in range(n_folds):
        size = base + (1 if index < remainder else 0)
        folds.append(
            CpcvFold(
                index=index,
                start=days[position],
                end=days[position + size - 1],
                days=size,
            )
        )
        position += size
    return tuple(folds)


def cpcv_splits(n_folds: int) -> tuple[CpcvSplit, ...]:
    """Every pair of folds as the test set, ``C(n, 2)`` of them, lexicographic."""

    _require_fold_count(n_folds)
    return tuple(
        CpcvSplit(index=index, test_folds=(first, second))
        for index, (first, second) in enumerate(combinations(range(n_folds), 2))
    )


def cpcv_paths(n_folds: int) -> tuple[CpcvPath, ...]:
    """The ``n - 1`` paths: path ``p`` takes for fold ``j`` the ``p``-th split holding ``j``.

    Splits are counted in lexicographic order, so path 0 uses the first split that
    tests each fold and path ``n - 2`` the last. Every fold appears exactly once per
    path; one split can serve two folds of the same path.
    """

    splits = cpcv_splits(n_folds)
    holding = [
        [split.index for split in splits if fold in split.test_folds] for fold in range(n_folds)
    ]
    return tuple(
        CpcvPath(
            index=path,
            assignments=tuple(
                CpcvAssignment(split_index=holding[fold][path], fold_index=fold)
                for fold in range(n_folds)
            ),
        )
        for path in range(n_folds - 1)
    )
