"""Tick backfill and update: one UTC day at a time, 24 hourly requests each.

The recorded days are the cursor, as stored bars are for bar ingest. A day is
fetched only once it has closed (a minute past midnight UTC). A lost broker
stops the run after recording the day as ``FAILED``; bad data records ``FAILED``
and the walk goes on. A backfill infers the broker's history wall from five
consecutive weekday ``EMPTY`` days rather than guessing it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Final, Protocol

from pydantic import JsonValue

from trading_house.core.clock import Clock
from trading_house.core.errors import BrokerUnavailableError, ConfigurationError
from trading_house.marketdata.tick_files import write_day_file
from trading_house.marketdata.tick_store import TickDay, TickDayOutcome, TickDayStore, settled_days
from trading_house.marketdata.ticks import RawTicks, TickDataError, to_tick_arrays

HOURS_PER_DAY: Final = 24
WALL_EMPTY_WEEKDAYS: Final = 5
SETTLE_MARGIN: Final = timedelta(minutes=1)


class TickProvider(Protocol):
    def ticks(self, instrument_id: str, start: datetime, end: datetime) -> RawTicks: ...


def last_closed_day(clock: Clock) -> date:
    return (clock.now() - SETTLE_MARGIN).date() - timedelta(days=1)


def fetch_day(
    provider: TickProvider,
    store: TickDayStore,
    root: Path,
    clock: Clock,
    *,
    instrument_id: str,
    day: date,
    point_size: Decimal,
) -> TickDay:
    midnight = datetime.combine(day, time(), UTC)
    try:
        hours = [
            provider.ticks(
                instrument_id,
                midnight + timedelta(hours=hour),
                midnight + timedelta(hours=hour + 1),
            )
            for hour in range(HOURS_PER_DAY)
        ]
        arrays = to_tick_arrays(RawTicks.concatenate(hours), point_size)
    except BrokerUnavailableError:
        store.record(
            TickDay(
                instrument_id=instrument_id,
                day=day,
                point_size=point_size,
                outcome=TickDayOutcome.FAILED,
                tick_count=0,
                detail="BrokerUnavailableError",
                fetched_at=clock.now(),
            )
        )
        raise
    except TickDataError as error:
        row = TickDay(
            instrument_id=instrument_id,
            day=day,
            point_size=point_size,
            outcome=TickDayOutcome.FAILED,
            tick_count=0,
            detail=f"TickDataError: {error}",
            fetched_at=clock.now(),
        )
        store.record(row)
        return row
    if len(arrays) == 0:
        row = TickDay(
            instrument_id=instrument_id,
            day=day,
            point_size=point_size,
            outcome=TickDayOutcome.EMPTY,
            tick_count=0,
            fetched_at=clock.now(),
        )
    else:
        row = TickDay(
            instrument_id=instrument_id,
            day=day,
            point_size=point_size,
            outcome=TickDayOutcome.COMPLETE,
            tick_count=len(arrays),
            first_time_ms=int(arrays.time_ms[0]),
            last_time_ms=int(arrays.time_ms[-1]),
            crossed_quotes=arrays.crossed_quotes(),
            file_sha256=write_day_file(root, instrument_id, day, point_size, arrays),
            fetched_at=clock.now(),
        )
    store.record(row)
    return row


@dataclass(frozen=True, slots=True)
class TickRunSummary:
    instrument_id: str
    recorded: tuple[TickDay, ...]
    wall_reached: bool
    earliest_complete: date | None

    def as_json(self) -> dict[str, JsonValue]:
        def count(outcome: TickDayOutcome) -> int:
            return sum(1 for row in self.recorded if row.outcome is outcome)

        return {
            "instrument_id": self.instrument_id,
            "days_complete": count(TickDayOutcome.COMPLETE),
            "days_empty": count(TickDayOutcome.EMPTY),
            "days_failed": count(TickDayOutcome.FAILED),
            "ticks": sum(row.tick_count for row in self.recorded),
            "wall_reached": self.wall_reached,
            "earliest_complete": None
            if self.earliest_complete is None
            else self.earliest_complete.isoformat(),
        }


def backfill_ticks(
    provider: TickProvider,
    store: TickDayStore,
    root: Path,
    clock: Clock,
    *,
    instrument_id: str,
    point_size: Decimal,
    until: date,
) -> TickRunSummary:
    settled = settled_days(store.rows(instrument_id))
    recorded: list[TickDay] = []
    empty_weekdays = 0
    earliest: date | None = None
    wall = False
    day = last_closed_day(clock)
    while day >= until:
        row = settled.get(day)
        if row is None:
            row = fetch_day(
                provider,
                store,
                root,
                clock,
                instrument_id=instrument_id,
                day=day,
                point_size=point_size,
            )
            recorded.append(row)
        if row.outcome is TickDayOutcome.COMPLETE:
            earliest = day
            empty_weekdays = 0
        elif row.outcome is TickDayOutcome.EMPTY and day.weekday() < 5:
            empty_weekdays += 1
            if empty_weekdays >= WALL_EMPTY_WEEKDAYS:
                wall = True
                break
        day -= timedelta(days=1)
    return TickRunSummary(instrument_id, tuple(recorded), wall, earliest)


def update_ticks(
    provider: TickProvider,
    store: TickDayStore,
    root: Path,
    clock: Clock,
    *,
    instrument_id: str,
    point_size: Decimal,
) -> TickRunSummary:
    settled = settled_days(store.rows(instrument_id))
    if not settled:
        raise ConfigurationError() from ValueError("no settled tick day yet: run backfill first")
    recorded: list[TickDay] = []
    earliest: date | None = None
    day = max(settled) + timedelta(days=1)
    last = last_closed_day(clock)
    while day <= last:
        row = fetch_day(
            provider,
            store,
            root,
            clock,
            instrument_id=instrument_id,
            day=day,
            point_size=point_size,
        )
        recorded.append(row)
        if row.outcome is TickDayOutcome.COMPLETE and earliest is None:
            earliest = day
        day += timedelta(days=1)
    return TickRunSummary(instrument_id, tuple(recorded), False, earliest)
