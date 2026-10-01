"""Purge and embargo applied to a trade sample, and the paths built from it (R-3).

Phase 8C1. Candidates are fixed-parameter -- nothing is refit -- so the returns of
a test fold are the same in every CPCV split, and without something that makes a
split's test sample depend on which folds were held out, every path would be the
same full series. R-3 is that something: a test trade is dropped when it crosses
the start of its own exit fold into a non-test fold, and a train trade when its span
reaches the purge or embargo zone of a test fold. Paths then differ only by which
single fold's boundary-crossing trades survive. That is bookkeeping about how many
trades straddle fold starts, not a measure of out-of-sample robustness. The embargo
affects only the train sample; per-path test counts depend on the purge alone. The
rules are specified in the design (section 3, R-3) and
restated on ``split_samples``.

A trade belongs to the fold that contains its **exit** day. Days are compared as
proleptic-Gregorian ordinals, so a day count can never overflow a ``date``.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from datetime import date, timedelta
from itertools import pairwise

import numpy as np
import numpy.typing as npt

from trading_house.research.validation.series import ReturnSeries, TradeSample, refusal
from trading_house.research.validation.splits import (
    CpcvFold,
    CpcvPath,
    CpcvSplit,
    require_zone,
)

_ONE_DAY = timedelta(days=1)

IntVector = npt.NDArray[np.intp]


def _require_tiling(days: Sequence[date], folds: Sequence[CpcvFold]) -> None:
    """The folds are, exactly, the series cut into consecutive blocks."""

    if not days or not folds:
        raise refusal("there are no days or no folds")
    if folds[0].start != days[0]:
        raise refusal("the first fold does not start with the series")
    if folds[-1].end != days[-1]:
        raise refusal("the last fold does not end with the series")
    for earlier, later in pairwise(folds):
        if later.start != earlier.end + _ONE_DAY:
            raise refusal("the folds are not consecutive")


def _exit_folds(trades: TradeSample, folds: Sequence[CpcvFold]) -> IntVector:
    """The index of the fold containing each trade's exit day."""

    starts = np.array([fold.start.toordinal() for fold in folds], dtype=np.int64)
    exits = _ordinals(trades.exit_day)
    if len(exits) and exits.min() < starts[0]:
        raise refusal("a trade exits before the series starts")
    if len(exits) and exits.max() > folds[-1].end.toordinal():
        raise refusal("a trade exits after the series ends")
    return np.asarray(np.searchsorted(starts, exits, side="right") - 1, dtype=np.intp)


def _ordinals(days: Sequence[date]) -> npt.NDArray[np.int64]:
    return np.array([day.toordinal() for day in days], dtype=np.int64)


def _require_path(
    folds: Sequence[CpcvFold], path: CpcvPath, splits: Sequence[CpcvSplit] | None = None
) -> None:
    """The path holds out every fold exactly once, each from a split that tests it."""

    if sorted(item.fold_index for item in path.assignments) != list(range(len(folds))):
        raise refusal("a path must hold out every fold exactly once")
    if splits is None:
        return
    for item in path.assignments:
        if item.split_index >= len(splits) or (
            item.fold_index not in splits[item.split_index].test_folds
        ):
            raise refusal("a path takes a fold from a split that does not test it")


def split_samples(
    trades: TradeSample,
    days: Sequence[date],
    folds: Sequence[CpcvFold],
    test_indices: Collection[int],
    *,
    purge_days: int,
    embargo_days: int,
) -> tuple[IntVector, IntVector]:
    """The ``(train, test)`` trade indices of one split, after purge and embargo (R-3).

    Test candidates are the trades whose exit fold is in ``test_indices``; train
    candidates are the rest.

    A **test** trade is excluded only when it crosses the boundary of its OWN exit
    fold: with ``F`` the fold containing its exit day and ``s`` that fold's start,
    when fold ``F - 1`` exists and is not a test fold, ``purge_days >= 1`` and
    ``entry_day <= s - 1``. It is never dropped for another test fold's boundary.

    A **train** trade is excluded when, for any test fold ``[s, e]``,
    ``entry_day <= e + embargo_days`` and ``exit_day >= s - purge_days``. That also
    removes a trade lying wholly inside either zone.

    Why: see the module docstring. The rule is the design's R-3, written there so
    that it cannot be strengthened or relaxed after results exist (amended once, after
    the 8C1 implementation found the first test-side wording incoherent). The test side
    uses ``purge_days`` only as an on/off switch, since a trade that crosses ``s``
    necessarily reaches the zone; the train side uses its length.
    """

    require_zone(purge_days, embargo_days)
    _require_tiling(days, folds)
    tests = sorted(set(test_indices))
    if not tests or tests[0] < 0 or tests[-1] >= len(folds):
        raise refusal("the test folds must be a non-empty subset of the folds")
    fold_of = _exit_folds(trades, folds)
    in_test = np.isin(fold_of, tests)
    entries, exits = _ordinals(trades.entry_day), _ordinals(trades.exit_day)
    test_excluded = np.zeros(len(trades), dtype=np.bool_)
    train_excluded = np.zeros(len(trades), dtype=np.bool_)
    for fold in tests:
        start, end = folds[fold].start.toordinal(), folds[fold].end.toordinal()
        if purge_days >= 1 and fold >= 1 and fold - 1 not in tests:
            test_excluded |= (fold_of == fold) & (entries <= start - 1)
        train_excluded |= (entries <= end + embargo_days) & (exits >= start - purge_days)
    return (
        np.flatnonzero(~in_test & ~train_excluded).astype(np.intp),
        np.flatnonzero(in_test & ~test_excluded).astype(np.intp),
    )


def path_return_series(
    series: ReturnSeries, folds: Sequence[CpcvFold], path: CpcvPath
) -> ReturnSeries:
    """The path's held-out fold segments, concatenated in chronological order.

    Nothing is inserted between segments, so no artificial zero day exists: the
    result holds exactly the series' own values, fold by fold. Because every fold
    is held out once in every path and returns are not purged, this equals the
    full series for each path; what differs between paths is the trade sample.
    """

    _require_tiling(series.days, folds)
    _require_path(folds, path)
    origin = series.days[0].toordinal()
    pieces: list[npt.NDArray[np.float64]] = []
    held_days: list[date] = []
    for fold in folds:
        low = fold.start.toordinal() - origin
        high = fold.end.toordinal() - origin + 1
        pieces.append(series.values[low:high])
        held_days.extend(series.days[low:high])
    values = np.concatenate(pieces)
    values.flags.writeable = False
    return ReturnSeries(
        days=tuple(held_days),
        values=values,
        basis=series.basis,
        evidence_sha256=series.evidence_sha256,
    )


def path_trade_sample(
    trades: TradeSample,
    days: Sequence[date],
    folds: Sequence[CpcvFold],
    splits: Sequence[CpcvSplit],
    path: CpcvPath,
    *,
    purge_days: int,
    embargo_days: int,
) -> tuple[IntVector, IntVector]:
    """The ``(kept, excluded)`` trade indices of one path.

    For each fold the path takes the test sample of its assigned split, restricted
    to the trades that exit in that fold; the trades of that fold that sample
    dropped are the excluded ones. Both come back in ascending trade order.
    """

    _require_tiling(days, folds)
    _require_path(folds, path, splits)
    fold_of = _exit_folds(trades, folds)
    kept: list[IntVector] = []
    excluded: list[IntVector] = []
    for item in path.assignments:
        split = splits[item.split_index]
        _, test = split_samples(
            trades,
            days,
            folds,
            split.test_folds,
            purge_days=purge_days,
            embargo_days=embargo_days,
        )
        in_fold = np.flatnonzero(fold_of == item.fold_index).astype(np.intp)
        mine = np.isin(in_fold, test)
        kept.append(in_fold[mine])
        excluded.append(in_fold[~mine])
    return np.sort(np.concatenate(kept)), np.sort(np.concatenate(excluded))
