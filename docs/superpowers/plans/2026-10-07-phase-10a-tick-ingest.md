# Phase 10a — Tick Ingest and Storage: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Collect EURUSD and XAUUSD bid/ask ticks from the MT5 demo terminal into immutable, digest-checked daily files recorded in PostgreSQL, with a reader that refuses rather than guesses.

**Architecture:** Pure tick types and conversion (`marketdata/ticks.py`); one thin `copy_ticks_range` method in `terminal.py` returning columns (`Mt5TickBatch`), converted to UTC by `Mt5BrokerAdapter.ticks`; one compressed `.npz` per instrument per UTC day (`marketdata/tick_files.py`, the only module that opens them); an append-only `marketdata.tick_days` record (migration 0010, `marketdata/tick_store.py`); day-walking ingest (`marketdata/tick_ingest.py`); `TickReader` and a window digest (`marketdata/tick_reader.py`); CLI commands under `data ticks` and `research dataset tick-digest`.

**Tech Stack:** Python 3.12, numpy 2 (already a dependency), Pydantic v2 strict, PostgreSQL 18 via psycopg 3, Alembic, Typer, pytest, testcontainers.

**Spec:** `docs/superpowers/specs/2026-10-07-phase-10a-tick-ingest-design.md`

## Global Constraints

- Python 3.12; mypy strict; Ruff `["E","F","I","B","UP","SIM","RUF","S","PT"]`; line length 100. **`UV_SYSTEM_CERTS=1` prefixes every `uv` command.** `uv run mypy` takes no path arguments.
- **No new dependency.** numpy is already declared (`numpy>=2,<3`).
- **Stored prices are int64 points** (`price / point_size`), never floats. Floats exist only inside `to_tick_arrays`, where MT5's float64 quotes are snapped to the point grid.
- **`MetaTrader5` is imported only in `src/trading_house/brokers/mt5/terminal.py`**; its statement cap rises from 80 to 90 for the one tick method (spec D-6). `marketdata/` never imports MetaTrader5.
- **Only `src/trading_house/marketdata/tick_files.py` calls `np.load`, `np.savez` or `np.savez_compressed`** in `src/`.
- Tick day files live under `RuntimeSettings.tick_root` (`TRADING_HOUSE_TICK_ROOT`, default `.local/ticks`, git-ignored). A day file is never overwritten with different ticks.
- `marketdata.tick_days` is append-only: triggers refuse UPDATE, DELETE and TRUNCATE; the runtime role has SELECT and INSERT only. A `COMPLETE` or `EMPTY` row is unique per `(instrument_id, day)`; `FAILED` rows are not.
- **No raw broker message, credential, DSN or account number** in any error, `detail` column, log or printed payload. `detail` holds our own error class name and, for `TickDataError`, our own message.
- Demo account only. Nothing in this plan creates, modifies or installs a scheduled task (spec D-7).
- **Commit before mutating; every new test earns its place by mutation** (break the code, confirm the test fails, restore with `git checkout -- <file>`).
- **Commits are made only with the user's authorization for this execution**; otherwise leave the file state and report the uncommitted diff.

## Plan deviations from the spec (amended in Task 8)

1. **Spec §7 says one MT5 request per UTC day.** The gateway times each request out at 10 s (`Mt5Gateway(request_timeout_seconds=10.0)`), and a day is 200k–400k ticks that the terminal may first have to download. This plan fetches each day as **24 hourly requests**, and the tick commands construct the gateway with `request_timeout_seconds=120.0`.
2. **Spec §9 names `research dataset digest --ticks`.** That command requires `--timeframe`; this plan adds a separate `research dataset tick-digest` instead of overloading it.

## Baseline

Before Task 1, record: `UV_SYSTEM_CERTS=1 uv run pytest tests/unit tests/acceptance -q --no-cov -p no:cacheprovider` (2026-10-04: 2494 passed, 3 skipped, plus later additions). Docker must be running for Task 4's integration tests.

## File Structure

| File | Responsibility |
|---|---|
| `src/trading_house/marketdata/ticks.py` | **New.** `RawTicks`, `TickArrays`, `TickDataError`, `epoch_ms`, `to_tick_arrays`, `day_digest`. Pure. |
| `src/trading_house/brokers/mt5/boundary.py` | Gains `Mt5TickBatch` and `TerminalPort.copy_ticks_range`. |
| `src/trading_house/brokers/mt5/terminal.py` | Gains `copy_ticks_range` (IPC only). |
| `src/trading_house/brokers/mt5/adapter.py` | Gains `ticks()`: server frame → UTC, end-exclusive. |
| `src/trading_house/marketdata/tick_files.py` | **New.** Day path, atomic write, verified read. The only module that opens tick files. |
| `migrations/versions/0010_tick_days.py` | **New.** `marketdata.tick_days`, its triggers and grants. |
| `src/trading_house/marketdata/tick_store.py` | **New.** `TickDayOutcome`, `TickDay`, `TickDayStore`, `PostgresTickDayStore`, `TickCoverage`, `coverage_of`, `settled_days`. |
| `src/trading_house/settings.py` | Gains `tick_root`. |
| `src/trading_house/marketdata/tick_ingest.py` | **New.** `TickProvider`, `fetch_day`, `backfill_ticks`, `update_ticks`, `TickRunSummary`. |
| `src/trading_house/marketdata/tick_reader.py` | **New.** `TickReader`, `TickWindowDigest`. |
| `src/trading_house/cli.py` | `data ticks backfill/update/coverage`, `research dataset tick-digest`; `_history_provider` takes a timeout. |
| `scripts/collect_ticks.ps1` | **New.** Runs `data ticks update` for both instruments. |
| `tests/unit/marketdata/tick_fakes.py` | **New.** `FakeTickProvider`, `InMemoryTickDayStore`, `day_ticks`, `raw_ticks`. |
| `tests/acceptance/test_phase10a.py` | **New.** Boundaries and an end-to-end run on fakes. |
| `tests/live/test_mt5_ticks.py` | **New.** One real hour of each instrument. |

---

### Task 1: The pure tick model

**Files:**
- Create: `src/trading_house/marketdata/ticks.py`
- Test: `tests/unit/marketdata/test_ticks.py`

**Interfaces:**
- Consumes: `trading_house.core.clock.ensure_utc`.
- Produces:
  - `TickDataError(ValueError)`
  - `epoch_ms(instant: datetime) -> int` (UTC ms; naive input raises `TimestampError`)
  - `RawTicks` (frozen dataclass, `eq=False`): `time_ms` int64 UTC ms, `bid`, `ask`, `last` float64, `volume` int64, `volume_real` float64, `flags` int64; `__len__`; `within(start_ms, end_ms) -> RawTicks`; `RawTicks.empty()`; `RawTicks.concatenate(parts)`.
  - `TickArrays` (frozen dataclass, `eq=False`): `time_ms` int64, `bid` int64, `ask` int64, `flags` uint16; `__len__`; `crossed_quotes() -> int`; `select(mask) -> TickArrays`; `TickArrays.empty()`; `TickArrays.concatenate(parts)`.
  - `to_tick_arrays(raw: RawTicks, point_size: Decimal) -> TickArrays`
  - `day_digest(instrument_id: str, day: date, point_size: Decimal, arrays: TickArrays) -> str` (64 lowercase hex)

- [ ] **Step 1: Write the failing tests**

```python
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import numpy as np
import pytest

from trading_house.core.errors import TimestampError
from trading_house.marketdata.ticks import (
    RawTicks,
    TickArrays,
    TickDataError,
    day_digest,
    epoch_ms,
    to_tick_arrays,
)

EURUSD_POINT = Decimal("0.00001")
GOLD_POINT = Decimal("0.01")
DAY = date(2026, 10, 6)


def _raw(
    times: list[int],
    bid: list[float],
    ask: list[float],
    *,
    last: float = 0.0,
    volume: int = 0,
    volume_real: float = 0.0,
    flags: int = 6,
) -> RawTicks:
    n = len(times)
    return RawTicks(
        time_ms=np.array(times, dtype=np.int64),
        bid=np.array(bid, dtype=np.float64),
        ask=np.array(ask, dtype=np.float64),
        last=np.full(n, last, dtype=np.float64),
        volume=np.full(n, volume, dtype=np.int64),
        volume_real=np.full(n, volume_real, dtype=np.float64),
        flags=np.full(n, flags, dtype=np.int64),
    )


def test_epoch_ms_is_integer_utc_milliseconds() -> None:
    assert epoch_ms(datetime(1970, 1, 1, 0, 0, 1, 500_000, tzinfo=UTC)) == 1500


def test_epoch_ms_refuses_a_naive_datetime() -> None:
    with pytest.raises(TimestampError):
        epoch_ms(datetime(2026, 10, 6))  # noqa: DTZ001


def test_eurusd_floats_snap_exactly_to_points() -> None:
    """1.12427 / 0.00001 is 112426.99999999999 in binary; it is still 112427 points."""

    arrays = to_tick_arrays(_raw([1, 2], [1.12427, 1.12430], [1.12437, 1.12440]), EURUSD_POINT)
    assert arrays.bid.tolist() == [112427, 112430]
    assert arrays.ask.tolist() == [112437, 112440]
    assert arrays.bid.dtype == np.int64


def test_gold_floats_snap_exactly_to_points() -> None:
    arrays = to_tick_arrays(_raw([1], [2650.12], [2650.45]), GOLD_POINT)
    assert arrays.bid.tolist() == [265012]
    assert arrays.ask.tolist() == [265045]


def test_a_price_off_the_point_grid_is_refused() -> None:
    with pytest.raises(TickDataError, match="point grid"):
        to_tick_arrays(_raw([1], [1.124275], [1.12437]), EURUSD_POINT)


@pytest.mark.parametrize(
    "kwargs", [{"last": 1.1}, {"volume": 3}, {"volume_real": 0.5}], ids=["last", "volume", "real"]
)
def test_a_trade_print_is_refused(kwargs: dict[str, float]) -> None:
    with pytest.raises(TickDataError, match="trade print"):
        to_tick_arrays(_raw([1], [1.1], [1.1001], **kwargs), EURUSD_POINT)  # type: ignore[arg-type]


def test_time_going_backwards_is_refused() -> None:
    with pytest.raises(TickDataError, match="backwards"):
        to_tick_arrays(_raw([5, 4], [1.1, 1.1], [1.1001, 1.1001]), EURUSD_POINT)


def test_ticks_sharing_a_millisecond_keep_the_broker_order() -> None:
    arrays = to_tick_arrays(_raw([5, 5], [1.10000, 1.10003], [1.1001, 1.1001]), EURUSD_POINT)
    assert arrays.bid.tolist() == [110000, 110003]


def test_a_non_positive_price_is_refused() -> None:
    with pytest.raises(TickDataError, match="not positive"):
        to_tick_arrays(_raw([1], [0.0], [1.1]), EURUSD_POINT)


def test_crossed_quotes_are_counted_not_dropped() -> None:
    arrays = to_tick_arrays(_raw([1, 2], [1.10010, 1.1], [1.10000, 1.1001]), EURUSD_POINT)
    assert len(arrays) == 2
    assert arrays.crossed_quotes() == 1


def test_an_empty_batch_is_an_empty_array_set() -> None:
    arrays = to_tick_arrays(RawTicks.empty(), EURUSD_POINT)
    assert len(arrays) == 0
    assert arrays.flags.dtype == np.uint16


def test_within_is_half_open() -> None:
    raw = _raw([10, 20, 30], [1.1] * 3, [1.1001] * 3)
    assert raw.within(10, 30).time_ms.tolist() == [10, 20]


def _arrays() -> TickArrays:
    return to_tick_arrays(_raw([1, 2], [1.1, 1.10001], [1.1001, 1.10011]), EURUSD_POINT)


def test_the_day_digest_is_stable_hex() -> None:
    first = day_digest("fx.eurusd", DAY, EURUSD_POINT, _arrays())
    assert first == day_digest("fx.eurusd", DAY, EURUSD_POINT, _arrays())
    assert len(first) == 64
    assert int(first, 16) >= 0


def test_the_day_digest_names_instrument_day_and_point_size() -> None:
    base = day_digest("fx.eurusd", DAY, EURUSD_POINT, _arrays())
    assert day_digest("metal.xauusd", DAY, EURUSD_POINT, _arrays()) != base
    assert day_digest("fx.eurusd", DAY + timedelta(days=1), EURUSD_POINT, _arrays()) != base
    assert day_digest("fx.eurusd", DAY, Decimal("0.0001"), _arrays()) != base


def test_the_day_digest_covers_every_array() -> None:
    base = _arrays()
    for field in ("time_ms", "bid", "ask", "flags"):
        changed = {name: getattr(base, name).copy() for name in ("time_ms", "bid", "ask", "flags")}
        changed[field][0] += 1
        assert day_digest("fx.eurusd", DAY, EURUSD_POINT, TickArrays(**changed)) != day_digest(
            "fx.eurusd", DAY, EURUSD_POINT, base
        ), field


def test_the_point_size_spelling_does_not_change_the_digest() -> None:
    assert day_digest("fx.eurusd", DAY, Decimal("0.000010"), _arrays()) == day_digest(
        "fx.eurusd", DAY, Decimal("1E-5"), _arrays()
    )
```

- [ ] **Step 2: Run them to verify they fail**

Run: `UV_SYSTEM_CERTS=1 uv run pytest tests/unit/marketdata/test_ticks.py -q --no-cov -p no:cacheprovider`
Expected: FAIL — `ModuleNotFoundError: trading_house.marketdata.ticks`.

- [ ] **Step 3: Write the module**

```python
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
_RAW_FIELDS: Final = ("time_ms", "bid", "ask", "last", "volume", "volume_real", "flags")
_STORED_FIELDS: Final = ("time_ms", "bid", "ask", "flags")


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
```

- [ ] **Step 4: Run the tests, ruff and mypy**

Run: `UV_SYSTEM_CERTS=1 uv run pytest tests/unit/marketdata/test_ticks.py -q --no-cov -p no:cacheprovider`
Then: `UV_SYSTEM_CERTS=1 uv run ruff check . && UV_SYSTEM_CERTS=1 uv run ruff format --check . && UV_SYSTEM_CERTS=1 uv run mypy`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/trading_house/marketdata/ticks.py tests/unit/marketdata/test_ticks.py
git commit -m "feat: phase 10a pure tick model, point conversion and day digest"
```

- [ ] **Step 6: Mutation proof** — one at a time, restoring after each; confirm what fails and report any divergence:
  - `POINT_TOLERANCE` → `0.0`. Expected: the EURUSD snap test fails.
  - Delete the trade-print check. Expected: the three trade-print cases fail.
  - `np.diff(raw.time_ms) < 0` → `<= 0`. Expected: the same-millisecond test fails.
  - Remove `(arrays.flags, "<u2")` from the digest loop. Expected: `test_the_day_digest_covers_every_array` fails for `flags`.

---

### Task 2: The broker boundary for ticks

**Files:**
- Modify: `src/trading_house/brokers/mt5/boundary.py` (add `Mt5TickBatch`; `TerminalPort.copy_ticks_range`)
- Modify: `src/trading_house/brokers/mt5/terminal.py` (add `copy_ticks_range`)
- Modify: `src/trading_house/brokers/mt5/adapter.py` (add `ticks`)
- Modify: `tests/acceptance/test_architecture.py:26` (`TERMINAL_STATEMENT_CAP = 90`)
- Modify: `tests/unit/brokers/mt5/conftest.py` (`FakeTerminal.copy_ticks_range`)
- Test: `tests/unit/brokers/mt5/test_ticks_boundary.py` (new)

**Interfaces:**
- Consumes: Task 1's `RawTicks`, `epoch_ms`.
- Produces:
  - `Mt5TickBatch` (frozen, `eq=False`): `time_msc` int64 (**server frame**), `bid`, `ask`, `last` float64, `volume` int64, `volume_real` float64, `flags` int64; `Mt5TickBatch.from_structured(raw: npt.NDArray[Any]) -> Mt5TickBatch`.
  - `TerminalPort.copy_ticks_range(server_symbol: str, start: datetime, end: datetime) -> Mt5TickBatch | None` (start/end are UTC; the real terminal shifts them to the server frame).
  - `Mt5BrokerAdapter.ticks(instrument_id: InstrumentId, start: datetime, end: datetime) -> RawTicks` — UTC ms, only ticks in `[start, end)`, `BrokerUnavailableError` when the terminal returns `None`.

- [ ] **Step 1: Write the failing tests** (`tests/unit/brokers/mt5/test_ticks_boundary.py`)

```python
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from tests.unit.brokers.mt5.conftest import FakeTerminal
from tests.unit.brokers.mt5.test_adapter import _adapter
from trading_house.brokers.mt5.boundary import Mt5TickBatch
from trading_house.core.errors import BrokerUnavailableError
from trading_house.marketdata.ticks import epoch_ms

START = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
END = START + timedelta(hours=1)
OFFSET_SECONDS = 3 * 3600


def _batch(server_ms: list[int]) -> Mt5TickBatch:
    n = len(server_ms)
    return Mt5TickBatch(
        time_msc=np.array(server_ms, dtype=np.int64),
        bid=np.full(n, 1.10000),
        ask=np.full(n, 1.10010),
        last=np.zeros(n),
        volume=np.zeros(n, dtype=np.int64),
        volume_real=np.zeros(n),
        flags=np.full(n, 6, dtype=np.int64),
    )


def test_from_structured_reads_mt5_columns_by_name() -> None:
    dtype = [
        ("time", "<i8"), ("bid", "<f8"), ("ask", "<f8"), ("last", "<f8"),
        ("volume", "<u8"), ("time_msc", "<i8"), ("flags", "<u4"), ("volume_real", "<f8"),
    ]
    raw = np.array([(1, 1.1, 1.2, 0.0, 0, 1000, 6, 0.0)], dtype=dtype)
    batch = Mt5TickBatch.from_structured(raw)
    assert batch.time_msc.tolist() == [1000]
    assert batch.bid.tolist() == [1.1]
    assert batch.flags.dtype == np.int64


class _ServerFrameTerminal(FakeTerminal):
    def __init__(self, batch: Mt5TickBatch | None) -> None:
        super().__init__()
        self.batch = batch
        self.tick_requests: list[tuple[str, datetime, datetime]] = []

    def server_utc_offset_seconds(self) -> int:
        return OFFSET_SECONDS

    def copy_ticks_range(
        self, server_symbol: str, start: datetime, end: datetime
    ) -> Mt5TickBatch | None:
        self.tick_requests.append((server_symbol, start, end))
        return self.batch


def test_ticks_are_moved_from_the_server_frame_to_utc_and_end_is_excluded() -> None:
    """MT5 answers in its own clock and end-inclusively; the adapter answers in UTC,
    half-open, as ingest windows are planned."""

    start_ms, end_ms, shift = epoch_ms(START), epoch_ms(END), OFFSET_SECONDS * 1000
    terminal = _ServerFrameTerminal(_batch([start_ms + shift, start_ms + 1500 + shift, end_ms + shift]))
    adapter, gateway = _adapter(terminal)
    try:
        raw = adapter.ticks("fx.eurusd", START, END)
    finally:
        gateway.stop()
    assert raw.time_ms.tolist() == [start_ms, start_ms + 1500]
    assert [request[1:] for request in terminal.tick_requests] == [(START, END)]


def test_a_failed_terminal_call_is_unavailable_not_empty() -> None:
    """Unlike bar history, an empty tick hour is a fact ingest records as EMPTY;
    a failed call must not be mistaken for one."""

    adapter, gateway = _adapter(_ServerFrameTerminal(None))
    try:
        with pytest.raises(BrokerUnavailableError):
            adapter.ticks("fx.eurusd", START, END)
    finally:
        gateway.stop()
```

If `_adapter` in `tests/unit/brokers/mt5/test_adapter.py` is not importable as written (check its signature first), use whatever that file uses to build an adapter and a started gateway from a `FakeTerminal`, and report what you used.

- [ ] **Step 2: Run them to verify they fail**

Run: `UV_SYSTEM_CERTS=1 uv run pytest tests/unit/brokers/mt5/test_ticks_boundary.py -q --no-cov -p no:cacheprovider`
Expected: FAIL — `ImportError: cannot import name 'Mt5TickBatch'`.

- [ ] **Step 3: Add `Mt5TickBatch` and the port method to `boundary.py`**

Add `import numpy as np`, `import numpy.typing as npt` and `from typing import Any` (if absent) to the imports, and beside the other boundary dataclasses:

```python
@dataclass(frozen=True, slots=True, eq=False)
class Mt5TickBatch:
    """``copy_ticks_range`` as columns. ``time_msc`` is still in the broker's clock;
    ``Mt5BrokerAdapter.ticks`` moves it to UTC."""

    time_msc: npt.NDArray[np.int64]
    bid: npt.NDArray[np.float64]
    ask: npt.NDArray[np.float64]
    last: npt.NDArray[np.float64]
    volume: npt.NDArray[np.int64]
    volume_real: npt.NDArray[np.float64]
    flags: npt.NDArray[np.int64]

    @classmethod
    def from_structured(cls, raw: npt.NDArray[Any]) -> Mt5TickBatch:
        return cls(
            time_msc=np.asarray(raw["time_msc"], dtype=np.int64),
            bid=np.asarray(raw["bid"], dtype=np.float64),
            ask=np.asarray(raw["ask"], dtype=np.float64),
            last=np.asarray(raw["last"], dtype=np.float64),
            volume=np.asarray(raw["volume"], dtype=np.int64),
            volume_real=np.asarray(raw["volume_real"], dtype=np.float64),
            flags=np.asarray(raw["flags"], dtype=np.int64),
        )
```

In `class TerminalPort(Protocol)`, after `copy_rates_range`:

```python
    def copy_ticks_range(
        self, server_symbol: str, start: datetime, end: datetime
    ) -> Mt5TickBatch | None: ...
```

- [ ] **Step 4: Add the terminal method** (`terminal.py`, after `copy_rates_range`; import `Mt5TickBatch` from boundary)

```python
    def copy_ticks_range(
        self, server_symbol: str, start: datetime, end: datetime
    ) -> Mt5TickBatch | None:
        mt5.symbol_select(server_symbol, True)
        raw = mt5.copy_ticks_range(
            server_symbol,
            utc_to_server_time(start, self._offset),
            utc_to_server_time(end, self._offset),
            mt5.COPY_TICKS_ALL,
        )
        if raw is None:
            return None
        return Mt5TickBatch.from_structured(raw)
```

And in `tests/acceptance/test_architecture.py`:

```python
TERMINAL_STATEMENT_CAP = 90
"""Raised from 80 once, for Phase 10a's ``copy_ticks_range`` (spec D-6): MetaTrader5
may be imported in one module only, so the tick call has to live here. Conversion
stays in ``boundary.py`` and the adapter."""
```

- [ ] **Step 5: Add the adapter method** (`adapter.py`, after `history`; import `RawTicks, epoch_ms` from `trading_house.marketdata.ticks`)

```python
    def ticks(self, instrument_id: InstrumentId, start: datetime, end: datetime) -> RawTicks:
        """Ticks in ``[start, end)``, UTC, at the lowest priority band.

        ``None`` from the terminal is a failed call, raised as unavailable: an hour
        with no ticks is an answer ingest records, and a failure must not pass for one.
        """

        server_symbol = self._server_symbol_for(instrument_id)
        batch = self._gateway.call(
            Priority.MARKET_DATA, lambda t: t.copy_ticks_range(server_symbol, start, end)
        )
        if batch is None:
            raise BrokerUnavailableError()
        shift_ms = self._gateway.server_utc_offset_seconds * 1000
        raw = RawTicks(
            time_ms=batch.time_msc - shift_ms,
            bid=batch.bid,
            ask=batch.ask,
            last=batch.last,
            volume=batch.volume,
            volume_real=batch.volume_real,
            flags=batch.flags,
        )
        # MetaTrader 5's range is end-inclusive; ingest windows are [start, end).
        return raw.within(epoch_ms(start), epoch_ms(end))
```

Give `FakeTerminal` in `tests/unit/brokers/mt5/conftest.py` a default:

```python
    def copy_ticks_range(
        self, server_symbol: str, start: datetime, end: datetime
    ) -> Mt5TickBatch | None:
        return None
```

- [ ] **Step 6: Run the tests, the architecture guard, the broker unit tests and mypy**

Run: `UV_SYSTEM_CERTS=1 uv run pytest tests/unit/brokers tests/acceptance/test_architecture.py tests/acceptance/test_phase1.py -q --no-cov -p no:cacheprovider`
Then: `UV_SYSTEM_CERTS=1 uv run ruff check . && UV_SYSTEM_CERTS=1 uv run ruff format --check . && UV_SYSTEM_CERTS=1 uv run mypy`
Expected: all pass; `test_the_terminal_module_stays_thin` passes with the new cap (report the new statement count).

- [ ] **Step 7: Commit**

```bash
git add src/trading_house/brokers/mt5/boundary.py src/trading_house/brokers/mt5/terminal.py src/trading_house/brokers/mt5/adapter.py tests/acceptance/test_architecture.py tests/unit/brokers/mt5/conftest.py tests/unit/brokers/mt5/test_ticks_boundary.py
git commit -m "feat: phase 10a fetch ticks through the gateway in UTC"
```

- [ ] **Step 8: Mutation proof** — one at a time, restoring after each:
  - Adapter: `batch.time_msc - shift_ms` → `batch.time_msc`. Expected: the server-frame test fails.
  - Adapter: drop the `.within(...)` call. Expected: the server-frame test fails (the end tick survives).
  - Adapter: replace the `None` branch with `return RawTicks.empty()`. Expected: the unavailable test fails.

---

### Task 3: Day files

**Files:**
- Create: `src/trading_house/marketdata/tick_files.py`
- Test: `tests/unit/marketdata/test_tick_files.py`

**Interfaces:**
- Consumes: `TickArrays`, `day_digest` (Task 1); `EvidenceIntegrityError` (`core/errors.py`).
- Produces:
  - `day_path(root: Path, instrument_id: str, day: date) -> Path` → `root/instrument_id/YYYY/YYYY-MM-DD.npz`
  - `write_day_file(root: Path, instrument_id: str, day: date, point_size: Decimal, arrays: TickArrays) -> str` — returns the day digest; idempotent for identical ticks; `EvidenceIntegrityError` if the file holds different ticks.
  - `read_day_file(path: Path) -> TickArrays` — `EvidenceIntegrityError` if missing, unreadable, or not exactly the four arrays.

- [ ] **Step 1: Write the failing tests**

```python
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
        np.array_equal(getattr(a, name), getattr(b, name)) and getattr(a, name).dtype == getattr(b, name).dtype
        for name in ("time_ms", "bid", "ask", "flags")
    )


def test_the_path_is_instrument_year_day(tmp_path: Path) -> None:
    assert day_path(tmp_path, "fx.eurusd", DAY) == tmp_path / "fx.eurusd" / "2026" / "2026-10-06.npz"


def test_a_written_day_reads_back_identically_and_returns_its_digest(tmp_path: Path) -> None:
    digest = write_day_file(tmp_path, "fx.eurusd", DAY, POINT, _arrays())
    stored = read_day_file(day_path(tmp_path, "fx.eurusd", DAY))
    assert _same(stored, _arrays())
    assert digest == day_digest("fx.eurusd", DAY, POINT, _arrays())


def test_rewriting_the_same_ticks_is_a_recognised_retry(tmp_path: Path) -> None:
    first = write_day_file(tmp_path, "fx.eurusd", DAY, POINT, _arrays())
    assert write_day_file(tmp_path, "fx.eurusd", DAY, POINT, _arrays()) == first


def test_a_day_file_is_never_overwritten_with_different_ticks(tmp_path: Path) -> None:
    write_day_file(tmp_path, "fx.eurusd", DAY, POINT, _arrays())
    with pytest.raises(EvidenceIntegrityError):
        write_day_file(tmp_path, "fx.eurusd", DAY, POINT, _arrays(first_bid=109999))
    assert _same(read_day_file(day_path(tmp_path, "fx.eurusd", DAY)), _arrays())


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
            handle, time_ms=arrays.time_ms, bid=arrays.bid, ask=arrays.ask, flags=arrays.flags,
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


def test_a_stale_partial_from_a_crash_does_not_block_the_write(tmp_path: Path) -> None:
    path = day_path(tmp_path, "fx.eurusd", DAY)
    path.parent.mkdir(parents=True)
    path.with_name(f"{path.stem}.partial").write_bytes(b"half")
    write_day_file(tmp_path, "fx.eurusd", DAY, POINT, _arrays())
    assert _same(read_day_file(path), _arrays())
```

- [ ] **Step 2: Run them to verify they fail**

Run: `UV_SYSTEM_CERTS=1 uv run pytest tests/unit/marketdata/test_tick_files.py -q --no-cov -p no:cacheprovider`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Write the module**

```python
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
```

- [ ] **Step 4: Run the tests, ruff and mypy** (Step 2's command, then the ruff/mypy line from Task 1 Step 4). Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/trading_house/marketdata/tick_files.py tests/unit/marketdata/test_tick_files.py
git commit -m "feat: phase 10a immutable tick day files"
```

- [ ] **Step 6: Mutation proof** — one at a time, restoring after each:
  - Replace the digest comparison in `write_day_file` with `return digest` (accept any existing file). Expected: `test_a_day_file_is_never_overwritten_with_different_ticks` fails.
  - Remove `zipfile.BadZipFile` from the `except`. Expected: `test_a_corrupt_file_is_refused` fails (confirm which exception escapes and report it).
  - Remove `partial.unlink(missing_ok=True)`. Expected: the stale-partial test fails.

---

### Task 4: The record — migration 0010, the store, the setting

**Files:**
- Create: `migrations/versions/0010_tick_days.py`
- Create: `src/trading_house/marketdata/tick_store.py`
- Modify: `src/trading_house/settings.py` (add `tick_root`)
- Test: `tests/unit/marketdata/test_tick_store.py`, `tests/integration/marketdata/test_tick_days.py`

**Interfaces:**
- Consumes: `ConnectionFactory` from `trading_house.marketdata.store`; `CanonicalModel`, `InstrumentId`, `PositiveDecimal` from `trading_house.core.values`; `ensure_utc`; `DatabaseUnavailableError`, `EvidenceIntegrityError`.
- Produces:
  - `TickDayOutcome(str, Enum)`: `COMPLETE`, `EMPTY`, `FAILED` (values equal names)
  - `TickDay(CanonicalModel)`: `instrument_id`, `day: date`, `outcome`, `tick_count`, `first_time_ms: int | None`, `last_time_ms: int | None`, `crossed_quotes`, `point_size: Decimal`, `file_sha256: str | None`, `detail: str | None`, `fetched_at: datetime`
  - `TickDayStore(Protocol)`: `record(day: TickDay) -> None`; `rows(instrument_id: str) -> tuple[TickDay, ...]` (ordered by `day`, then `fetched_at`)
  - `PostgresTickDayStore(connection_factory)`; recording a second settled row raises `EvidenceIntegrityError`
  - `settled_days(rows: Sequence[TickDay]) -> dict[date, TickDay]` (non-`FAILED` rows by day)
  - `TickCoverage` (frozen dataclass) with `as_json() -> dict[str, JsonValue]`; `coverage_of(instrument_id: str, rows: Sequence[TickDay]) -> TickCoverage`
  - `RuntimeSettings.tick_root: Path = Path(".local/ticks")`

- [ ] **Step 1: Write the failing unit tests** (`tests/unit/marketdata/test_tick_store.py`)

```python
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from trading_house.marketdata.tick_store import TickDay, TickDayOutcome, coverage_of, settled_days

FETCHED = datetime(2026, 10, 7, 1, 0, tzinfo=UTC)
POINT = Decimal("0.00001")
SHA = "a" * 64


def _complete(day: date, ticks: int = 10) -> TickDay:
    return TickDay(
        instrument_id="fx.eurusd", day=day, outcome=TickDayOutcome.COMPLETE, tick_count=ticks,
        first_time_ms=1, last_time_ms=2, crossed_quotes=0, point_size=POINT, file_sha256=SHA,
        fetched_at=FETCHED,
    )


def _empty(day: date) -> TickDay:
    return TickDay(
        instrument_id="fx.eurusd", day=day, outcome=TickDayOutcome.EMPTY, tick_count=0,
        point_size=POINT, fetched_at=FETCHED,
    )


def _failed(day: date) -> TickDay:
    return TickDay(
        instrument_id="fx.eurusd", day=day, outcome=TickDayOutcome.FAILED, tick_count=0,
        point_size=POINT, detail="BrokerUnavailableError", fetched_at=FETCHED,
    )


def test_a_complete_day_needs_a_digest() -> None:
    with pytest.raises(ValidationError):
        TickDay(
            instrument_id="fx.eurusd", day=date(2026, 10, 6), outcome=TickDayOutcome.COMPLETE,
            tick_count=10, first_time_ms=1, last_time_ms=2, point_size=POINT, fetched_at=FETCHED,
        )


def test_an_empty_day_has_no_ticks() -> None:
    with pytest.raises(ValidationError):
        TickDay(
            instrument_id="fx.eurusd", day=date(2026, 10, 6), outcome=TickDayOutcome.EMPTY,
            tick_count=3, point_size=POINT, fetched_at=FETCHED,
        )


def test_a_failed_day_needs_a_detail() -> None:
    with pytest.raises(ValidationError):
        TickDay(
            instrument_id="fx.eurusd", day=date(2026, 10, 6), outcome=TickDayOutcome.FAILED,
            tick_count=0, point_size=POINT, fetched_at=FETCHED,
        )


def test_settled_days_ignore_failures() -> None:
    rows = (_failed(date(2026, 10, 5)), _complete(date(2026, 10, 6)), _failed(date(2026, 10, 6)))
    settled = settled_days(rows)
    assert set(settled) == {date(2026, 10, 6)}
    assert settled[date(2026, 10, 6)].outcome is TickDayOutcome.COMPLETE


def test_coverage_counts_and_finds_weekday_gaps() -> None:
    """Mon 5th and Thu 8th complete, Tue 6th a holiday (EMPTY), Wed 7th failed only:
    the 7th is a weekday gap; the holiday is not."""

    rows = (
        _complete(date(2026, 10, 5), ticks=7),
        _empty(date(2026, 10, 6)),
        _failed(date(2026, 10, 7)),
        _complete(date(2026, 10, 8), ticks=5),
    )
    coverage = coverage_of("fx.eurusd", rows)
    assert coverage.complete_days == 2
    assert coverage.empty_days == 1
    assert coverage.unsettled_failed_days == 1
    assert coverage.ticks == 12
    assert coverage.earliest_complete == date(2026, 10, 5)
    assert coverage.latest_complete == date(2026, 10, 8)
    assert coverage.weekday_gaps == (date(2026, 10, 7),)
```

- [ ] **Step 2: Run them to verify they fail** — `UV_SYSTEM_CERTS=1 uv run pytest tests/unit/marketdata/test_tick_store.py -q --no-cov -p no:cacheprovider`. Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Write `tick_store.py`**

```python
"""The record of every tick day: what was fetched, what came back, which file holds it.

Append-only (migration 0010). A day is *settled* by one ``COMPLETE`` or ``EMPTY``
row; ``FAILED`` rows are history, and a later attempt may settle the day.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Any, Never, Protocol, Self

import psycopg
from pydantic import JsonValue, NonNegativeInt, field_validator, model_validator

from trading_house.core.clock import ensure_utc
from trading_house.core.errors import (
    DatabaseUnavailableError,
    EvidenceIntegrityError,
    TimestampError,
)
from trading_house.core.values import CanonicalModel, InstrumentId, PositiveDecimal
from trading_house.marketdata.store import ConnectionFactory

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class TickDayOutcome(str, Enum):  # noqa: UP042
    COMPLETE = "COMPLETE"
    EMPTY = "EMPTY"
    FAILED = "FAILED"


class TickDay(CanonicalModel):
    instrument_id: InstrumentId
    day: date
    outcome: TickDayOutcome
    tick_count: NonNegativeInt
    first_time_ms: int | None = None
    last_time_ms: int | None = None
    crossed_quotes: NonNegativeInt = 0
    point_size: PositiveDecimal
    file_sha256: str | None = None
    detail: str | None = None
    fetched_at: datetime

    @field_validator("fetched_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        try:
            return ensure_utc(value)
        except TimestampError as error:
            raise ValueError(str(error)) from error

    @model_validator(mode="after")
    def shape_matches_outcome(self) -> Self:
        times = (self.first_time_ms, self.last_time_ms)
        if self.outcome is TickDayOutcome.COMPLETE:
            if (
                self.tick_count == 0
                or self.file_sha256 is None
                or not _SHA256.match(self.file_sha256)
                or None in times
                or self.detail is not None
            ):
                raise ValueError("a complete day has ticks, both times, a digest and no detail")
            assert self.first_time_ms is not None and self.last_time_ms is not None
            if self.first_time_ms > self.last_time_ms:
                raise ValueError("a complete day's first tick precedes its last")
        elif self.outcome is TickDayOutcome.EMPTY:
            if (
                self.tick_count
                or self.crossed_quotes
                or self.file_sha256 is not None
                or times != (None, None)
                or self.detail is not None
            ):
                raise ValueError("an empty day has no ticks, times, digest or detail")
        elif not self.detail or self.file_sha256 is not None:
            raise ValueError("a failed day has a detail and no digest")
        return self


class TickDayStore(Protocol):
    def record(self, day: TickDay) -> None: ...

    def rows(self, instrument_id: str) -> tuple[TickDay, ...]: ...


def settled_days(rows: Sequence[TickDay]) -> dict[date, TickDay]:
    return {row.day: row for row in rows if row.outcome is not TickDayOutcome.FAILED}


@dataclass(frozen=True, slots=True)
class TickCoverage:
    instrument_id: str
    complete_days: int
    empty_days: int
    unsettled_failed_days: int
    ticks: int
    earliest_complete: date | None
    latest_complete: date | None
    weekday_gaps: tuple[date, ...]

    def as_json(self) -> dict[str, JsonValue]:
        return {
            "complete_days": self.complete_days,
            "empty_days": self.empty_days,
            "unsettled_failed_days": self.unsettled_failed_days,
            "ticks": self.ticks,
            "earliest_complete": None if self.earliest_complete is None else self.earliest_complete.isoformat(),
            "latest_complete": None if self.latest_complete is None else self.latest_complete.isoformat(),
            "weekday_gaps": [gap.isoformat() for gap in self.weekday_gaps],
        }


def coverage_of(instrument_id: str, rows: Sequence[TickDay]) -> TickCoverage:
    settled = settled_days(rows)
    complete = sorted(day for day, row in settled.items() if row.outcome is TickDayOutcome.COMPLETE)
    failed_days = {row.day for row in rows if row.outcome is TickDayOutcome.FAILED}
    gaps: list[date] = []
    if complete:
        day = complete[0]
        while day <= complete[-1]:
            if day.weekday() < 5 and day not in settled:
                gaps.append(day)
            day += timedelta(days=1)
    return TickCoverage(
        instrument_id=instrument_id,
        complete_days=len(complete),
        empty_days=sum(1 for row in settled.values() if row.outcome is TickDayOutcome.EMPTY),
        unsettled_failed_days=len(failed_days - set(settled)),
        ticks=sum(row.tick_count for row in settled.values()),
        earliest_complete=complete[0] if complete else None,
        latest_complete=complete[-1] if complete else None,
        weekday_gaps=tuple(gaps),
    )


class _TickStoreFailure(Exception):
    """Credential- and row-free diagnostic cause."""


def _unavailable() -> Never:
    raise DatabaseUnavailableError() from _TickStoreFailure("tick-day store operation failed")


_COLUMNS = (
    "instrument_id, day, outcome, tick_count, first_time_ms, last_time_ms, crossed_quotes, "
    "point_size, file_sha256, detail, fetched_at"
)
_INSERT = f"INSERT INTO marketdata.tick_days ({_COLUMNS}) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"  # noqa: S608
_SELECT = f"SELECT {_COLUMNS} FROM marketdata.tick_days WHERE instrument_id = %s ORDER BY day, fetched_at"  # noqa: S608


class PostgresTickDayStore:
    def __init__(self, connection_factory: ConnectionFactory) -> None:
        self._connection_factory = connection_factory

    def _connect(self) -> psycopg.Connection[tuple[Any, ...]]:
        try:
            return self._connection_factory()
        except DatabaseUnavailableError:
            raise
        except Exception:
            _unavailable()

    def record(self, day: TickDay) -> None:
        connection = self._connect()
        try:
            with connection, connection.cursor() as cursor:
                cursor.execute(
                    _INSERT,
                    (
                        day.instrument_id, day.day, day.outcome.value, day.tick_count,
                        day.first_time_ms, day.last_time_ms, day.crossed_quotes, day.point_size,
                        day.file_sha256, day.detail, day.fetched_at,
                    ),
                )
        except psycopg.errors.UniqueViolation as error:
            # A settled day recorded twice: a second writer raced this one, or a
            # caller skipped the settled-day check. Either way history is not revised.
            raise EvidenceIntegrityError() from error
        except psycopg.Error:
            _unavailable()
        finally:
            connection.close()

    def rows(self, instrument_id: str) -> tuple[TickDay, ...]:
        connection = self._connect()
        try:
            with connection, connection.cursor() as cursor:
                cursor.execute(_SELECT, (instrument_id,))
                fetched = cursor.fetchall()
        except psycopg.Error:
            _unavailable()
        finally:
            connection.close()
        return tuple(
            TickDay(
                instrument_id=row[0], day=row[1], outcome=TickDayOutcome(row[2]), tick_count=row[3],
                first_time_ms=row[4], last_time_ms=row[5], crossed_quotes=row[6],
                point_size=Decimal(row[7]), file_sha256=row[8], detail=row[9], fetched_at=row[10],
            )
            for row in fetched
        )
```

(`JsonValue` is pydantic's, as `cli.py` imports it.)

In `settings.py`, after `evidence_root`:

```python
    tick_root: Path = Path(".local/ticks")
```

- [ ] **Step 4: Run the unit tests** (Step 2's command). Expected: pass.

- [ ] **Step 5: Write the migration** (`migrations/versions/0010_tick_days.py`)

```python
"""Create the append-only record of tick days (Phase 10a).

One row per fetch of one instrument's UTC day. A ``COMPLETE`` or ``EMPTY`` row
settles the day and is unique; ``FAILED`` rows are history, so a later attempt
may settle a day an earlier one could not. The ticks themselves live in files
under ``tick_root``; this table holds each file's digest, so a file changed on
disk is refused by the reader rather than trusted.

Append-only twice over, like the audit ledger: the runtime holds SELECT and
INSERT only, and triggers refuse UPDATE, DELETE and TRUNCATE for every role.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0010_tick_days"
down_revision: str | None = "0009_bars_inside_run_window"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET ROLE trading_house_owner")
    op.execute(
        """
        CREATE TABLE marketdata.tick_days (
            instrument_id TEXT NOT NULL,
            day DATE NOT NULL,
            outcome TEXT NOT NULL CHECK (outcome IN ('COMPLETE', 'EMPTY', 'FAILED')),
            tick_count INTEGER NOT NULL CHECK (tick_count >= 0),
            first_time_ms BIGINT,
            last_time_ms BIGINT,
            crossed_quotes INTEGER NOT NULL CHECK (crossed_quotes >= 0),
            point_size NUMERIC NOT NULL CHECK (point_size > 0),
            file_sha256 TEXT,
            detail TEXT,
            fetched_at TIMESTAMPTZ NOT NULL,
            CONSTRAINT tick_days_complete_shape CHECK (
                outcome <> 'COMPLETE' OR (
                    tick_count > 0 AND file_sha256 ~ '^[0-9a-f]{64}$'
                    AND first_time_ms IS NOT NULL AND last_time_ms IS NOT NULL
                    AND first_time_ms <= last_time_ms AND detail IS NULL
                )
            ),
            CONSTRAINT tick_days_empty_shape CHECK (
                outcome <> 'EMPTY' OR (
                    tick_count = 0 AND crossed_quotes = 0 AND file_sha256 IS NULL
                    AND first_time_ms IS NULL AND last_time_ms IS NULL AND detail IS NULL
                )
            ),
            CONSTRAINT tick_days_failed_shape CHECK (
                outcome <> 'FAILED' OR (detail IS NOT NULL AND file_sha256 IS NULL)
            )
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX tick_days_one_settled
            ON marketdata.tick_days (instrument_id, day)
            WHERE outcome IN ('COMPLETE', 'EMPTY')
        """
    )
    op.execute("CREATE INDEX tick_days_by_day ON marketdata.tick_days (instrument_id, day)")
    op.execute(
        """
        CREATE FUNCTION marketdata.reject_tick_day_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog
        AS $function$
        BEGIN
            RAISE EXCEPTION USING
                ERRCODE = '55000',
                MESSAGE = 'tick days are append-only';
        END;
        $function$
        """
    )
    op.execute(
        """
        CREATE TRIGGER reject_tick_day_row_mutation
        BEFORE UPDATE OR DELETE ON marketdata.tick_days
        FOR EACH ROW EXECUTE FUNCTION marketdata.reject_tick_day_mutation()
        """
    )
    op.execute(
        """
        CREATE TRIGGER reject_tick_day_truncate
        BEFORE TRUNCATE ON marketdata.tick_days
        FOR EACH STATEMENT EXECUTE FUNCTION marketdata.reject_tick_day_mutation()
        """
    )
    op.execute("REVOKE ALL ON TABLE marketdata.tick_days FROM PUBLIC")
    op.execute("REVOKE ALL ON FUNCTION marketdata.reject_tick_day_mutation() FROM PUBLIC")
    op.execute("GRANT SELECT, INSERT ON marketdata.tick_days TO trading_house_runtime")


def downgrade() -> None:
    op.execute("SET ROLE trading_house_owner")
    op.execute("DROP TABLE marketdata.tick_days")
    op.execute("DROP FUNCTION marketdata.reject_tick_day_mutation()")
```

- [ ] **Step 6: Write the integration tests** (`tests/integration/marketdata/test_tick_days.py`; Docker must be running)

```python
from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

import psycopg
import pytest
from pydantic import SecretStr

from trading_house.core.errors import EvidenceIntegrityError
from trading_house.database.connection import open_runtime_connection
from trading_house.marketdata.tick_store import PostgresTickDayStore, TickDay, TickDayOutcome

if TYPE_CHECKING:
    from ...conftest import DatabaseHarness

DAY = date(2026, 10, 6)
FETCHED = datetime(2026, 10, 7, 1, tzinfo=UTC)


def _row(outcome: TickDayOutcome, *, day: date = DAY) -> TickDay:
    if outcome is TickDayOutcome.COMPLETE:
        return TickDay(
            instrument_id="fx.eurusd", day=day, outcome=outcome, tick_count=3, first_time_ms=1,
            last_time_ms=9, crossed_quotes=1, point_size=Decimal("0.00001"), file_sha256="b" * 64,
            fetched_at=FETCHED,
        )
    if outcome is TickDayOutcome.EMPTY:
        return TickDay(
            instrument_id="fx.eurusd", day=day, outcome=outcome, tick_count=0,
            point_size=Decimal("0.00001"), fetched_at=FETCHED,
        )
    return TickDay(
        instrument_id="fx.eurusd", day=day, outcome=outcome, tick_count=0,
        point_size=Decimal("0.00001"), detail="BrokerUnavailableError", fetched_at=FETCHED,
    )


@pytest.fixture
def tick_store(database: DatabaseHarness) -> Iterator[PostgresTickDayStore]:
    yield PostgresTickDayStore(lambda: open_runtime_connection(SecretStr(database.runtime_dsn)))
    # The triggers refuse DELETE and TRUNCATE for every role; replica mode is how a
    # superuser test harness clears an append-only table between tests.
    with psycopg.connect(database.test_superuser_dsn, autocommit=True) as connection:
        connection.execute("SET session_replication_role = replica")
        connection.execute("DELETE FROM marketdata.tick_days")


def test_a_recorded_day_reads_back(tick_store: PostgresTickDayStore) -> None:
    tick_store.record(_row(TickDayOutcome.COMPLETE))
    assert tick_store.rows("fx.eurusd") == (_row(TickDayOutcome.COMPLETE),)


def test_a_settled_day_cannot_be_recorded_twice(tick_store: PostgresTickDayStore) -> None:
    tick_store.record(_row(TickDayOutcome.EMPTY))
    with pytest.raises(EvidenceIntegrityError):
        tick_store.record(_row(TickDayOutcome.COMPLETE))


def test_failures_do_not_block_a_later_settlement(tick_store: PostgresTickDayStore) -> None:
    tick_store.record(_row(TickDayOutcome.FAILED))
    tick_store.record(_row(TickDayOutcome.FAILED))
    tick_store.record(_row(TickDayOutcome.COMPLETE))
    assert [row.outcome for row in tick_store.rows("fx.eurusd")] == [
        TickDayOutcome.FAILED, TickDayOutcome.FAILED, TickDayOutcome.COMPLETE,
    ]


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE marketdata.tick_days SET tick_count = 4",
        "DELETE FROM marketdata.tick_days",
        "TRUNCATE marketdata.tick_days",
    ],
)
def test_the_runtime_cannot_rewrite_history(
    tick_store: PostgresTickDayStore, database: DatabaseHarness, statement: str
) -> None:
    tick_store.record(_row(TickDayOutcome.COMPLETE))
    with (
        open_runtime_connection(SecretStr(database.runtime_dsn)) as connection,
        pytest.raises(psycopg.Error),
    ):
        connection.execute(statement)


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE marketdata.tick_days SET tick_count = 4",
        "DELETE FROM marketdata.tick_days",
        "TRUNCATE marketdata.tick_days",
    ],
)
def test_the_triggers_refuse_even_a_superuser(
    tick_store: PostgresTickDayStore, database: DatabaseHarness, statement: str
) -> None:
    tick_store.record(_row(TickDayOutcome.COMPLETE))
    with (
        psycopg.connect(database.test_superuser_dsn, autocommit=True) as connection,
        pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState, match="append-only"),
    ):
        connection.execute(statement)
```

If `open_runtime_connection` is not usable as a context manager, mirror how `tests/integration/marketdata/test_store_writes.py:186` opens and closes a runtime connection, and report it.

- [ ] **Step 7: Run unit, integration, migration and architecture tests**

Run: `UV_SYSTEM_CERTS=1 uv run pytest tests/unit/marketdata/test_tick_store.py tests/integration/marketdata/test_tick_days.py tests/integration/database tests/acceptance/test_architecture.py -q --no-cov -p no:cacheprovider`
Then ruff and mypy. Expected: all pass. If an existing migration test pins the head revision name, update it to `0010_tick_days` and report it.

- [ ] **Step 8: Commit**

```bash
git add migrations/versions/0010_tick_days.py src/trading_house/marketdata/tick_store.py src/trading_house/settings.py tests/unit/marketdata/test_tick_store.py tests/integration/marketdata/test_tick_days.py
git commit -m "feat: phase 10a append-only tick-day record"
```

- [ ] **Step 9: Mutation proof** — one at a time, restoring after each:
  - Migration: drop `WHERE outcome IN ('COMPLETE', 'EMPTY')` from the unique index. Expected: `test_failures_do_not_block_a_later_settlement` fails. (Re-run the integration file so the container re-migrates; if the harness migrates once per session, run that file alone.)
  - Migration: remove the TRUNCATE trigger. Expected: the superuser TRUNCATE case fails.
  - `coverage_of`: drop `day.weekday() < 5 and`. Expected: no current test fails — **add** a Saturday-and-Sunday case to `test_coverage_counts_and_finds_weekday_gaps` (complete Fri 9th and Mon 12th, nothing on the weekend, gaps still `(7th,)`), confirm it fails under the mutation, restore, confirm it passes.

---

### Task 5: Ingest

**Files:**
- Create: `src/trading_house/marketdata/tick_ingest.py`
- Create: `tests/unit/marketdata/tick_fakes.py`
- Test: `tests/unit/marketdata/test_tick_ingest.py`

**Interfaces:**
- Consumes: Tasks 1, 3, 4. `Clock` from `trading_house.core.clock`; `BrokerUnavailableError`, `ConfigurationError`.
- Produces:
  - `TickProvider(Protocol)`: `ticks(instrument_id: str, start: datetime, end: datetime) -> RawTicks`
  - `HOURS_PER_DAY = 24`, `WALL_EMPTY_WEEKDAYS = 5`, `SETTLE_MARGIN = timedelta(minutes=1)`
  - `last_closed_day(clock: Clock) -> date`
  - `fetch_day(provider, store, root, clock, *, instrument_id, day, point_size) -> TickDay` — records and returns the row; re-raises `BrokerUnavailableError` **after** recording a `FAILED` row.
  - `TickRunSummary` (frozen dataclass): `instrument_id`, `recorded: tuple[TickDay, ...]`, `wall_reached: bool`, `earliest_complete: date | None`; `as_json()`.
  - `backfill_ticks(provider, store, root, clock, *, instrument_id, point_size, until: date) -> TickRunSummary`
  - `update_ticks(provider, store, root, clock, *, instrument_id, point_size) -> TickRunSummary` — `ConfigurationError` when no day is settled yet.

- [ ] **Step 1: Write the fakes** (`tests/unit/marketdata/tick_fakes.py`)

```python
"""Fakes shared by the tick unit, CLI and acceptance tests."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta

import numpy as np

from trading_house.core.errors import BrokerUnavailableError, EvidenceIntegrityError
from trading_house.marketdata.tick_store import TickDay, TickDayOutcome
from trading_house.marketdata.ticks import RawTicks, epoch_ms


def raw_ticks(times_ms: list[int], *, bid: float = 1.10000, ask: float = 1.10010, last: float = 0.0) -> RawTicks:
    n = len(times_ms)
    return RawTicks(
        time_ms=np.array(times_ms, dtype=np.int64),
        bid=np.full(n, bid), ask=np.full(n, ask), last=np.full(n, last),
        volume=np.zeros(n, dtype=np.int64), volume_real=np.zeros(n), flags=np.full(n, 6, dtype=np.int64),
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
            row.instrument_id == day.instrument_id and row.day == day.day
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


ONE_DAY = timedelta(days=1)
```

- [ ] **Step 2: Write the failing tests** (`tests/unit/marketdata/test_tick_ingest.py`)

```python
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tests.unit.marketdata.tick_fakes import (
    FakeTickProvider, InMemoryTickDayStore, day_ticks, raw_ticks, weekdays_only,
)
from trading_house.core.clock import FixedClock
from trading_house.core.errors import BrokerUnavailableError, ConfigurationError
from trading_house.marketdata.tick_files import day_path, read_day_file
from trading_house.marketdata.tick_ingest import (
    backfill_ticks, fetch_day, last_closed_day, update_ticks,
)
from trading_house.marketdata.tick_store import TickDayOutcome
from trading_house.marketdata.ticks import RawTicks, day_digest

POINT = Decimal("0.00001")
TUESDAY = date(2026, 10, 6)
CLOCK = FixedClock(datetime(2026, 10, 7, 1, 0, tzinfo=UTC))  # Wednesday 01:00 UTC


def _fetch(provider: FakeTickProvider, store: InMemoryTickDayStore, root: Path, day: date = TUESDAY):  # type: ignore[no-untyped-def]
    return fetch_day(provider, store, root, CLOCK, instrument_id="fx.eurusd", day=day, point_size=POINT)


def test_a_day_with_ticks_is_complete_and_its_file_matches_its_row(tmp_path: Path) -> None:
    store = InMemoryTickDayStore()
    row = _fetch(FakeTickProvider(day_ticks), store, tmp_path)
    assert row.outcome is TickDayOutcome.COMPLETE
    assert row.tick_count == 48
    stored = read_day_file(day_path(tmp_path, "fx.eurusd", TUESDAY))
    assert row.file_sha256 == day_digest("fx.eurusd", TUESDAY, POINT, stored)
    assert row.first_time_ms == int(stored.time_ms[0])
    assert row.last_time_ms == int(stored.time_ms[-1])
    assert store.recorded == [row]


def test_a_day_is_fetched_as_24_contiguous_hours(tmp_path: Path) -> None:
    provider = FakeTickProvider(day_ticks)
    _fetch(provider, InMemoryTickDayStore(), tmp_path)
    starts = [start for _instrument, start, _end in provider.calls]
    ends = [end for _instrument, _start, end in provider.calls]
    midnight = datetime(2026, 10, 6, tzinfo=UTC)
    assert starts == [midnight + timedelta(hours=h) for h in range(24)]
    assert ends == [midnight + timedelta(hours=h + 1) for h in range(24)]


def test_a_day_without_ticks_is_empty_and_writes_no_file(tmp_path: Path) -> None:
    row = _fetch(FakeTickProvider(), InMemoryTickDayStore(), tmp_path)
    assert row.outcome is TickDayOutcome.EMPTY
    assert not day_path(tmp_path, "fx.eurusd", TUESDAY).exists()


def test_bad_data_is_a_failed_day_with_our_own_reason(tmp_path: Path) -> None:
    printed = FakeTickProvider(lambda day: raw_ticks([1_791_288_000_000], last=1.1))
    row = _fetch(printed, InMemoryTickDayStore(), tmp_path)
    assert row.outcome is TickDayOutcome.FAILED
    assert row.detail is not None and "trade print" in row.detail
    assert not day_path(tmp_path, "fx.eurusd", TUESDAY).exists()


def test_a_lost_broker_is_recorded_then_raised(tmp_path: Path) -> None:
    store = InMemoryTickDayStore()
    with pytest.raises(BrokerUnavailableError):
        _fetch(FakeTickProvider(failing=frozenset({TUESDAY})), store, tmp_path)
    assert [row.outcome for row in store.recorded] == [TickDayOutcome.FAILED]
    assert store.recorded[0].detail == "BrokerUnavailableError"


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (datetime(2026, 10, 7, 0, 0, 30, tzinfo=UTC), date(2026, 10, 5)),
        (datetime(2026, 10, 7, 0, 1, 0, tzinfo=UTC), date(2026, 10, 6)),
        (datetime(2026, 10, 7, 23, 0, tzinfo=UTC), date(2026, 10, 6)),
    ],
)
def test_a_day_is_closed_one_minute_after_midnight(now: datetime, expected: date) -> None:
    assert last_closed_day(FixedClock(now)) == expected


def test_backfill_walks_back_to_until_and_skips_settled_days(tmp_path: Path) -> None:
    store = InMemoryTickDayStore()
    provider = FakeTickProvider(weekdays_only)
    _fetch(provider, store, tmp_path, day=date(2026, 10, 5))
    provider.calls.clear()
    summary = backfill_ticks(
        provider, store, tmp_path, CLOCK, instrument_id="fx.eurusd", point_size=POINT,
        until=date(2026, 10, 1),
    )
    # Tue 6 back to Thu 1, minus Mon 5 which is settled.
    assert provider.days_called() == [
        date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 3), date(2026, 10, 4), date(2026, 10, 6),
    ]
    assert summary.wall_reached is False
    assert summary.earliest_complete == date(2026, 10, 1)


def test_backfill_retries_a_failed_day(tmp_path: Path) -> None:
    store = InMemoryTickDayStore()
    with pytest.raises(BrokerUnavailableError):
        _fetch(FakeTickProvider(failing=frozenset({TUESDAY})), store, tmp_path)
    backfill_ticks(
        FakeTickProvider(weekdays_only), store, tmp_path, CLOCK, instrument_id="fx.eurusd",
        point_size=POINT, until=TUESDAY,
    )
    assert [row.outcome for row in store.rows("fx.eurusd")] == [
        TickDayOutcome.FAILED, TickDayOutcome.COMPLETE,
    ]


def test_backfill_stops_at_five_empty_weekdays(tmp_path: Path) -> None:
    """Ticks only from Thu 1 Oct onward. Walking back from Tue 6: Sep 30 .. Sep 24 are
    five weekdays (30, 29, 28, 25, 24) plus a weekend; the walk stops after the fifth."""

    first_tick_day = date(2026, 10, 1)
    provider = FakeTickProvider(lambda day: weekdays_only(day) if day >= first_tick_day else RawTicks.empty())
    summary = backfill_ticks(
        provider, InMemoryTickDayStore(), tmp_path, CLOCK, instrument_id="fx.eurusd",
        point_size=POINT, until=date(2026, 1, 1),
    )
    assert summary.wall_reached is True
    assert summary.earliest_complete == first_tick_day
    assert min(provider.days_called()) == date(2026, 9, 24)


def test_update_fills_from_the_latest_settled_day_to_the_last_closed_day(tmp_path: Path) -> None:
    store = InMemoryTickDayStore()
    provider = FakeTickProvider(weekdays_only)
    _fetch(provider, store, tmp_path, day=date(2026, 10, 2))
    provider.calls.clear()
    update_ticks(provider, store, tmp_path, CLOCK, instrument_id="fx.eurusd", point_size=POINT)
    assert provider.days_called() == [
        date(2026, 10, 3), date(2026, 10, 4), date(2026, 10, 5), date(2026, 10, 6),
    ]


def test_update_refuses_before_any_backfill(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError):
        update_ticks(
            FakeTickProvider(), InMemoryTickDayStore(), tmp_path, CLOCK, instrument_id="fx.eurusd",
            point_size=POINT,
        )
```

- [ ] **Step 3: Run them to verify they fail** — `UV_SYSTEM_CERTS=1 uv run pytest tests/unit/marketdata/test_tick_ingest.py -q --no-cov -p no:cacheprovider`. Expected: `ModuleNotFoundError`.

- [ ] **Step 4: Write `tick_ingest.py`**

```python
"""Tick backfill and update: one UTC day at a time, 24 hourly requests each.

The recorded days are the cursor, as stored bars are for bar ingest. A day is
fetched only once it has closed (a minute past midnight UTC). A lost broker
stops the run after recording the day as ``FAILED``; bad data records ``FAILED``
and the walk goes on. A backfill infers the broker's history wall from five
consecutive weekday ``EMPTY`` days rather than guessing it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Final, Protocol

from pydantic import JsonValue

from trading_house.core.clock import Clock
from trading_house.core.errors import BrokerUnavailableError, ConfigurationError
from trading_house.marketdata.tick_files import write_day_file
from trading_house.marketdata.tick_store import TickDay, TickDayOutcome, TickDayStore, settled_days
from trading_house.marketdata.ticks import RawTicks, TickDataError, to_tick_arrays

HOURS_PER_DAY: Final = 24
WALL_EMPTY_WEEKDAYS: Final = 5
SETTLE_MARGIN: Final = timedelta(minutes=1)


class TickProvider(Protocol):
    def ticks(self, instrument_id: str, start: datetime, end: datetime) -> RawTicks: ...


def last_closed_day(clock: Clock) -> date:
    return (clock.now() - SETTLE_MARGIN).date() - timedelta(days=1)


def fetch_day(
    provider: TickProvider,
    store: TickDayStore,
    root: Path,
    clock: Clock,
    *,
    instrument_id: str,
    day: date,
    point_size: Decimal,
) -> TickDay:
    midnight = datetime.combine(day, time(), UTC)
    common = {"instrument_id": instrument_id, "day": day, "point_size": point_size}
    try:
        hours = [
            provider.ticks(
                instrument_id, midnight + timedelta(hours=hour), midnight + timedelta(hours=hour + 1)
            )
            for hour in range(HOURS_PER_DAY)
        ]
        arrays = to_tick_arrays(RawTicks.concatenate(hours), point_size)
    except BrokerUnavailableError:
        store.record(
            TickDay(
                **common, outcome=TickDayOutcome.FAILED, tick_count=0,
                detail="BrokerUnavailableError", fetched_at=clock.now(),
            )
        )
        raise
    except TickDataError as error:
        row = TickDay(
            **common, outcome=TickDayOutcome.FAILED, tick_count=0,
            detail=f"TickDataError: {error}", fetched_at=clock.now(),
        )
        store.record(row)
        return row
    if len(arrays) == 0:
        row = TickDay(**common, outcome=TickDayOutcome.EMPTY, tick_count=0, fetched_at=clock.now())
    else:
        row = TickDay(
            **common,
            outcome=TickDayOutcome.COMPLETE,
            tick_count=len(arrays),
            first_time_ms=int(arrays.time_ms[0]),
            last_time_ms=int(arrays.time_ms[-1]),
            crossed_quotes=arrays.crossed_quotes(),
            file_sha256=write_day_file(root, instrument_id, day, point_size, arrays),
            fetched_at=clock.now(),
        )
    store.record(row)
    return row


@dataclass(frozen=True, slots=True)
class TickRunSummary:
    instrument_id: str
    recorded: tuple[TickDay, ...]
    wall_reached: bool
    earliest_complete: date | None

    def as_json(self) -> dict[str, JsonValue]:
        def count(outcome: TickDayOutcome) -> int:
            return sum(1 for row in self.recorded if row.outcome is outcome)

        return {
            "instrument_id": self.instrument_id,
            "days_complete": count(TickDayOutcome.COMPLETE),
            "days_empty": count(TickDayOutcome.EMPTY),
            "days_failed": count(TickDayOutcome.FAILED),
            "ticks": sum(row.tick_count for row in self.recorded),
            "wall_reached": self.wall_reached,
            "earliest_complete": None if self.earliest_complete is None else self.earliest_complete.isoformat(),
        }


def backfill_ticks(
    provider: TickProvider,
    store: TickDayStore,
    root: Path,
    clock: Clock,
    *,
    instrument_id: str,
    point_size: Decimal,
    until: date,
) -> TickRunSummary:
    settled = settled_days(store.rows(instrument_id))
    recorded: list[TickDay] = []
    empty_weekdays = 0
    earliest: date | None = None
    wall = False
    day = last_closed_day(clock)
    while day >= until:
        row = settled.get(day)
        if row is None:
            row = fetch_day(
                provider, store, root, clock, instrument_id=instrument_id, day=day, point_size=point_size
            )
            recorded.append(row)
        if row.outcome is TickDayOutcome.COMPLETE:
            earliest = day
            empty_weekdays = 0
        elif row.outcome is TickDayOutcome.EMPTY and day.weekday() < 5:
            empty_weekdays += 1
            if empty_weekdays >= WALL_EMPTY_WEEKDAYS:
                wall = True
                break
        day -= timedelta(days=1)
    return TickRunSummary(instrument_id, tuple(recorded), wall, earliest)


def update_ticks(
    provider: TickProvider,
    store: TickDayStore,
    root: Path,
    clock: Clock,
    *,
    instrument_id: str,
    point_size: Decimal,
) -> TickRunSummary:
    settled = settled_days(store.rows(instrument_id))
    if not settled:
        raise ConfigurationError() from ValueError("no settled tick day yet: run backfill first")
    recorded: list[TickDay] = []
    earliest: date | None = None
    day = max(settled) + timedelta(days=1)
    last = last_closed_day(clock)
    while day <= last:
        row = fetch_day(
            provider, store, root, clock, instrument_id=instrument_id, day=day, point_size=point_size
        )
        recorded.append(row)
        if row.outcome is TickDayOutcome.COMPLETE and earliest is None:
            earliest = day
        day += timedelta(days=1)
    return TickRunSummary(instrument_id, tuple(recorded), False, earliest)
```

(mypy may reject `**common` into `TickDay(...)` because the dict's value type is a union; if so, pass the three fields explicitly in each constructor call.)

- [ ] **Step 5: Run the tests, ruff and mypy** (Step 3's command). Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/trading_house/marketdata/tick_ingest.py tests/unit/marketdata/tick_fakes.py tests/unit/marketdata/test_tick_ingest.py
git commit -m "feat: phase 10a tick backfill and update"
```

- [ ] **Step 7: Mutation proof** — one at a time, restoring after each:
  - `SETTLE_MARGIN` → `timedelta(0)`. Expected: the `00:00:30` closing case fails.
  - In `backfill_ticks`, remove the `and day.weekday() < 5`. Expected: `test_backfill_stops_at_five_empty_weekdays` fails (it stops too early).
  - In `fetch_day`, move `store.record(...)` in the `BrokerUnavailableError` branch after `raise` (so nothing is recorded). Expected: `test_a_lost_broker_is_recorded_then_raised` fails.

---

### Task 6: The reader and the window digest

**Files:**
- Create: `src/trading_house/marketdata/tick_reader.py`
- Test: `tests/unit/marketdata/test_tick_reader.py`

**Interfaces:**
- Consumes: Tasks 1, 3, 4, 5's fakes.
- Produces:
  - `TickWindowDigest` (frozen dataclass): `sha256: str`, `days: int`, `ticks: int`; `as_json()`.
  - `TickReader(store: TickDayStore, root: Path)`:
    - `ticks(instrument_id: str, start_ms: int, end_ms: int, *, as_of_ms: int) -> TickArrays`
    - `window_digest(instrument_id: str, start_ms: int, end_ms: int) -> TickWindowDigest`
  - Refusals: `CoverageError` for an empty window, a day without a settled row; `EvidenceIntegrityError` for a missing file or a digest mismatch.

- [ ] **Step 1: Write the failing tests**

```python
from datetime import UTC, date, datetime, time, timedelta
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
        provider = FakeTickProvider(day_ticks if has_ticks else lambda _day: RawTicks.empty())
        fetch_day(provider, store, tmp_path, CLOCK, instrument_id="fx.eurusd", day=day, point_size=POINT)
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
            FakeTickProvider(failing=frozenset({TUE})), store, tmp_path, CLOCK,
            instrument_id="fx.eurusd", day=TUE, point_size=POINT,
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
    write_day_file(tmp_path, "fx.eurusd", MON, POINT, original.select(np.arange(len(original)) < 40))
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
    assert first.days == 2 and first.ticks == 96
    assert reader.window_digest("fx.eurusd", _ms(MON), _ms(WED) - 1).sha256 != first.sha256


def test_the_window_digest_changes_with_the_ticks(tmp_path: Path) -> None:
    a = TickReader(_stored(tmp_path / "a", {MON: True}), tmp_path / "a")
    store_b = InMemoryTickDayStore()
    fetch_day(
        FakeTickProvider(lambda day: day_ticks(day, 47)), store_b, tmp_path / "b", CLOCK,
        instrument_id="fx.eurusd", day=MON, point_size=POINT,
    )
    b = TickReader(store_b, tmp_path / "b")
    assert a.window_digest("fx.eurusd", _ms(MON), _ms(TUE)).sha256 != b.window_digest(
        "fx.eurusd", _ms(MON), _ms(TUE)
    ).sha256
```


- [ ] **Step 2: Run them to verify they fail** — `UV_SYSTEM_CERTS=1 uv run pytest tests/unit/marketdata/test_tick_reader.py -q --no-cov -p no:cacheprovider`. Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Write `tick_reader.py`**

```python
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
from trading_house.marketdata.ticks import TickArrays, day_digest

WINDOW_DIGEST_DOMAIN: Final[bytes] = b"trading-house/tick-window/v1\0"


@dataclass(frozen=True, slots=True)
class TickWindowDigest:
    sha256: str
    days: int
    ticks: int

    def as_json(self) -> dict[str, JsonValue]:
        return {"sha256": self.sha256, "days": self.days, "ticks": self.ticks}


def _day_of(ms: int) -> date:
    return datetime.fromtimestamp(ms / 1000, UTC).date()


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
                if day_digest(instrument_id, day, row.point_size, arrays) != row.file_sha256:
                    raise EvidenceIntegrityError() from ValueError(f"{instrument_id} {day} changed on disk")
                days.append((row, arrays))
            day += timedelta(days=1)
        return days

    def ticks(self, instrument_id: str, start_ms: int, end_ms: int, *, as_of_ms: int) -> TickArrays:
        joined = TickArrays.concatenate([arrays for _row, arrays in self._verified(instrument_id, start_ms, end_ms)])
        keep = (joined.time_ms >= start_ms) & (joined.time_ms < end_ms) & (joined.time_ms < as_of_ms)
        return joined.select(keep)

    def window_digest(self, instrument_id: str, start_ms: int, end_ms: int) -> TickWindowDigest:
        verified = self._verified(instrument_id, start_ms, end_ms)
        digest = hashlib.sha256(WINDOW_DIGEST_DOMAIN)
        digest.update(f"{instrument_id}\0{start_ms}\0{end_ms}\0".encode())
        ticks = 0
        for row, arrays in verified:
            digest.update(f"{row.day.isoformat()}\0{row.outcome.value}\0{row.file_sha256 or '-'}\0".encode())
            ticks += int(((arrays.time_ms >= start_ms) & (arrays.time_ms < end_ms)).sum())
        return TickWindowDigest(sha256=digest.hexdigest(), days=len(verified), ticks=ticks)
```

- [ ] **Step 4: Run the tests, ruff and mypy** (Step 2's command). Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/trading_house/marketdata/tick_reader.py tests/unit/marketdata/test_tick_reader.py
git commit -m "feat: phase 10a verified point-in-time tick reader and window digest"
```

- [ ] **Step 6: Mutation proof** — one at a time, restoring after each:
  - Drop `& (joined.time_ms < as_of_ms)`. Expected: `test_nothing_after_as_of_is_returned` fails.
  - Skip the digest comparison. Expected: `test_a_file_changed_on_disk_is_refused` fails.
  - Remove `{start_ms}\0{end_ms}\0` from the window preimage. Expected: the "names its window" assertion fails.

---

### Task 7: CLI commands

**Files:**
- Modify: `src/trading_house/cli.py`
- Test: `tests/unit/test_cli_ticks.py` (new)

**Interfaces:**
- Consumes: Tasks 4–6; `Mt5BrokerAdapter.ticks` and `describe_instrument` (Task 2 and existing).
- Produces:
  - `_history_provider(venue_binding, venue_binding_signature, venue_binding_public_key, *, request_timeout_seconds: float = 10.0)` yielding `(Mt5BrokerAdapter, VenueBinding, int)`.
  - `_TICK_REQUEST_TIMEOUT_SECONDS = 120.0`; `_tick_store() -> PostgresTickDayStore`; `_tick_root() -> Path`.
  - Commands: `data ticks backfill --instrument ID --from DATE`, `data ticks update --instrument ID`, `data ticks coverage`, `research dataset tick-digest --instrument ID --start T --end T` (UTC, half-open).

- [ ] **Step 1: Write the failing tests** (`tests/unit/test_cli_ticks.py`)

```python
from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from tests.unit.marketdata.tick_fakes import FakeTickProvider, InMemoryTickDayStore, weekdays_only
from tests.unit.test_cli import FAKE_DSN
from trading_house import cli
from trading_house.core.clock import FixedClock

runner = CliRunner()
NOW = datetime(2026, 10, 7, 1, tzinfo=UTC)


class _FakeAdapter(FakeTickProvider):
    def describe_instrument(self, instrument_id: str) -> Any:
        return SimpleNamespace(point_size=Decimal("0.00001"))


@pytest.fixture
def wired(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.setenv("TRADING_HOUSE_DATABASE_DSN", FAKE_DSN)
    monkeypatch.setenv("TRADING_HOUSE_TICK_ROOT", str(tmp_path))
    adapter = _FakeAdapter(weekdays_only)
    store = InMemoryTickDayStore()
    timeouts: list[float] = []

    @contextmanager
    def provider(*_args: Any, request_timeout_seconds: float = 10.0) -> Iterator[Any]:
        timeouts.append(request_timeout_seconds)
        yield adapter, None, 0

    monkeypatch.setattr(cli, "_history_provider", provider)
    monkeypatch.setattr(cli, "_tick_store", lambda: store)
    monkeypatch.setattr(cli, "SystemClock", lambda: FixedClock(NOW))
    return {"adapter": adapter, "store": store, "timeouts": timeouts, "root": tmp_path}


def test_backfill_records_days_and_uses_the_long_timeout(wired: dict[str, Any]) -> None:
    result = runner.invoke(
        cli.app, ["data", "ticks", "backfill", "--instrument", "fx.eurusd", "--from", "2026-10-05"]
    )
    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["days_complete"] == 2  # Mon 5 and Tue 6
    assert wired["timeouts"] == [120.0]


def test_update_after_backfill_fetches_nothing_new_on_the_same_day(wired: dict[str, Any]) -> None:
    runner.invoke(cli.app, ["data", "ticks", "backfill", "--instrument", "fx.eurusd", "--from", "2026-10-05"])
    result = runner.invoke(cli.app, ["data", "ticks", "update", "--instrument", "fx.eurusd"])
    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout)["days_complete"] == 0


def test_update_before_backfill_is_a_configuration_error(wired: dict[str, Any]) -> None:
    result = runner.invoke(cli.app, ["data", "ticks", "update", "--instrument", "fx.eurusd"])
    assert result.exit_code == cli.ExitCode.CONFIGURATION


def test_tick_digest_prints_a_window_digest(wired: dict[str, Any]) -> None:
    runner.invoke(cli.app, ["data", "ticks", "backfill", "--instrument", "fx.eurusd", "--from", "2026-10-05"])
    result = runner.invoke(
        cli.app,
        ["research", "dataset", "tick-digest", "--instrument", "fx.eurusd",
         "--start", "2026-10-05T00:00:00", "--end", "2026-10-07T00:00:00"],
    )
    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["days"] == 2 and payload["ticks"] == 96 and len(payload["sha256"]) == 64
```

(Check whether `cli._run` prints the operation's dict at top level or under a key, by reading `_emit`; adjust the `payload[...]` lookups to match and report it. `data ticks coverage` loads the signed venue binding; cover it in Task 8's acceptance test instead of here.)

- [ ] **Step 2: Run them to verify they fail** — `UV_SYSTEM_CERTS=1 uv run pytest tests/unit/test_cli_ticks.py -q --no-cov -p no:cacheprovider`. Expected: FAIL (no `ticks` command / no `_tick_store`).

- [ ] **Step 3: Implement in `cli.py`**

1. `_history_provider`: add a keyword-only parameter `request_timeout_seconds: float = 10.0`, pass it as `Mt5Gateway(terminal_factory(probe_symbol), clock=clock, request_timeout_seconds=request_timeout_seconds)`, and change its return annotation's first element from `HistoryProvider` to `Mt5BrokerAdapter`.
2. Imports: `PostgresTickDayStore`, `coverage_of` from `trading_house.marketdata.tick_store`; `backfill_ticks`, `update_ticks` from `trading_house.marketdata.tick_ingest`; `TickReader` from `trading_house.marketdata.tick_reader`; `epoch_ms` from `trading_house.marketdata.ticks`.
3. Helpers and commands (place `ticks_app` beside `data_app`'s other definitions and `data_app.add_typer(ticks_app, name="ticks")` beside the other `add_typer` calls):

```python
ticks_app = typer.Typer(no_args_is_help=True, help="Tick commands.")
_TICK_REQUEST_TIMEOUT_SECONDS = 120.0
"""Per hourly request. The gateway's 10 s default suits a bar page; an hour of
ticks the terminal must first download from the broker can take far longer."""


def _tick_store() -> PostgresTickDayStore:
    settings = _settings()
    return PostgresTickDayStore(lambda: open_runtime_connection(settings.database_dsn))


def _tick_root() -> Path:
    return _settings().tick_root


@ticks_app.command("backfill")
def data_ticks_backfill(
    instrument: Annotated[str, typer.Option("--instrument", help="Instrument id.")],
    from_: Annotated[datetime, typer.Option("--from", help="Earliest UTC day to fetch.")],
    venue_binding: Annotated[Path, typer.Option("--venue-binding")] = DEFAULT_BINDING,
    venue_binding_signature: Annotated[
        Path, typer.Option("--venue-binding-signature")
    ] = DEFAULT_BINDING_SIGNATURE,
    venue_binding_public_key: Annotated[
        Path, typer.Option("--venue-binding-public-key")
    ] = DEFAULT_PUBLIC_KEY,
) -> None:
    """Walk back from yesterday to ``--from`` or the broker's tick wall, one UTC day at a time."""

    def operation() -> dict[str, JsonValue]:
        with _history_provider(
            venue_binding, venue_binding_signature, venue_binding_public_key,
            request_timeout_seconds=_TICK_REQUEST_TIMEOUT_SECONDS,
        ) as (provider, _binding, _offset):
            summary = backfill_ticks(
                provider, _tick_store(), _tick_root(), SystemClock(),
                instrument_id=instrument,
                point_size=provider.describe_instrument(instrument).point_size,
                until=_as_utc(from_).date(),
            )
        return summary.as_json()

    _run(operation)


@ticks_app.command("update")
def data_ticks_update(
    instrument: Annotated[str, typer.Option("--instrument", help="Instrument id.")],
    venue_binding: Annotated[Path, typer.Option("--venue-binding")] = DEFAULT_BINDING,
    venue_binding_signature: Annotated[
        Path, typer.Option("--venue-binding-signature")
    ] = DEFAULT_BINDING_SIGNATURE,
    venue_binding_public_key: Annotated[
        Path, typer.Option("--venue-binding-public-key")
    ] = DEFAULT_PUBLIC_KEY,
) -> None:
    """Fetch every closed UTC day after the latest settled one, through yesterday."""

    def operation() -> dict[str, JsonValue]:
        with _history_provider(
            venue_binding, venue_binding_signature, venue_binding_public_key,
            request_timeout_seconds=_TICK_REQUEST_TIMEOUT_SECONDS,
        ) as (provider, _binding, _offset):
            summary = update_ticks(
                provider, _tick_store(), _tick_root(), SystemClock(),
                instrument_id=instrument,
                point_size=provider.describe_instrument(instrument).point_size,
            )
        return summary.as_json()

    _run(operation)


@ticks_app.command("coverage")
def data_ticks_coverage(
    venue_binding: Annotated[Path, typer.Option("--venue-binding")] = DEFAULT_BINDING,
    venue_binding_signature: Annotated[
        Path, typer.Option("--venue-binding-signature")
    ] = DEFAULT_BINDING_SIGNATURE,
    venue_binding_public_key: Annotated[
        Path, typer.Option("--venue-binding-public-key")
    ] = DEFAULT_PUBLIC_KEY,
) -> None:
    """Report the tick days the store holds, per bound instrument. Never touches MetaTrader5."""

    def operation() -> dict[str, JsonValue]:
        binding = load_venue_binding(venue_binding, venue_binding_signature, venue_binding_public_key)
        store = _tick_store()
        return {
            "coverage": {
                instrument_id: coverage_of(instrument_id, store.rows(instrument_id)).as_json()
                for instrument_id in sorted(binding.instruments)
            }
        }

    _run(operation)


@dataset_app.command("tick-digest")
def research_dataset_tick_digest(
    instrument: Annotated[str, typer.Option("--instrument", help="Instrument id.")],
    start: Annotated[datetime, typer.Option("--start", help="Window start, UTC, inclusive.")],
    end: Annotated[datetime, typer.Option("--end", help="Window end, UTC, exclusive.")],
) -> None:
    """Print the digest of the tick days a window reads, verifying every day file."""

    def operation() -> dict[str, JsonValue]:
        reader = TickReader(_tick_store(), _tick_root())
        return reader.window_digest(
            instrument, epoch_ms(_as_utc(start)), epoch_ms(_as_utc(end))
        ).as_json()

    _run(operation)
```

- [ ] **Step 4: Run the tests, the whole CLI suite, ruff and mypy**

Run: `UV_SYSTEM_CERTS=1 uv run pytest tests/unit/test_cli_ticks.py tests/unit/test_cli.py -q --no-cov -p no:cacheprovider`, then ruff and mypy. Expected: all pass, Session Momentum pins included.

- [ ] **Step 5: Commit**

```bash
git add src/trading_house/cli.py tests/unit/test_cli_ticks.py
git commit -m "feat: phase 10a data ticks commands and the tick-digest command"
```

- [ ] **Step 6: Mutation proof** — pass `request_timeout_seconds=10.0` in `data_ticks_backfill`. Expected: `test_backfill_records_days_and_uses_the_long_timeout` fails. Restore.

---

### Task 8: Collection script, documentation, acceptance, live test, verification

**Files:**
- Create: `scripts/collect_ticks.ps1`
- Create: `tests/acceptance/test_phase10a.py`
- Create: `tests/live/test_mt5_ticks.py`
- Modify: `README.md` (Phase 10a section; operator-commands table rows)
- Modify: `docs/superpowers/specs/2026-10-07-phase-10a-tick-ingest-design.md` (§7, §9, §10 amendments; status)

- [ ] **Step 1: The script** (`scripts/collect_ticks.ps1`)

```powershell
# Collect every closed UTC day of ticks not yet stored, for both bound instruments.
# Run Monday-Friday after 00:30 UTC. Reads .env for this process only: the
# application itself never loads .env (see RuntimeSettings).
$ErrorActionPreference = "Stop"
Set-Location -Path (Split-Path -Parent $PSScriptRoot)
Get-Content .env | Where-Object { $_ -match '^[A-Z_]+=' } | ForEach-Object {
    $name, $value = $_ -split '=', 2
    [Environment]::SetEnvironmentVariable($name, $value, 'Process')
}
$env:UV_SYSTEM_CERTS = "1"
$failed = 0
foreach ($instrument in @("fx.eurusd", "metal.xauusd")) {
    uv run trading-house data ticks update --instrument $instrument
    if ($LASTEXITCODE -ne 0) { $failed = $LASTEXITCODE }
}
exit $failed
```

- [ ] **Step 2: The acceptance test** (`tests/acceptance/test_phase10a.py`)

```python
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
        FakeTickProvider(weekdays_only), store, tmp_path, clock, instrument_id="fx.eurusd",
        point_size=Decimal("0.00001"), until=date(2026, 10, 1),
    )
    assert summary.as_json()["days_complete"] == 4  # Thu 1, Fri 2, Mon 5, Tue 6
    coverage = coverage_of("fx.eurusd", store.rows("fx.eurusd"))
    assert coverage.weekday_gaps == ()
    reader = TickReader(store, tmp_path)
    start = epoch_ms(datetime.combine(date(2026, 10, 1), time(), UTC))
    end = epoch_ms(datetime.combine(date(2026, 10, 7), time(), UTC))
    assert len(reader.ticks("fx.eurusd", start, end, as_of_ms=end)) == 4 * 48
    digest = reader.window_digest("fx.eurusd", start, end)
    assert digest.days == 6 and digest.ticks == 4 * 48
    day_path(tmp_path, "fx.eurusd", date(2026, 10, 2)).write_bytes(b"tampered")
    with pytest.raises(EvidenceIntegrityError):
        reader.window_digest("fx.eurusd", start, end)
```

- [ ] **Step 3: The live test** (`tests/live/test_mt5_ticks.py`)

```python
"""One recent hour of real ticks for each bound instrument. Skipped when no
suitable terminal is present or the market is closed (``skip_reason()``)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from trading_house.brokers.mt5.boundary import TerminalPort
from trading_house.brokers.mt5.gateway import Mt5Gateway
from trading_house.constitution.binding import load_venue_binding
from trading_house.core.clock import SystemClock
from trading_house.marketdata.ticks import epoch_ms, to_tick_arrays

from .conftest import skip_reason

_SKIP = skip_reason()
pytestmark = [pytest.mark.mt5, pytest.mark.skipif(_SKIP is not None, reason=_SKIP or "")]
ROOT = Path(__file__).resolve().parents[2]


def _terminal() -> TerminalPort:
    from trading_house.brokers.mt5.terminal import Mt5Terminal

    return Mt5Terminal()


@pytest.mark.parametrize("instrument_id", ["fx.eurusd", "metal.xauusd"])
def test_a_real_hour_of_ticks_converts_exactly(instrument_id: str) -> None:
    from trading_house.brokers.mt5.adapter import Mt5BrokerAdapter

    binding = load_venue_binding(
        ROOT / "config/venue_binding.mt5.yaml",
        ROOT / "config/venue_binding.mt5.yaml.sig",
        ROOT / "config/risk_constitution.public.pem",
    )
    end = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    start = end - timedelta(hours=1)
    with Mt5Gateway(_terminal(), clock=SystemClock(), request_timeout_seconds=120.0) as gateway:
        adapter = Mt5BrokerAdapter(gateway, binding, clock=SystemClock())
        point = adapter.describe_instrument(instrument_id).point_size
        raw = adapter.ticks(instrument_id, start, end)
    assert len(raw) > 0
    arrays = to_tick_arrays(raw, point)
    assert int(arrays.time_ms.min()) >= epoch_ms(start)
    assert int(arrays.time_ms.max()) < epoch_ms(end)
    assert int(arrays.bid.min()) > 0
```

- [ ] **Step 4: Documentation**

README — a `## Phase 10a — Tick ingest and storage` section after the Phase 9 evidence section, covering, in prose and with commands:
- what is stored (one `.npz` per instrument per UTC day under `TRADING_HOUSE_TICK_ROOT`, integer points, `marketdata.tick_days` record with outcomes), measured volumes and the ~2-year rolling broker depth;
- the commands `data ticks backfill --instrument ID --from DATE`, `data ticks update --instrument ID`, `data ticks coverage`, `research dataset tick-digest --instrument ID --start T --end T`, and that backfill/update refuse while the market is closed;
- daily collection: `scripts/collect_ticks.ps1`, and the scheduled task **the operator installs**:

```powershell
schtasks /Create /TN "TradingHouse\CollectTicks" /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 02:30 /TR "powershell -NoProfile -ExecutionPolicy Bypass -File C:\Users\sourc\Downloads\Multi-Agent\scripts\collect_ticks.ps1"
```

  with the note that `/ST` is local time (02:30 in Eswatini, UTC+2, is 00:30 UTC) and that the repository never installs it;
- add the four commands to the operator-commands table.

Spec amendments, each marked "amended 2026-10-07 by the Phase 10a plan":
- §7: "One request per UTC day" → "24 hourly requests per UTC day, with the gateway's request timeout raised to 120 s for tick commands (the default 10 s cannot cover a day's download)".
- §9: `research dataset digest --ticks` → `research dataset tick-digest`.
- §10: the acceptance file asserts the `np.load`/`np.save*` boundary; the terminal cap lives in `test_architecture.py`.
- `**Status:** approved design` → `implemented`.

- [ ] **Step 5: Whole-suite verification** (Docker running)

```
UV_SYSTEM_CERTS=1 uv run ruff format --check .
UV_SYSTEM_CERTS=1 uv run ruff check .
UV_SYSTEM_CERTS=1 uv run mypy
UV_SYSTEM_CERTS=1 uv run pytest tests/unit tests/acceptance tests/property -q -p no:cacheprovider
UV_SYSTEM_CERTS=1 uv run pytest tests/integration/marketdata tests/integration/database -q --no-cov -p no:cacheprovider
UV_SYSTEM_CERTS=1 uv run pytest tests/live/test_mt5_ticks.py -q --no-cov -p no:cacheprovider
```

Expected: clean; coverage ≥ 95%; integration green; the live test passes when the market is open (or skips, with its reason, when it is not — report which).

- [ ] **Step 6: Commit**

```bash
git add scripts/collect_ticks.ps1 tests/acceptance/test_phase10a.py tests/live/test_mt5_ticks.py README.md docs/superpowers/specs/2026-10-07-phase-10a-tick-ingest-design.md
git commit -m "docs: phase 10a README, collection script, acceptance and live tests"
```

- [ ] **Step 7: Mutation proof** — in `tests/acceptance/test_phase10a.py`'s guard, add a temporary `np.load` call to `src/trading_house/marketdata/tick_reader.py` (e.g. an unused function). Expected: `test_only_tick_files_opens_numpy_files` fails naming `tick_reader.py`. Restore.

---

## After the tasks: the first real collection (operator steps)

1. Start Docker Desktop and apply the migration as the migrator: `uv run alembic -x url=postgresql+psycopg://trading_house_migrator:PASSWORD@127.0.0.1/trading_house upgrade head` (and the research database, as the README describes).
2. With the market open and the terminal logged in: `uv run trading-house data ticks backfill --instrument fx.eurusd --from 2024-01-01`, then the same for `metal.xauusd`. Expect several hours: about 500 days × 24 requests each.
3. `uv run trading-house data ticks coverage`; record the stored span, days and ticks in the README.
4. Install the scheduled task yourself (README command), then confirm the next weekday's `coverage` shows yesterday.

## Self-Review

- **Spec coverage:** §4 tick model → Task 1; §5 file → Task 3 (+ setting in Task 4); §6 record → Task 4; §7 ingest → Tasks 2 and 5 (hourly requests: deviation 1); §8 collection → Task 8; §9 reader and window digest → Task 6 and Task 7 (`tick-digest`: deviation 2); §10 testing → each task, Task 8 acceptance and live; §11 exclusions respected; D-6 cap → Task 2.
- **Placeholders:** none; the "check and report" notes name the exact file and what to verify.
- **Type consistency:** `RawTicks`, `TickArrays`, `epoch_ms`, `to_tick_arrays`, `day_digest`, `Mt5TickBatch`, `copy_ticks_range`, `ticks`, `day_path`, `write_day_file`, `read_day_file`, `TickDay`, `TickDayOutcome`, `TickDayStore`, `PostgresTickDayStore`, `settled_days`, `coverage_of`, `TickCoverage`, `fetch_day`, `backfill_ticks`, `update_ticks`, `last_closed_day`, `TickRunSummary`, `TickReader`, `TickWindowDigest`, `_tick_store`, `_tick_root` are spelled the same in every task that uses them.
