"""The window digest and the two refusals that compare it (Phase 8E, Task 3)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tests.unit.research.backtest.conftest import FakeBarReader
from tests.unit.research.backtest.test_dataset import _bar, _reference
from trading_house.core.errors import CoverageError, ScenarioEvidenceError, StatisticalInputError
from trading_house.marketdata.models import Bar, Timeframe
from trading_house.ops.dataset import (
    WindowDigest,
    refuse_changed_dataset,
    refuse_dataset_mismatch,
    window_digest,
)

ORIGIN = datetime(2026, 9, 21, tzinfo=UTC)
A, B = "a" * 64, "b" * 64


def _held() -> tuple[Bar, ...]:
    """Bars 0-5 and 8-9: a gap where 6 and 7 would be."""

    return tuple(_bar(i) for i in (0, 1, 2, 3, 4, 5, 8, 9))


def _at(index: int) -> datetime:
    return ORIGIN + timedelta(minutes=15 * index)


def _digest(reader: FakeBarReader, first: int, last: int) -> WindowDigest:
    return window_digest(
        reader,
        instrument_id="fx.eurusd",
        timeframe=Timeframe.M15,
        start=_at(first),
        end=_at(last),
    )


def test_the_window_digest_is_the_independent_digest_of_the_bars_the_window_reads() -> None:
    reader = FakeBarReader(_held())

    found = _digest(reader, 2, 5)

    assert found.dataset_sha256 == _reference([_bar(i) for i in (2, 3, 4, 5)])
    assert (found.bar_count, found.first_event_time, found.last_event_time) == (4, _at(2), _at(5))


def test_a_window_across_a_gap_hashes_the_bars_that_exist() -> None:
    found = _digest(FakeBarReader(_held()), 4, 9)

    assert found.bar_count == 4
    assert found.dataset_sha256 == _reference([_bar(i) for i in (4, 5, 8, 9)])


def test_a_window_the_store_does_not_hold_is_the_stores_coverage_refusal() -> None:
    reader = FakeBarReader(_held())

    with pytest.raises(CoverageError):
        _digest(reader, 5, 10)  # ends after the last stored bar
    with pytest.raises(CoverageError):
        _digest(reader, -1, 3)  # starts before the first
    with pytest.raises(CoverageError):
        _digest(FakeBarReader(()), 0, 1)  # an empty store holds nothing


def test_a_window_inside_the_store_that_holds_no_bar_is_a_statistical_input_refusal() -> None:
    with pytest.raises(StatisticalInputError):
        _digest(FakeBarReader(_held()), 6, 7)


def _cause(error: pytest.ExceptionInfo[ScenarioEvidenceError]) -> str:
    assert str(error.value) == "sealed scenarios do not match the declared cost grid"
    return str(error.value.__cause__)


def test_a_declared_digest_that_is_not_the_computed_one_is_refused_naming_both() -> None:
    refuse_dataset_mismatch(A, A)
    with pytest.raises(ScenarioEvidenceError) as excinfo:
        refuse_dataset_mismatch(A, B)

    assert A in _cause(excinfo)
    assert B in _cause(excinfo)


def test_a_protocol_declaring_no_digest_is_refused_not_matched() -> None:
    with pytest.raises(ScenarioEvidenceError) as excinfo:
        refuse_dataset_mismatch(None, B)

    assert B in _cause(excinfo)


def test_a_run_that_read_other_bars_than_the_preflight_hashed_is_refused() -> None:
    refuse_changed_dataset(A, A)
    for after in (B, None):
        with pytest.raises(ScenarioEvidenceError) as excinfo:
            refuse_changed_dataset(A, after)
        assert "changed during the run" in _cause(excinfo)
