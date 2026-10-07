"""One immutable compressed file per instrument per UTC day.

The only module in ``src/`` that opens a tick file (an acceptance test says so).
A file is written under a temporary name and renamed into place; a file already
in place is only ever confirmed, never replaced.
"""

from __future__ import annotations

import zipfile
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Final

import numpy as np

from trading_house.core.errors import EvidenceIntegrityError
from trading_house.marketdata.ticks import TickArrays, day_digest

_ARRAYS: Final = ("time_ms", "bid", "ask", "flags")


def day_path(root: Path, instrument_id: str, day: date) -> Path:
    return root / instrument_id / f"{day.year:04d}" / f"{day.isoformat()}.npz"


def read_day_file(path: Path) -> TickArrays:
    try:
        with np.load(path, allow_pickle=False) as data:
            if sorted(data.files) != sorted(_ARRAYS):
                raise EvidenceIntegrityError() from ValueError(f"{path.name} has the wrong arrays")
            return TickArrays(
                time_ms=data["time_ms"].astype(np.int64),
                bid=data["bid"].astype(np.int64),
                ask=data["ask"].astype(np.int64),
                flags=data["flags"].astype(np.uint16),
            )
    except (OSError, ValueError, zipfile.BadZipFile) as error:
        raise EvidenceIntegrityError() from error


def write_day_file(
    root: Path, instrument_id: str, day: date, point_size: Decimal, arrays: TickArrays
) -> str:
    """Write one day's ticks once and return their digest.

    Rewriting identical ticks is a recognised retry -- a crash between writing the
    file and recording its row must not strand the day. Different ticks at the same
    path are refused: a stored day is never revised.
    """

    digest = day_digest(instrument_id, day, point_size, arrays)
    path = day_path(root, instrument_id, day)
    if path.exists():
        if day_digest(instrument_id, day, point_size, read_day_file(path)) != digest:
            raise EvidenceIntegrityError() from ValueError(f"{path.name} holds different ticks")
        return digest
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(f"{path.stem}.partial")
    partial.unlink(missing_ok=True)
    with partial.open("xb") as handle:
        np.savez_compressed(
            handle, time_ms=arrays.time_ms, bid=arrays.bid, ask=arrays.ask, flags=arrays.flags
        )
    partial.rename(path)
    return digest
