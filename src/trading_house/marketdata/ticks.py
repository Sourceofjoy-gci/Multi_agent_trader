"""Ticks: what the broker sends, what is stored, and the day digest. Pure.

``RawTicks`` is the broker's batch with its clock already in UTC, still carrying
MT5's float64 quotes and the trade-print columns. ``TickArrays`` is what is
stored: integer points, exact like ``Decimal`` and fast like numpy. The one
place a float becomes a price is ``to_tick_arrays``.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Final

import numpy as np
import numpy.typing as npt

from trading_house.core.clock import ensure_utc

TICK_DIGEST_DOMAIN: Final[bytes] = b"trading-house/ticks/v1\0"
POINT_TOLERANCE: Final[float] = 1e-6
"""How far, in points, a float64 quote may sit from the integer grid and still be
on it. 1.12427 / 0.00001 is 112426.99999999999 in binary; a genuinely off-grid
quote is off by at least a tenth of a point -- five orders of magnitude more."""

_EPOCH: Final[datetime] = datetime(1970, 1, 1, tzinfo=UTC)
_RAW_FIELDS: Final[tuple[str, ...]] = (
    "time_ms",
    "bid",
    "ask",
    "last",
    "volume",
    "volume_real",
    "flags",
)
_STORED_FIELDS: Final[tuple[str, ...]] = ("time_ms", "bid", "ask", "flags")


class TickDataError(ValueError):
    """A day's ticks cannot be stored as delivered. The message names the rule."""


def epoch_ms(instant: datetime) -> int:
    """UTC epoch milliseconds, by integer arithmetic (``timestamp()`` is a float)."""

    return (ensure_utc(instant) - _EPOCH) // timedelta(milliseconds=1)


@dataclass(frozen=True, slots=True, eq=False)
class RawTicks:
    time_ms: npt.NDArray[np.int64]
    bid: npt.NDArray[np.float64]
    ask: npt.NDArray[np.float64]
    last: npt.NDArray[np.float64]
    volume: npt.NDArray[np.int64]
    volume_real: npt.NDArray[np.float64]
    flags: npt.NDArray[np.int64]

    def __len__(self) -> int:
        return int(self.time_ms.shape[0])

    def within(self, start_ms: int, end_ms: int) -> RawTicks:
        keep = (self.time_ms >= start_ms) & (self.time_ms < end_ms)
        return RawTicks(*(getattr(self, name)[keep] for name in _RAW_FIELDS))

    @staticmethod
    def empty() -> RawTicks:
        return RawTicks(
            time_ms=np.empty(0, dtype=np.int64),
            bid=np.empty(0, dtype=np.float64),
            ask=np.empty(0, dtype=np.float64),
            last=np.empty(0, dtype=np.float64),
            volume=np.empty(0, dtype=np.int64),
            volume_real=np.empty(0, dtype=np.float64),
            flags=np.empty(0, dtype=np.int64),
        )

    @staticmethod
    def concatenate(parts: Sequence[RawTicks]) -> RawTicks:
        if not parts:
            return RawTicks.empty()
        return RawTicks(
            *(np.concatenate([getattr(part, name) for part in parts]) for name in _RAW_FIELDS)
        )


@dataclass(frozen=True, slots=True, eq=False)
class TickArrays:
    time_ms: npt.NDArray[np.int64]
    bid: npt.NDArray[np.int64]
    ask: npt.NDArray[np.int64]
    flags: npt.NDArray[np.uint16]

    def __len__(self) -> int:
        return int(self.time_ms.shape[0])

    def crossed_quotes(self) -> int:
        return int(np.count_nonzero(self.ask < self.bid))

    def select(self, mask: npt.NDArray[np.bool_]) -> TickArrays:
        return TickArrays(*(getattr(self, name)[mask] for name in _STORED_FIELDS))

    @staticmethod
    def empty() -> TickArrays:
        return TickArrays(
            time_ms=np.empty(0, dtype=np.int64),
            bid=np.empty(0, dtype=np.int64),
            ask=np.empty(0, dtype=np.int64),
            flags=np.empty(0, dtype=np.uint16),
        )

    @staticmethod
    def concatenate(parts: Sequence[TickArrays]) -> TickArrays:
        if not parts:
            return TickArrays.empty()
        return TickArrays(
            *(np.concatenate([getattr(part, name) for part in parts]) for name in _STORED_FIELDS)
        )


def _points(prices: npt.NDArray[np.float64], point: float, side: str) -> npt.NDArray[np.int64]:
    scaled = prices / point
    snapped = np.rint(scaled)
    if np.any(np.abs(scaled - snapped) > POINT_TOLERANCE):
        raise TickDataError(f"a {side} is not on the point grid")
    return snapped.astype(np.int64)


def to_tick_arrays(raw: RawTicks, point_size: Decimal) -> TickArrays:
    """The storable form of one batch, or ``TickDataError`` naming the broken rule."""

    if len(raw) == 0:
        return TickArrays.empty()
    if np.any(raw.last != 0) or np.any(raw.volume != 0) or np.any(raw.volume_real != 0):
        raise TickDataError("a tick carries a trade print (last, volume or volume_real)")
    if np.any(np.diff(raw.time_ms) < 0):
        raise TickDataError("tick time goes backwards")
    if np.any(raw.bid <= 0) or np.any(raw.ask <= 0):
        raise TickDataError("a bid or ask is not positive")
    if np.any(raw.flags < 0) or np.any(raw.flags > 0xFFFF):
        raise TickDataError("tick flags do not fit in 16 bits")
    point = float(point_size)
    return TickArrays(
        time_ms=raw.time_ms.astype(np.int64),
        bid=_points(raw.bid, point, "bid"),
        ask=_points(raw.ask, point, "ask"),
        flags=raw.flags.astype(np.uint16),
    )


def _canonical_decimal(value: Decimal) -> str:
    return format(value.normalize(), "f")


def day_digest(instrument_id: str, day: date, point_size: Decimal, arrays: TickArrays) -> str:
    """SHA-256 over the arrays' little-endian bytes, never over a file's bytes.

    A compressed file's bytes depend on the zip writer; the ticks do not. The four
    arrays share one length, so their concatenation parses one way only.
    """

    digest = hashlib.sha256(TICK_DIGEST_DOMAIN)
    for part in (instrument_id, day.isoformat(), _canonical_decimal(point_size)):
        digest.update(part.encode("utf-8"))
        digest.update(b"\0")
    for array, dtype in (
        (arrays.time_ms, "<i8"),
        (arrays.bid, "<i8"),
        (arrays.ask, "<i8"),
        (arrays.flags, "<u2"),
    ):
        digest.update(np.ascontiguousarray(array, dtype=dtype).tobytes())
    return digest.hexdigest()
