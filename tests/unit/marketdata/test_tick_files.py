import hashlib
from datetime import date
from decimal import Decimal
from pathlib import Path

import numpy as np
import pytest

from trading_house.core.errors import EvidenceIntegrityError
from trading_house.marketdata.tick_files import day_path, read_day_file, write_day_file
from trading_house.marketdata.ticks import TickArrays, day_digest

DAY = date(2026, 10, 6)
POINT = Decimal("0.00001")


def _arrays(first_bid: int = 110000) -> TickArrays:
    return TickArrays(
        time_ms=np.array([10, 20, 20], dtype=np.int64),
        bid=np.array([first_bid, 110001, 110002], dtype=np.int64),
        ask=np.array([110010, 110011, 110012], dtype=np.int64),
        flags=np.array([6, 2, 4], dtype=np.uint16),
    )


def _same(a: TickArrays, b: TickArrays) -> bool:
    return all(
        np.array_equal(getattr(a, name), getattr(b, name))
        and getattr(a, name).dtype == getattr(b, name).dtype
        for name in ("time_ms", "bid", "ask", "flags")
    )


def test_the_path_is_instrument_year_day(tmp_path: Path) -> None:
    expected = tmp_path / "fx.eurusd" / "2026" / "2026-10-06.npz"
    assert day_path(tmp_path, "fx.eurusd", DAY) == expected


def test_a_written_day_reads_back_identically_and_returns_its_digest(tmp_path: Path) -> None:
    digest = write_day_file(tmp_path, "fx.eurusd", DAY, POINT, _arrays())
    stored = read_day_file(day_path(tmp_path, "fx.eurusd", DAY))
    assert _same(stored, _arrays())
    assert digest == day_digest("fx.eurusd", DAY, POINT, _arrays())


def test_rewriting_the_same_ticks_is_a_recognised_retry(tmp_path: Path) -> None:
    first = write_day_file(tmp_path, "fx.eurusd", DAY, POINT, _arrays())
    assert write_day_file(tmp_path, "fx.eurusd", DAY, POINT, _arrays()) == first


def _orphans(tmp_path: Path) -> list[Path]:
    return sorted(day_path(tmp_path, "fx.eurusd", DAY).parent.glob(f"{DAY}.orphan-*.npz"))


def test_an_unrecorded_file_with_different_ticks_is_kept_aside_and_replaced(
    tmp_path: Path,
) -> None:
    """Ingest writes only days no settled row vouches for, so a file already there
    is an orphan (e.g. a crash after writing, then a different answer on retry).
    It must not block the instrument, and it is never deleted."""

    path = day_path(tmp_path, "fx.eurusd", DAY)
    write_day_file(tmp_path, "fx.eurusd", DAY, POINT, _arrays())
    old_bytes = path.read_bytes()

    digest = write_day_file(tmp_path, "fx.eurusd", DAY, POINT, _arrays(first_bid=109999))

    assert digest == day_digest("fx.eurusd", DAY, POINT, _arrays(first_bid=109999))
    assert _same(read_day_file(path), _arrays(first_bid=109999))
    own = hashlib.sha256(old_bytes).hexdigest()[:12]
    assert _orphans(tmp_path) == [path.with_name(f"2026-10-06.orphan-{own}.npz")]
    assert _orphans(tmp_path)[0].read_bytes() == old_bytes


def test_an_unreadable_unrecorded_file_is_kept_aside_and_replaced(tmp_path: Path) -> None:
    path = day_path(tmp_path, "fx.eurusd", DAY)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"not a zip")

    write_day_file(tmp_path, "fx.eurusd", DAY, POINT, _arrays())

    assert _same(read_day_file(path), _arrays())
    assert [orphan.read_bytes() for orphan in _orphans(tmp_path)] == [b"not a zip"]


def test_a_recognised_retry_leaves_no_orphan(tmp_path: Path) -> None:
    write_day_file(tmp_path, "fx.eurusd", DAY, POINT, _arrays())
    write_day_file(tmp_path, "fx.eurusd", DAY, POINT, _arrays())
    assert _orphans(tmp_path) == []


def test_the_digest_does_not_depend_on_how_the_file_was_compressed(tmp_path: Path) -> None:
    """The same ticks saved uncompressed hash the same: the digest covers ticks, not zip bytes."""

    path = day_path(tmp_path, "fx.eurusd", DAY)
    path.parent.mkdir(parents=True)
    arrays = _arrays()
    with path.open("wb") as handle:
        np.savez(handle, time_ms=arrays.time_ms, bid=arrays.bid, ask=arrays.ask, flags=arrays.flags)
    assert day_digest("fx.eurusd", DAY, POINT, read_day_file(path)) == day_digest(
        "fx.eurusd", DAY, POINT, arrays
    )


def test_a_missing_file_is_an_integrity_failure(tmp_path: Path) -> None:
    with pytest.raises(EvidenceIntegrityError):
        read_day_file(day_path(tmp_path, "fx.eurusd", DAY))


def test_a_file_with_an_extra_array_is_refused(tmp_path: Path) -> None:
    path = day_path(tmp_path, "fx.eurusd", DAY)
    path.parent.mkdir(parents=True)
    arrays = _arrays()
    with path.open("wb") as handle:
        np.savez_compressed(
            handle,
            time_ms=arrays.time_ms,
            bid=arrays.bid,
            ask=arrays.ask,
            flags=arrays.flags,
            extra=arrays.bid,
        )
    with pytest.raises(EvidenceIntegrityError):
        read_day_file(path)


def test_a_corrupt_file_is_refused(tmp_path: Path) -> None:
    path = day_path(tmp_path, "fx.eurusd", DAY)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"not a zip")
    with pytest.raises(EvidenceIntegrityError):
        read_day_file(path)


def test_a_file_with_zip_magic_but_no_zip_is_refused(tmp_path: Path) -> None:
    """File starts with zip magic but contains garbage.

    This triggers zipfile.BadZipFile, not ValueError.
    """
    path = day_path(tmp_path, "fx.eurusd", DAY)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"PK\x03\x04" + b"garbage" * 10)
    with pytest.raises(EvidenceIntegrityError):
        read_day_file(path)


def test_a_stale_partial_from_a_crash_does_not_block_the_write(tmp_path: Path) -> None:
    path = day_path(tmp_path, "fx.eurusd", DAY)
    path.parent.mkdir(parents=True)
    path.with_name(f"{path.stem}.partial").write_bytes(b"half")
    write_day_file(tmp_path, "fx.eurusd", DAY, POINT, _arrays())
    assert _same(read_day_file(path), _arrays())
