"""The only way research code reads ticks: point-in-time, verified, or refused."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Final

from pydantic import JsonValue

from trading_house.core.errors import CoverageError, EvidenceIntegrityError
from trading_house.marketdata.tick_files import day_path, read_day_file
from trading_house.marketdata.tick_store import TickDay, TickDayOutcome, TickDayStore, settled_days
from trading_house.marketdata.ticks import TickArrays, canonical_decimal, day_digest

WINDOW_DIGEST_DOMAIN: Final[bytes] = b"trading-house/tick-window/v1\0"


@dataclass(frozen=True, slots=True)
class TickWindowDigest:
    sha256: str
    days: int
    ticks: int
    point_size: str | None
    """The window's one point size, canonical; ``None`` when no day is ``COMPLETE``."""

    def as_json(self) -> dict[str, JsonValue]:
        return {
            "sha256": self.sha256,
            "days": self.days,
            "ticks": self.ticks,
            "point_size": self.point_size,
        }


def _day_of(ms: int) -> date:
    return datetime.fromtimestamp(ms / 1000, UTC).date()


def _point_size_of(instrument_id: str, days: list[tuple[TickDay, TickArrays]]) -> str | None:
    """The one point size the window's ticks are counted in, or a refusal: two days
    stored in different points would read as prices a factor of ten apart."""

    complete = (row for row, _ in days if row.outcome is TickDayOutcome.COMPLETE)
    sizes = sorted({canonical_decimal(row.point_size) for row in complete})
    if len(sizes) > 1:
        msg = f"{instrument_id} mixes point sizes {sizes[0]} and {sizes[1]} in one window"
        raise CoverageError() from ValueError(msg)
    return sizes[0] if sizes else None


class TickReader:
    def __init__(self, store: TickDayStore, root: Path) -> None:
        self._store = store
        self._root = root

    def _verified(
        self, instrument_id: str, start_ms: int, end_ms: int
    ) -> list[tuple[TickDay, TickArrays]]:
        if end_ms <= start_ms:
            raise CoverageError() from ValueError("an empty tick window")
        settled = settled_days(self._store.rows(instrument_id))
        days: list[tuple[TickDay, TickArrays]] = []
        day, last = _day_of(start_ms), _day_of(end_ms - 1)
        while day <= last:
            row = settled.get(day)
            if row is None:
                raise CoverageError() from ValueError(f"{instrument_id} {day} is not settled")
            if row.outcome is TickDayOutcome.EMPTY:
                days.append((row, TickArrays.empty()))
            else:
                arrays = read_day_file(day_path(self._root, instrument_id, day))
                expected_digest = day_digest(instrument_id, day, row.point_size, arrays)
                if expected_digest != row.file_sha256:
                    msg = f"{instrument_id} {day} changed on disk"
                    raise EvidenceIntegrityError() from ValueError(msg)
                days.append((row, arrays))
            day += timedelta(days=1)
        _point_size_of(instrument_id, days)
        return days

    def ticks(self, instrument_id: str, start_ms: int, end_ms: int, *, as_of_ms: int) -> TickArrays:
        verified = self._verified(instrument_id, start_ms, end_ms)
        joined = TickArrays.concatenate([arrays for _row, arrays in verified])
        keep = (
            (joined.time_ms >= start_ms) & (joined.time_ms < end_ms) & (joined.time_ms < as_of_ms)
        )
        return joined.select(keep)

    def window_digest(self, instrument_id: str, start_ms: int, end_ms: int) -> TickWindowDigest:
        verified = self._verified(instrument_id, start_ms, end_ms)
        point_size = _point_size_of(instrument_id, verified)
        digest = hashlib.sha256(WINDOW_DIGEST_DOMAIN)
        digest.update(f"{instrument_id}\0{start_ms}\0{end_ms}\0{point_size or '-'}\0".encode())
        ticks = 0
        for row, arrays in verified:
            file_sha = row.file_sha256 or "-"
            digest.update(f"{row.day.isoformat()}\0{row.outcome.value}\0{file_sha}\0".encode())
            in_window = (arrays.time_ms >= start_ms) & (arrays.time_ms < end_ms)
            ticks += int(in_window.sum())
        return TickWindowDigest(
            sha256=digest.hexdigest(), days=len(verified), ticks=ticks, point_size=point_size
        )
