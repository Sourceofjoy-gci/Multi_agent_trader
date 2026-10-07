from datetime import UTC, date, datetime, time
from decimal import Decimal
from pathlib import Path

import numpy as np
import pytest

from tests.unit.marketdata.tick_fakes import FakeTickProvider, InMemoryTickDayStore, day_ticks
from trading_house.core.clock import FixedClock
from trading_house.core.errors import BrokerUnavailableError, CoverageError, EvidenceIntegrityError
from trading_house.marketdata.tick_files import day_path, read_day_file, write_day_file
from trading_house.marketdata.tick_ingest import fetch_day
from trading_house.marketdata.tick_reader import TickReader
from trading_house.marketdata.ticks import RawTicks, epoch_ms

POINT = Decimal("0.00001")
MON, TUE, WED = date(2026, 10, 5), date(2026, 10, 6), date(2026, 10, 7)
CLOCK = FixedClock(datetime(2026, 10, 9, 1, tzinfo=UTC))


def _ms(day: date, hour: int = 0) -> int:
    return epoch_ms(datetime.combine(day, time(hour), UTC))


def _stored(tmp_path: Path, days: dict[date, bool]) -> InMemoryTickDayStore:
    """``days`` maps a day to True (ticks) or False (an EMPTY day)."""

    store = InMemoryTickDayStore()
    for day, has_ticks in days.items():
        if has_ticks:
            provider = FakeTickProvider(day_ticks)
        else:
            provider = FakeTickProvider(lambda _day: RawTicks.empty())
        fetch_day(
            provider, store, tmp_path, CLOCK, instrument_id="fx.eurusd", day=day, point_size=POINT
        )
    return store


def test_ticks_across_two_days_come_back_in_order_and_half_open(tmp_path: Path) -> None:
    reader = TickReader(_stored(tmp_path, {MON: True, TUE: True}), tmp_path)
    arrays = reader.ticks("fx.eurusd", _ms(MON, 12), _ms(TUE, 12), as_of_ms=_ms(WED))
    assert len(arrays) == 48
    assert int(arrays.time_ms[0]) == _ms(MON, 12)
    assert int(arrays.time_ms[-1]) < _ms(TUE, 12)
    assert bool(np.all(np.diff(arrays.time_ms) > 0))


def test_nothing_after_as_of_is_returned(tmp_path: Path) -> None:
    reader = TickReader(_stored(tmp_path, {MON: True}), tmp_path)
    arrays = reader.ticks("fx.eurusd", _ms(MON), _ms(TUE), as_of_ms=_ms(MON, 6))
    assert len(arrays) == 12
    assert int(arrays.time_ms.max()) < _ms(MON, 6)


def test_an_empty_day_contributes_nothing_without_refusing(tmp_path: Path) -> None:
    reader = TickReader(_stored(tmp_path, {MON: True, TUE: False}), tmp_path)
    assert len(reader.ticks("fx.eurusd", _ms(MON), _ms(WED), as_of_ms=_ms(WED))) == 48


def test_a_day_with_no_settled_row_is_refused(tmp_path: Path) -> None:
    reader = TickReader(_stored(tmp_path, {MON: True}), tmp_path)
    with pytest.raises(CoverageError):
        reader.ticks("fx.eurusd", _ms(MON), _ms(WED), as_of_ms=_ms(WED))


def test_a_failed_only_day_is_refused(tmp_path: Path) -> None:
    store = _stored(tmp_path, {MON: True})
    with pytest.raises(BrokerUnavailableError):
        fetch_day(
            FakeTickProvider(failing=frozenset({TUE})),
            store,
            tmp_path,
            CLOCK,
            instrument_id="fx.eurusd",
            day=TUE,
            point_size=POINT,
        )
    with pytest.raises(CoverageError):
        TickReader(store, tmp_path).ticks("fx.eurusd", _ms(MON), _ms(WED), as_of_ms=_ms(WED))


def test_a_missing_file_is_refused(tmp_path: Path) -> None:
    reader = TickReader(_stored(tmp_path, {MON: True}), tmp_path)
    day_path(tmp_path, "fx.eurusd", MON).unlink()
    with pytest.raises(EvidenceIntegrityError):
        reader.ticks("fx.eurusd", _ms(MON), _ms(TUE), as_of_ms=_ms(WED))


def test_a_file_changed_on_disk_is_refused(tmp_path: Path) -> None:
    """Monday's file is replaced by a valid file holding fewer ticks; its row's digest no
    longer matches, so the reader refuses rather than serving the edited day."""

    store = _stored(tmp_path, {MON: True})
    path = day_path(tmp_path, "fx.eurusd", MON)
    original = read_day_file(path)
    path.unlink()
    mask = np.arange(len(original)) < 40
    write_day_file(tmp_path, "fx.eurusd", MON, POINT, original.select(mask))
    with pytest.raises(EvidenceIntegrityError):
        TickReader(store, tmp_path).ticks("fx.eurusd", _ms(MON), _ms(TUE), as_of_ms=_ms(WED))


def test_an_empty_window_is_refused(tmp_path: Path) -> None:
    with pytest.raises(CoverageError):
        TickReader(_stored(tmp_path, {MON: True}), tmp_path).ticks(
            "fx.eurusd", _ms(MON, 5), _ms(MON, 5), as_of_ms=_ms(WED)
        )


def test_the_window_digest_is_stable_and_names_its_window(tmp_path: Path) -> None:
    reader = TickReader(_stored(tmp_path, {MON: True, TUE: True}), tmp_path)
    first = reader.window_digest("fx.eurusd", _ms(MON), _ms(WED))
    assert first == reader.window_digest("fx.eurusd", _ms(MON), _ms(WED))
    assert first.days == 2
    assert first.ticks == 96
    different = reader.window_digest("fx.eurusd", _ms(MON), _ms(WED) - 1)
    assert different.sha256 != first.sha256


def test_the_window_digest_changes_with_the_ticks(tmp_path: Path) -> None:
    a = TickReader(_stored(tmp_path / "a", {MON: True}), tmp_path / "a")
    store_b = InMemoryTickDayStore()
    fetch_day(
        FakeTickProvider(lambda day: day_ticks(day, 47)),
        store_b,
        tmp_path / "b",
        CLOCK,
        instrument_id="fx.eurusd",
        day=MON,
        point_size=POINT,
    )
    b = TickReader(store_b, tmp_path / "b")
    assert (
        a.window_digest("fx.eurusd", _ms(MON), _ms(TUE)).sha256
        != b.window_digest("fx.eurusd", _ms(MON), _ms(TUE)).sha256
    )
