"""One immutable compressed file per instrument per UTC day.

The only module in ``src/`` that opens a tick file (an acceptance test says so).
A file is written under a temporary name and renamed into place. A file vouched
for by a settled row is never rewritten (ingest never writes that day again); an
unvouched file is preserved aside and replaced.
"""

from __future__ import annotations

import hashlib
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
    """Write one day's ticks and return their digest.

    Ingest calls this only for a day no settled row vouches for, so a file already
    at the path is unrecorded. Identical ticks are a recognised retry -- a crash
    between writing the file and recording its row must not strand the day.
    Anything else there (different ticks, or a file that no longer reads) is an
    orphan: it is renamed aside to ``<day>.orphan-<first 12 hex of its SHA-256>.npz``,
    kept and never deleted, and the new ticks take the path.
    """

    digest = day_digest(instrument_id, day, point_size, arrays)
    path = day_path(root, instrument_id, day)
    if path.exists():
        try:
            if day_digest(instrument_id, day, point_size, read_day_file(path)) == digest:
                return digest
        except EvidenceIntegrityError:
            pass  # unreadable: an orphan like any other
        own = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
        path.replace(path.with_name(f"{day.isoformat()}.orphan-{own}.npz"))
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(f"{path.stem}.partial")
    partial.unlink(missing_ok=True)
    with partial.open("xb") as handle:
        np.savez_compressed(
            handle, time_ms=arrays.time_ms, bid=arrays.bid, ask=arrays.ask, flags=arrays.flags
        )
    partial.rename(path)
    return digest
