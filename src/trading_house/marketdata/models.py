"""What a stored bar is, and the arithmetic that decides when it was knowable.

``availability_time`` is the whole point of this module. A bar stamped 09:00
did not exist at 09:00; it existed at 09:01, when it closed. Everything that
reads market data filters on availability, never on the event time, and that
is what keeps a backtest from seeing its own future (I-17).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Self
from uuid import UUID

from pydantic import NonNegativeInt, field_validator, model_validator

from trading_house.core.clock import ensure_utc
from trading_house.core.errors import TimestampError
from trading_house.core.values import CanonicalModel, InstrumentId, NonNegativeDecimal


class Timeframe(str, Enum):  # noqa: UP042
    M1 = "M1"
    M5 = "M5"
    M15 = "M15"
    H1 = "H1"
    H4 = "H4"
    D1 = "D1"


class BarQuality(str, Enum):  # noqa: UP042
    OK = "OK"
    OHLC_INCOHERENT = "OHLC_INCOHERENT"
    NON_POSITIVE_PRICE = "NON_POSITIVE_PRICE"
    NEGATIVE_SPREAD = "NEGATIVE_SPREAD"
    MISALIGNED_TIMESTAMP = "MISALIGNED_TIMESTAMP"


class IngestOutcome(str, Enum):  # noqa: UP042
    COMPLETE = "COMPLETE"
    TRUNCATED = "TRUNCATED"
    SPARSE = "SPARSE"
    EMPTY = "EMPTY"
    FAILED = "FAILED"


_SECONDS: dict[Timeframe, int] = {
    Timeframe.M1: 60,
    Timeframe.M5: 300,
    Timeframe.M15: 900,
    Timeframe.H1: 3_600,
    Timeframe.H4: 14_400,
    Timeframe.D1: 86_400,
}


def duration(timeframe: Timeframe) -> timedelta:
    return timedelta(seconds=_SECONDS[timeframe])


def availability_of(timeframe: Timeframe, event_time: datetime) -> datetime:
    """When a bar opening at ``event_time`` became knowable: when it closed."""

    return event_time + duration(timeframe)


def is_aligned(timeframe: Timeframe, event_time: datetime, *, server_offset_seconds: int) -> bool:
    """Whether a bar's open sits on a timeframe boundary in the BROKER's frame.

    Not in UTC. A broker running UTC+3 opens its H4 bars at 21:00, 01:00 and
    05:00 UTC, none of which divide 14400 -- a modulo against UTC would reject
    every H4 and D1 bar the broker ever sent. Adding the offset back recovers
    the server epoch, which is where the boundary actually is.
    """

    server_epoch = event_time.timestamp() + server_offset_seconds
    return int(server_epoch) % _SECONDS[timeframe] == 0


class Bar(CanonicalModel):
    """One closed bar, exactly as the broker reported it."""

    instrument_id: InstrumentId
    timeframe: Timeframe
    event_time: datetime
    availability_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    tick_volume: NonNegativeInt
    spread: int
    real_volume: NonNegativeInt
    quality: BarQuality

    @field_validator("event_time", "availability_time")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        try:
            return ensure_utc(value)
        except TimestampError as error:
            raise ValueError(str(error)) from error

    @model_validator(mode="after")
    def a_clean_bar_is_coherent(self) -> Self:
        """Defective bars carry whatever the broker sent (D-2). A bar claiming
        to be clean while holding an impossible price is a different thing: a
        contradiction, and refusing it here stops a gate bug reaching storage.
        """

        if self.quality is BarQuality.OK and min(self.open, self.high, self.low, self.close) <= 0:
            raise ValueError("a clean bar cannot carry a non-positive price")
        if self.availability_time <= self.event_time:
            raise ValueError("a bar is available only after it closes")
        return self


class Coverage(CanonicalModel):
    """What the store actually holds for one instrument and timeframe."""

    instrument_id: InstrumentId
    timeframe: Timeframe
    earliest_event_time: datetime | None
    latest_event_time: datetime | None
    latest_availability_time: datetime | None
    clean_bars: NonNegativeInt
    defective_bars: NonNegativeInt


class IngestRun(CanonicalModel):
    """One backfill or update attempt, and what it actually achieved.

    Exists so that a run which asked for a year and received a week leaves a
    record. Without it, "less data" and "the market was closed" look the same
    from the outside, forever.
    """

    run_id: UUID
    instrument_id: InstrumentId
    timeframe: Timeframe
    requested_from: datetime
    requested_to: datetime
    started_at: datetime
    finished_at: datetime
    earliest_event_time: datetime | None
    bars_returned: NonNegativeInt
    bars_stored: NonNegativeInt
    bars_rejected: NonNegativeInt
    bars_conflicting: NonNegativeInt
    expected_bars: NonNegativeInt
    coverage_ratio: NonNegativeDecimal
    outcome: IngestOutcome
    detail: str | None

    @field_validator("requested_from", "requested_to", "started_at", "finished_at")
    @classmethod
    def normalize_run_timestamps(cls, value: datetime) -> datetime:
        try:
            return ensure_utc(value)
        except TimestampError as error:
            raise ValueError(str(error)) from error
