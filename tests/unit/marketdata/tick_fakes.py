"""Fakes shared by the tick unit, CLI and acceptance tests."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, time

import numpy as np

from trading_house.core.errors import BrokerUnavailableError, EvidenceIntegrityError
from trading_house.marketdata.tick_store import TickDay, TickDayOutcome
from trading_house.marketdata.ticks import RawTicks, epoch_ms


def raw_ticks(
    times_ms: list[int], *, bid: float = 1.10000, ask: float = 1.10010, last: float = 0.0
) -> RawTicks:
    n = len(times_ms)
    return RawTicks(
        time_ms=np.array(times_ms, dtype=np.int64),
        bid=np.full(n, bid),
        ask=np.full(n, ask),
        last=np.full(n, last),
        volume=np.zeros(n, dtype=np.int64),
        volume_real=np.zeros(n),
        flags=np.full(n, 6, dtype=np.int64),
    )


def day_ticks(day: date, count: int = 48) -> RawTicks:
    """``count`` ticks spread evenly over the UTC day, starting at midnight."""

    start = epoch_ms(datetime.combine(day, time(), UTC))
    step = 86_400_000 // count
    return raw_ticks([start + i * step for i in range(count)])


class FakeTickProvider:
    """Serves ``ticks_for(day)`` for every day; raises for days in ``failing``."""

    def __init__(
        self,
        ticks_for: Callable[[date], RawTicks] = lambda day: RawTicks.empty(),
        *,
        failing: frozenset[date] = frozenset(),
    ) -> None:
        self.ticks_for = ticks_for
        self.failing = failing
        self.calls: list[tuple[str, datetime, datetime]] = []

    def ticks(self, instrument_id: str, start: datetime, end: datetime) -> RawTicks:
        self.calls.append((instrument_id, start, end))
        if start.date() in self.failing:
            raise BrokerUnavailableError()
        return self.ticks_for(start.date()).within(epoch_ms(start), epoch_ms(end))

    def days_called(self) -> list[date]:
        return sorted({start.date() for _instrument, start, _end in self.calls})


class InMemoryTickDayStore:
    def __init__(self) -> None:
        self.recorded: list[TickDay] = []

    def record(self, day: TickDay) -> None:
        if day.outcome is not TickDayOutcome.FAILED and any(
            row.instrument_id == day.instrument_id
            and row.day == day.day
            and row.outcome is not TickDayOutcome.FAILED
            for row in self.recorded
        ):
            raise EvidenceIntegrityError()
        self.recorded.append(day)

    def rows(self, instrument_id: str) -> tuple[TickDay, ...]:
        return tuple(
            sorted(
                (row for row in self.recorded if row.instrument_id == instrument_id),
                key=lambda row: (row.day, row.fetched_at),
            )
        )


def weekdays_only(day: date) -> RawTicks:
    return day_ticks(day) if day.weekday() < 5 else RawTicks.empty()
