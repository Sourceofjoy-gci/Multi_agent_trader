"""Persist closed MetaTrader 5 bars without ever rewriting one.

``append_bars`` inserts through
``ON CONFLICT (instrument_id, timeframe, event_time) DO NOTHING`` and then
re-reads whichever keys did not insert. When the row already on disk agrees
with what was just handed in, the write was a *duplicate* -- normal traffic,
because backfill pages overlap at their boundaries by design -- and it is
counted but never reported as a problem. When the row disagrees, the broker
has revised its own history: that is a *conflict*. The first observation
stands, the conflict is counted, and nothing is rewritten.

The alternative -- overwriting on conflict -- would mean a backtest run
today and the identical backtest run next month could produce different
numbers, with no code change and no record of why. Silence on a duplicate
and refusal on a conflict are both deliberate (spec 4.3, D-2), not
oversights: the runtime role holds only SELECT and INSERT on
``marketdata.bars`` (migration 0003), so this policy is also the only one
this module is *able* to implement -- it cannot UPDATE or DELETE a row even
if it wanted to.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Never, Protocol
from uuid import UUID

import psycopg
from psycopg import sql

from trading_house.core.errors import DatabaseUnavailableError
from trading_house.marketdata.models import Bar, BarQuality, Coverage, IngestRun, Timeframe

BarKey = tuple[str, str, datetime]
"""(instrument_id, timeframe value, event_time) -- the table's primary key."""

_BAR_COLUMNS = (
    "instrument_id",
    "timeframe",
    "event_time",
    "availability_time",
    "open",
    "high",
    "low",
    "close",
    "tick_volume",
    "spread",
    "real_volume",
    "quality",
    "ingest_run_id",
)

_INSERT_RUN_SQL = """
    INSERT INTO marketdata.ingest_runs (
        run_id, instrument_id, timeframe, requested_from, requested_to,
        started_at, finished_at, earliest_event_time, bars_returned,
        bars_stored, bars_rejected, bars_conflicting, expected_bars,
        coverage_ratio, outcome, detail
    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""

_MISSING_KEYS_SQL = """
    SELECT event_time, open, high, low, close
    FROM marketdata.bars
    WHERE instrument_id = %s AND timeframe = %s AND event_time = ANY(%s)
"""

_COVERAGE_SQL = """
    SELECT
        MIN(event_time), MAX(event_time), MAX(availability_time),
        COUNT(*) FILTER (WHERE quality = 'OK'),
        COUNT(*) FILTER (WHERE quality <> 'OK')
    FROM marketdata.bars
    WHERE instrument_id = %s AND timeframe = %s
"""

_BARS_SQL = """
    SELECT instrument_id, timeframe, event_time, availability_time,
           open, high, low, close, tick_volume, spread, real_volume, quality
    FROM marketdata.bars
    WHERE instrument_id = %s AND timeframe = %s
      AND event_time >= %s AND event_time < %s
      AND availability_time <= %s
    ORDER BY event_time
"""


class ConnectionFactory(Protocol):
    """Open one distinct runtime connection for a store operation."""

    def __call__(self) -> psycopg.Connection[tuple[Any, ...]]: ...


class _BarStoreFailure(Exception):
    """Credential- and row-free diagnostic cause for a failed store operation."""


@dataclass(frozen=True, slots=True)
class WriteResult:
    """What one ``append_bars`` call actually did to the ledger."""

    stored: int
    duplicate: int
    conflicting: int


class BarStore(Protocol):
    """The surface an ingest pipeline needs from a bar ledger."""

    def record_run(self, run: IngestRun) -> None: ...

    def append_bars(self, bars: Sequence[Bar], run_id: UUID) -> WriteResult: ...

    def bars(
        self,
        instrument_id: str,
        timeframe: Timeframe,
        *,
        start: datetime,
        end: datetime,
        as_of: datetime,
    ) -> tuple[Bar, ...]: ...

    def coverage(self, instrument_id: str, timeframe: Timeframe) -> Coverage: ...


def _raise_database_unavailable() -> Never:
    raise DatabaseUnavailableError() from _BarStoreFailure("market-data store operation failed")


def _run_params(run: IngestRun) -> tuple[Any, ...]:
    return (
        run.run_id,
        run.instrument_id,
        run.timeframe.value,
        run.requested_from,
        run.requested_to,
        run.started_at,
        run.finished_at,
        run.earliest_event_time,
        run.bars_returned,
        run.bars_stored,
        run.bars_rejected,
        run.bars_conflicting,
        run.expected_bars,
        run.coverage_ratio,
        run.outcome.value,
        run.detail,
    )


def _bar_params(bar: Bar, run_id: UUID) -> tuple[Any, ...]:
    return (
        bar.instrument_id,
        bar.timeframe.value,
        bar.event_time,
        bar.availability_time,
        bar.open,
        bar.high,
        bar.low,
        bar.close,
        bar.tick_volume,
        bar.spread,
        bar.real_volume,
        bar.quality.value,
        run_id,
    )


def _key(bar: Bar) -> BarKey:
    return (bar.instrument_id, bar.timeframe.value, bar.event_time)


def _insert_bars_statement(row_count: int) -> sql.Composed:
    """Build a multi-row INSERT with exactly ``row_count`` value groups.

    Composed with ``psycopg.sql`` rather than plain string formatting: the
    only variable is how many ``(%s, ...)`` groups appear, never any
    identifier or value, but this keeps that fact structurally obvious
    rather than relying on the reader to notice it.
    """

    columns = sql.SQL(", ").join(sql.Identifier(name) for name in _BAR_COLUMNS)
    one_row = sql.SQL("({})").format(sql.SQL(", ").join([sql.Placeholder()] * len(_BAR_COLUMNS)))
    all_rows = sql.SQL(", ").join([one_row] * row_count)
    return sql.SQL(
        "INSERT INTO marketdata.bars ({columns}) VALUES {rows} "
        "ON CONFLICT (instrument_id, timeframe, event_time) DO NOTHING "
        "RETURNING instrument_id, timeframe, event_time"
    ).format(columns=columns, rows=all_rows)


def _bar_from_row(row: tuple[Any, ...]) -> Bar:
    return Bar(
        instrument_id=row[0],
        timeframe=Timeframe(row[1]),
        event_time=row[2],
        availability_time=row[3],
        open=row[4],
        high=row[5],
        low=row[6],
        close=row[7],
        tick_volume=row[8],
        spread=row[9],
        real_volume=row[10],
        quality=BarQuality(row[11]),
    )


class PostgresBarStore:
    """Append-only access to the market-data bar ledger."""

    def __init__(self, connection_factory: ConnectionFactory) -> None:
        self._connection_factory = connection_factory

    def _connect(self) -> psycopg.Connection[tuple[Any, ...]]:
        try:
            return self._connection_factory()
        except DatabaseUnavailableError:
            raise
        except Exception:
            _raise_database_unavailable()

    def record_run(self, run: IngestRun) -> None:
        """Insert the run row a bar's foreign key will reference.

        Must happen before ``append_bars`` is called for that run: a bar's
        ``ingest_run_id`` is ``NOT NULL REFERENCES ingest_runs(run_id)``.
        """

        connection = self._connect()
        try:
            with connection, connection.cursor() as cursor:
                cursor.execute(_INSERT_RUN_SQL, _run_params(run))
        finally:
            connection.close()

    def append_bars(self, bars: Sequence[Bar], run_id: UUID) -> WriteResult:
        """Insert ``bars``, absorbing duplicates and counting conflicts.

        A foreign-key violation (an unknown ``run_id``) propagates as the
        native ``psycopg.errors.ForeignKeyViolation`` rather than being
        translated to ``DatabaseUnavailableError``: it is not a connectivity
        failure, it is the caller passing a run that was never recorded, and
        collapsing it into a generic "database unavailable" would hide
        exactly the bug the foreign key exists to catch.
        """

        if not bars:
            return WriteResult(stored=0, duplicate=0, conflicting=0)

        connection = self._connect()
        try:
            with connection, connection.cursor() as cursor:
                params: list[Any] = []
                for bar in bars:
                    params.extend(_bar_params(bar, run_id))
                cursor.execute(_insert_bars_statement(len(bars)), params)
                inserted: set[BarKey] = {(row[0], row[1], row[2]) for row in cursor.fetchall()}

                missing = [bar for bar in bars if _key(bar) not in inserted]
                stored_ohlc = self._read_back(cursor, missing)

                duplicate = 0
                conflicting = 0
                for bar in missing:
                    held = stored_ohlc.get(_key(bar))
                    if held == (bar.open, bar.high, bar.low, bar.close):
                        duplicate += 1
                    else:
                        conflicting += 1
        finally:
            connection.close()

        return WriteResult(stored=len(inserted), duplicate=duplicate, conflicting=conflicting)

    @staticmethod
    def _read_back(
        cursor: psycopg.Cursor[tuple[Any, ...]], missing: Sequence[Bar]
    ) -> dict[BarKey, tuple[Any, Any, Any, Any]]:
        """Look up the OHLC currently stored for keys that did not insert.

        Grouped by (instrument_id, timeframe) and matched with
        ``event_time = ANY(%s)`` rather than one clause per bar, so the
        query text never grows with the number of missing bars.
        """

        by_partition: dict[tuple[str, str], list[datetime]] = {}
        for bar in missing:
            by_partition.setdefault((bar.instrument_id, bar.timeframe.value), []).append(
                bar.event_time
            )

        stored: dict[BarKey, tuple[Any, Any, Any, Any]] = {}
        for (instrument_id, timeframe_value), event_times in by_partition.items():
            cursor.execute(_MISSING_KEYS_SQL, (instrument_id, timeframe_value, event_times))
            for row in cursor.fetchall():
                stored[(instrument_id, timeframe_value, row[0])] = (row[1], row[2], row[3], row[4])
        return stored

    def bars(
        self,
        instrument_id: str,
        timeframe: Timeframe,
        *,
        start: datetime,
        end: datetime,
        as_of: datetime,
    ) -> tuple[Bar, ...]:
        """Bars opening in ``[start, end)`` that were knowable by ``as_of``."""

        connection = self._connect()
        try:
            with connection, connection.cursor() as cursor:
                cursor.execute(_BARS_SQL, (instrument_id, timeframe.value, start, end, as_of))
                rows = cursor.fetchall()
        finally:
            connection.close()
        return tuple(_bar_from_row(row) for row in rows)

    def coverage(self, instrument_id: str, timeframe: Timeframe) -> Coverage:
        """What the store currently holds for one instrument and timeframe."""

        connection = self._connect()
        try:
            with connection, connection.cursor() as cursor:
                cursor.execute(_COVERAGE_SQL, (instrument_id, timeframe.value))
                row = cursor.fetchone()
        finally:
            connection.close()
        if row is None:
            _raise_database_unavailable()
        return Coverage(
            instrument_id=instrument_id,
            timeframe=timeframe,
            earliest_event_time=row[0],
            latest_event_time=row[1],
            latest_availability_time=row[2],
            clean_bars=row[3],
            defective_bars=row[4],
        )
