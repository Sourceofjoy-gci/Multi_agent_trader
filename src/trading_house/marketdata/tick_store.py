"""The record of every tick day: what was fetched, what came back, which file holds it.

Append-only (migration 0010). A day is *settled* by one ``COMPLETE`` or ``EMPTY``
row; ``FAILED`` rows are history, and a later attempt may settle the day.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Any, Never, Protocol, Self

import psycopg
from pydantic import JsonValue, NonNegativeInt, field_validator, model_validator

from trading_house.core.clock import ensure_utc
from trading_house.core.errors import (
    DatabaseUnavailableError,
    EvidenceIntegrityError,
    TimestampError,
)
from trading_house.core.values import CanonicalModel, InstrumentId, PositiveDecimal
from trading_house.marketdata.store import ConnectionFactory

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class TickDayOutcome(str, Enum):  # noqa: UP042
    COMPLETE = "COMPLETE"
    EMPTY = "EMPTY"
    FAILED = "FAILED"


class TickDay(CanonicalModel):
    instrument_id: InstrumentId
    day: date
    outcome: TickDayOutcome
    tick_count: NonNegativeInt
    first_time_ms: int | None = None
    last_time_ms: int | None = None
    crossed_quotes: NonNegativeInt = 0
    point_size: PositiveDecimal
    file_sha256: str | None = None
    detail: str | None = None
    fetched_at: datetime

    @field_validator("fetched_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        try:
            return ensure_utc(value)
        except TimestampError as error:
            raise ValueError(str(error)) from error

    @model_validator(mode="after")
    def shape_matches_outcome(self) -> Self:
        first_time_ms = self.first_time_ms
        last_time_ms = self.last_time_ms
        if self.outcome is TickDayOutcome.COMPLETE:
            if (
                self.tick_count == 0
                or self.file_sha256 is None
                or not _SHA256.match(self.file_sha256)
                or first_time_ms is None
                or last_time_ms is None
                or self.detail is not None
            ):
                raise ValueError("a complete day has ticks, both times, a digest and no detail")
            if first_time_ms > last_time_ms:
                raise ValueError("a complete day's first tick precedes its last")
        elif self.outcome is TickDayOutcome.EMPTY:
            if (
                self.tick_count
                or self.crossed_quotes
                or self.file_sha256 is not None
                or (first_time_ms, last_time_ms) != (None, None)
                or self.detail is not None
            ):
                raise ValueError("an empty day has no ticks, times, digest or detail")
        elif not self.detail or self.file_sha256 is not None:
            raise ValueError("a failed day has a detail and no digest")
        return self


class TickDayStore(Protocol):
    def record(self, day: TickDay) -> None: ...

    def rows(self, instrument_id: str) -> tuple[TickDay, ...]: ...


def settled_days(rows: Sequence[TickDay]) -> dict[date, TickDay]:
    return {row.day: row for row in rows if row.outcome is not TickDayOutcome.FAILED}


@dataclass(frozen=True, slots=True)
class TickCoverage:
    instrument_id: str
    complete_days: int
    empty_days: int
    unsettled_failed_days: int
    ticks: int
    earliest_complete: date | None
    latest_complete: date | None
    weekday_gaps: tuple[date, ...]

    def as_json(self) -> dict[str, JsonValue]:
        earliest = None if self.earliest_complete is None else self.earliest_complete.isoformat()
        latest = None if self.latest_complete is None else self.latest_complete.isoformat()
        return {
            "complete_days": self.complete_days,
            "empty_days": self.empty_days,
            "unsettled_failed_days": self.unsettled_failed_days,
            "ticks": self.ticks,
            "earliest_complete": earliest,
            "latest_complete": latest,
            "weekday_gaps": [gap.isoformat() for gap in self.weekday_gaps],
        }


def coverage_of(instrument_id: str, rows: Sequence[TickDay]) -> TickCoverage:
    settled = settled_days(rows)
    complete = sorted(day for day, row in settled.items() if row.outcome is TickDayOutcome.COMPLETE)
    failed_days = {row.day for row in rows if row.outcome is TickDayOutcome.FAILED}
    gaps: list[date] = []
    if complete:
        day = complete[0]
        while day <= complete[-1]:
            if day.weekday() < 5 and day not in settled:
                gaps.append(day)
            day += timedelta(days=1)
    return TickCoverage(
        instrument_id=instrument_id,
        complete_days=len(complete),
        empty_days=sum(1 for row in settled.values() if row.outcome is TickDayOutcome.EMPTY),
        unsettled_failed_days=len(failed_days - set(settled)),
        ticks=sum(row.tick_count for row in settled.values()),
        earliest_complete=complete[0] if complete else None,
        latest_complete=complete[-1] if complete else None,
        weekday_gaps=tuple(gaps),
    )


class _TickStoreFailure(Exception):
    """Credential- and row-free diagnostic cause."""


def _unavailable() -> Never:
    raise DatabaseUnavailableError() from _TickStoreFailure("tick-day store operation failed")


_COLUMNS = (
    "instrument_id, day, outcome, tick_count, first_time_ms, last_time_ms, crossed_quotes, "
    "point_size, file_sha256, detail, fetched_at"
)
_INSERT = (
    f"INSERT INTO marketdata.tick_days ({_COLUMNS}) "  # noqa: S608
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
)
_SELECT = (
    f"SELECT {_COLUMNS} FROM marketdata.tick_days "  # noqa: S608
    "WHERE instrument_id = %s ORDER BY day, fetched_at"
)


class PostgresTickDayStore:
    def __init__(self, connection_factory: ConnectionFactory) -> None:
        self._connection_factory = connection_factory

    def _connect(self) -> psycopg.Connection[tuple[Any, ...]]:
        try:
            return self._connection_factory()
        except DatabaseUnavailableError:
            raise
        except Exception:
            _unavailable()

    def record(self, day: TickDay) -> None:
        connection = self._connect()
        try:
            with connection, connection.cursor() as cursor:
                cursor.execute(
                    _INSERT,
                    (
                        day.instrument_id,
                        day.day,
                        day.outcome.value,
                        day.tick_count,
                        day.first_time_ms,
                        day.last_time_ms,
                        day.crossed_quotes,
                        day.point_size,
                        day.file_sha256,
                        day.detail,
                        day.fetched_at,
                    ),
                )
        except psycopg.errors.UniqueViolation as error:
            # A settled day recorded twice: a second writer raced this one, or a
            # caller skipped the settled-day check. Either way history is not revised.
            raise EvidenceIntegrityError() from error
        except psycopg.Error:
            _unavailable()
        finally:
            connection.close()

    def rows(self, instrument_id: str) -> tuple[TickDay, ...]:
        connection = self._connect()
        try:
            with connection, connection.cursor() as cursor:
                cursor.execute(_SELECT, (instrument_id,))
                fetched = cursor.fetchall()
        except psycopg.Error:
            _unavailable()
        finally:
            connection.close()
        return tuple(
            TickDay(
                instrument_id=row[0],
                day=row[1],
                outcome=TickDayOutcome(row[2]),
                tick_count=row[3],
                first_time_ms=row[4],
                last_time_ms=row[5],
                crossed_quotes=row[6],
                point_size=Decimal(row[7]),
                file_sha256=row[8],
                detail=row[9],
                fetched_at=row[10],
            )
            for row in fetched
        )
