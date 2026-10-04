"""The append-only write path: duplicates are silent, conflicts are counted."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import pytest
from psycopg import sql
from pydantic import SecretStr

from tests.integration.marketdata.conftest import NINE, _bar, seed
from trading_house.database.connection import open_runtime_connection
from trading_house.marketdata.models import BarQuality, Timeframe
from trading_house.marketdata.store import BarStore, PostgresBarStore

if TYPE_CHECKING:
    from ...conftest import DatabaseHarness

pytestmark = pytest.mark.integration


class _CountingCursor:
    def __init__(
        self,
        cursor: Any,
        insert_parameter_counts: list[int],
        read_back_count: list[int],
    ) -> None:
        self._cursor = cursor
        self._insert_parameter_counts = insert_parameter_counts
        self._read_back_count = read_back_count

    def __enter__(self) -> _CountingCursor:
        self._cursor.__enter__()
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> object:
        return self._cursor.__exit__(exc_type, exc_value, traceback)

    def execute(self, statement: Any, parameters: Any = None) -> Any:
        rendered = statement.as_string() if isinstance(statement, sql.Composed) else statement
        if isinstance(rendered, str):
            normalized = rendered.lstrip()
            if normalized.startswith("INSERT INTO marketdata.bars"):
                self._insert_parameter_counts.append(len(parameters))
            elif normalized.startswith("SELECT event_time, open, high, low, close"):
                self._read_back_count[0] += 1
        return self._cursor.execute(statement, parameters)

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._cursor.fetchall()

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._cursor.fetchone()


class _CountingConnection:
    def __init__(
        self,
        connection: Any,
        insert_parameter_counts: list[int],
        read_back_count: list[int],
    ) -> None:
        self._connection = connection
        self._insert_parameter_counts = insert_parameter_counts
        self._read_back_count = read_back_count

    def __enter__(self) -> _CountingConnection:
        self._connection.__enter__()
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> object:
        return self._connection.__exit__(exc_type, exc_value, traceback)

    def cursor(self) -> _CountingCursor:
        return _CountingCursor(
            self._connection.cursor(),
            self._insert_parameter_counts,
            self._read_back_count,
        )

    def close(self) -> None:
        self._connection.close()


def test_appending_the_same_bar_twice_stores_it_once(bar_store: BarStore) -> None:
    """Page boundaries overlap by design, so a second identical write is
    normal traffic and must be silent -- not an error, and not a conflict."""

    first = seed(bar_store, [_bar(0)])
    second = seed(bar_store, [_bar(0)])

    assert first.stored == 1
    assert second.stored == 0
    assert second.duplicate == 1
    assert second.conflicting == 0


def test_a_refetch_that_disagrees_is_counted_not_applied(bar_store: BarStore) -> None:
    """Brokers revise history. The first observation stands, the disagreement
    is counted, and nothing is silently rewritten (spec 4.3). Overwriting
    would mean a backtest run today and the same backtest run next month
    could differ with no code change and no record of why."""

    seed(bar_store, [_bar(0)])
    revised = _bar(0).model_copy(update={"close": Decimal("9.99999")})

    result = seed(bar_store, [revised])

    assert result.conflicting == 1
    assert result.stored == 0
    held = bar_store.bars(
        "fx.eurusd",
        Timeframe.M1,
        start=NINE,
        end=NINE + timedelta(hours=1),
        as_of=NINE + timedelta(hours=1),
    )
    assert held[0].close == Decimal("1.10020")


def test_a_defective_bar_is_stored_alongside_clean_ones(bar_store: BarStore) -> None:
    """D-2: the forensic record is the point. A gate I decide was too strict
    next month can only be re-adjudicated against data that was kept."""

    result = seed(bar_store, [_bar(0), _bar(1, quality=BarQuality.OHLC_INCOHERENT)])

    assert result.stored == 2
    assert bar_store.coverage("fx.eurusd", Timeframe.M1).defective_bars == 1


def test_a_bar_cannot_be_written_without_a_run_to_blame(bar_store: BarStore) -> None:
    """The foreign key is the point: every bar is traceable to the run that
    fetched it, so a suspect window can be tied back to how it was obtained."""

    import psycopg

    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        bar_store.append_bars([_bar(0)], run_id=uuid4())


def test_two_identical_bars_in_one_batch_count_once_and_are_not_lost(
    bar_store: BarStore,
) -> None:
    """Every bar handed in must land in exactly one counter. A batch whose
    counts do not add up to its length is hiding something."""

    result = seed(bar_store, [_bar(0), _bar(0)])

    assert result.stored == 1
    assert result.duplicate == 1
    assert result.stored + result.duplicate + result.conflicting == 2


def test_two_disagreeing_bars_in_one_batch_report_a_conflict(
    bar_store: BarStore,
) -> None:
    """The same disagreement across two calls is counted as a conflict.
    Arriving inside one call must not make it vanish."""

    revised = _bar(0).model_copy(update={"close": Decimal("9.99999")})

    result = seed(bar_store, [_bar(0), revised])

    assert result.stored == 1
    assert result.conflicting == 1


def test_a_large_batch_is_chunked_and_counted_across_chunk_boundaries(
    bar_store: BarStore, database: DatabaseHarness
) -> None:
    safe_chunk_size = 65_535 // 13
    bars = tuple(_bar(index) for index in range(safe_chunk_size + 1))
    duplicate = bars[0]
    conflict = bars[safe_chunk_size]
    revised = conflict.model_copy(update={"close": Decimal("9.99999")})
    seed(bar_store, [duplicate, revised])

    insert_parameter_counts: list[int] = []
    read_back_count = [0]
    counted_store = PostgresBarStore(
        lambda: _CountingConnection(
            open_runtime_connection(SecretStr(database.runtime_dsn)),
            insert_parameter_counts,
            read_back_count,
        )
    )

    result = seed(counted_store, bars)

    assert result.stored == safe_chunk_size - 1
    assert result.duplicate == 1
    assert result.conflicting == 1
    assert result.stored + result.duplicate + result.conflicting == len(bars)
    assert insert_parameter_counts == [safe_chunk_size * 13, 13]
    assert all(count <= 65_535 for count in insert_parameter_counts)
    assert read_back_count == [1]


def test_the_run_ledger_records_what_the_write_actually_did(
    bar_store: BarStore, database: DatabaseHarness
) -> None:
    """bars_stored is the count the store accepted, not the count the broker
    returned. A re-run over already-stored history stores nothing, and the
    ledger has to say so."""

    seed(bar_store, [_bar(0)])
    second_run_id = uuid4()
    second = seed(bar_store, [_bar(0)], run_id=second_run_id)

    assert second.stored == 0
    assert second.duplicate == 1

    with (
        open_runtime_connection(SecretStr(database.runtime_dsn)) as connection,
        connection.cursor() as cursor,
    ):
        cursor.execute(
            "SELECT bars_stored, bars_conflicting FROM marketdata.ingest_runs WHERE run_id = %s",
            (second_run_id,),
        )
        row = cursor.fetchone()

    assert row == (0, 0)


# One trigger function enforces both rules (migrations 0008 and 0009) with one
# message each, so a test names the rule it expects rather than accepting
# whichever CheckViolation fires first.
AFTER_FINISH = "closes after its ingest run"
OUTSIDE_WINDOW = "is outside the window its ingest run"


def test_a_run_cannot_vouch_for_a_bar_that_closed_after_it_finished(
    bar_store: BarStore,
) -> None:
    """A run can only have fetched bars that had closed by the time it finished.

    The run requests ``[NINE, NINE + 1h)`` but finishes at NINE + 30min, so
    both bars sit inside its window and only their closing times differ: the
    bar opening at minute 29 closes exactly as the run finishes and is
    accepted, the one opening at minute 30 closes after it and is refused.
    """

    import psycopg

    run_id = uuid4()
    seed(bar_store, [_bar(29)], run_id=run_id, finished_at=NINE + timedelta(minutes=30))

    with pytest.raises(psycopg.errors.CheckViolation, match=AFTER_FINISH):
        bar_store.append_bars([_bar(30)], run_id=run_id)


def test_a_superuser_cannot_file_later_bars_under_a_finished_run(
    bar_store: BarStore, database: DatabaseHarness
) -> None:
    """The 2026-10-01 incident: a hand-typed superuser INSERT that filed
    synthetic bars under a run that had already finished.

    A grant cannot stop a superuser and the store was never in the path, so
    the refusal has to live in the table itself. The bar here sits inside the
    run's window, so it is the finish-time rule that has to refuse it.
    """

    import psycopg

    run_id = uuid4()
    seed(bar_store, [_bar(0)], run_id=run_id, finished_at=NINE + timedelta(minutes=30))
    later = NINE + timedelta(minutes=40)

    with (
        psycopg.connect(database.test_superuser_dsn, autocommit=True) as connection,
        connection.cursor() as cursor,
        pytest.raises(psycopg.errors.CheckViolation, match=AFTER_FINISH),
    ):
        cursor.execute(
            "INSERT INTO marketdata.bars (instrument_id, timeframe, event_time, "
            "availability_time, open, high, low, close, tick_volume, spread, "
            "real_volume, quality, ingest_run_id) "
            "VALUES ('fx.eurusd', 'M1', %s, %s, 1.1, 1.1, 1.1, 1.1, 100, 10, 0, 'OK', %s)",
            (later, later + timedelta(minutes=1), run_id),
        )


def test_a_run_cannot_vouch_for_a_bar_before_its_window(bar_store: BarStore) -> None:
    """``seed`` requests ``[NINE, NINE + 1h)``. A bar opening a minute before
    closed long before the run finished, so the finish-time rule lets it
    through; only the requested window says that run never asked for it."""

    import psycopg

    run_id = uuid4()
    seed(bar_store, [_bar(0)], run_id=run_id)

    with pytest.raises(psycopg.errors.CheckViolation, match=OUTSIDE_WINDOW):
        bar_store.append_bars([_bar(-1)], run_id=run_id)


def test_a_run_cannot_vouch_for_the_bar_opening_at_its_window_end(
    bar_store: BarStore,
) -> None:
    """The window is half-open, as ingest plans it. A run that finished an
    hour after ``requested_to`` could have seen the bar opening there, but it
    never asked for it."""

    import psycopg

    run_id = uuid4()
    seed(bar_store, [_bar(0)], run_id=run_id, finished_at=NINE + timedelta(hours=2))

    with pytest.raises(psycopg.errors.CheckViolation, match=OUTSIDE_WINDOW):
        bar_store.append_bars([_bar(60)], run_id=run_id)
