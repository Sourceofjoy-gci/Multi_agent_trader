from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from trading_house.core.errors import TimestampError


def ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise TimestampError("timestamp must be timezone-aware")
    return value.astimezone(UTC)


class Clock(Protocol):
    def now(self) -> datetime: ...


@dataclass(frozen=True, slots=True)
class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class FixedClock:
    instant: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "instant", ensure_utc(self.instant))

    def now(self) -> datetime:
        return self.instant
