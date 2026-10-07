"""Phase 10a acceptance: the tick pipeline's boundaries and an end-to-end run on fakes."""

from __future__ import annotations

import ast
from datetime import UTC, date, datetime, time
from decimal import Decimal
from pathlib import Path

import pytest

from tests.unit.marketdata.tick_fakes import FakeTickProvider, InMemoryTickDayStore, weekdays_only
from trading_house.core.clock import FixedClock
from trading_house.core.errors import EvidenceIntegrityError
from trading_house.marketdata.tick_files import day_path
from trading_house.marketdata.tick_ingest import backfill_ticks
from trading_house.marketdata.tick_reader import TickReader
from trading_house.marketdata.tick_store import coverage_of
from trading_house.marketdata.ticks import epoch_ms

SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src" / "trading_house"
TICK_FILES = SOURCE_ROOT / "marketdata" / "tick_files.py"
_FILE_CALLS = {"load", "save", "savez", "savez_compressed"}


def test_only_tick_files_opens_numpy_files() -> None:
    offenders = []
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        if path == TICK_FILES:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Attribute)
                and node.attr in _FILE_CALLS
                and isinstance(node.value, ast.Name)
                and node.value.id in {"np", "numpy"}
            ):
                offenders.append(f"{path.relative_to(SOURCE_ROOT)}:{node.lineno}")
    assert offenders == []


def test_the_guard_above_can_fail() -> None:
    tree = ast.parse("import numpy as np\nnp.load('x')\n")
    assert any(isinstance(n, ast.Attribute) and n.attr == "load" for n in ast.walk(tree))


def test_backfill_coverage_reader_and_digest_end_to_end(tmp_path: Path) -> None:
    clock = FixedClock(datetime(2026, 10, 7, 1, tzinfo=UTC))
    store = InMemoryTickDayStore()
    summary = backfill_ticks(
        FakeTickProvider(weekdays_only),
        store,
        tmp_path,
        clock,
        instrument_id="fx.eurusd",
        point_size=Decimal("0.00001"),
        until=date(2026, 10, 1),
    )
    assert summary.as_json()["days_complete"] == 4  # Thu 1, Fri 2, Mon 5, Tue 6
    coverage = coverage_of("fx.eurusd", store.rows("fx.eurusd"))
    assert coverage.weekday_gaps == ()
    reader = TickReader(store, tmp_path)
    start = epoch_ms(datetime.combine(date(2026, 10, 1), time(), UTC))
    end = epoch_ms(datetime.combine(date(2026, 10, 7), time(), UTC))
    assert len(reader.ticks("fx.eurusd", start, end, as_of_ms=end)) == 4 * 48
    digest = reader.window_digest("fx.eurusd", start, end)
    assert digest.days == 6 and digest.ticks == 4 * 48  # noqa: PT018
    day_path(tmp_path, "fx.eurusd", date(2026, 10, 2)).write_bytes(b"tampered")
    with pytest.raises(EvidenceIntegrityError):
        reader.window_digest("fx.eurusd", start, end)
