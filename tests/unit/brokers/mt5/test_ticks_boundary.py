from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from tests.unit.brokers.mt5.conftest import FakeTerminal
from tests.unit.brokers.mt5.test_adapter import BINDING
from trading_house.brokers.mt5.adapter import Mt5BrokerAdapter
from trading_house.brokers.mt5.boundary import Mt5TickBatch
from trading_house.brokers.mt5.gateway import Mt5Gateway
from trading_house.core.clock import SystemClock
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
        ("time", "<i8"),
        ("bid", "<f8"),
        ("ask", "<f8"),
        ("last", "<f8"),
        ("volume", "<u8"),
        ("time_msc", "<i8"),
        ("flags", "<u4"),
        ("volume_real", "<f8"),
    ]
    raw = np.array([(1, 1.1, 1.2, 0.0, 0, 1000, 6, 0.0)], dtype=dtype)
    batch = Mt5TickBatch.from_structured(raw)
    assert batch.time_msc.tolist() == [1000]
    assert batch.bid.tolist() == [1.1]
    assert batch.flags.dtype == np.int64


class _ServerFrameTerminal(FakeTerminal):
    def __init__(self, *batches: Mt5TickBatch | None) -> None:
        """Answers each request with the next batch, repeating the last."""

        super().__init__()
        self.batches = batches
        self.tick_requests: list[tuple[str, datetime, datetime]] = []

    def server_utc_offset_seconds(self) -> int:
        return OFFSET_SECONDS

    def copy_ticks_range(
        self, server_symbol: str, start: datetime, end: datetime
    ) -> Mt5TickBatch | None:
        self.tick_requests.append((server_symbol, start, end))
        return self.batches[min(len(self.tick_requests), len(self.batches)) - 1]


def _adapter(
    terminal: FakeTerminal, pauses: list[float] | None = None
) -> tuple[Mt5BrokerAdapter, Mt5Gateway]:
    gateway = Mt5Gateway(terminal, clock=SystemClock(), request_timeout_seconds=5.0)
    gateway.start()
    sink: list[float] = [] if pauses is None else pauses
    return Mt5BrokerAdapter(gateway, BINDING, clock=SystemClock(), pause=sink.append), gateway


def test_ticks_are_moved_from_the_server_frame_to_utc_and_end_is_excluded() -> None:
    """MT5 answers in its own clock and end-inclusively; the adapter answers in UTC,
    half-open, as ingest windows are planned."""

    start_ms, end_ms, shift = epoch_ms(START), epoch_ms(END), OFFSET_SECONDS * 1000
    terminal = _ServerFrameTerminal(
        _batch([start_ms + shift, start_ms + 1500 + shift, end_ms + shift])
    )
    adapter, gateway = _adapter(terminal)
    try:
        raw = adapter.ticks("fx.eurusd", START, END)
    finally:
        gateway.stop()
    assert raw.time_ms.tolist() == [start_ms, start_ms + 1500]
    assert [request[1:] for request in terminal.tick_requests] == [(START, END)]


def test_winter_ticks_are_moved_at_the_winter_offset_not_todays() -> None:
    """The binding's zone, not the gateway's measured offset, converts history:
    the terminal here reports today's UTC+3, but a January hour on an EU-rules
    server is UTC+2."""

    winter = datetime(2026, 1, 14, 12, 0, tzinfo=UTC)
    start_ms, shift = epoch_ms(winter), 2 * 3600 * 1000
    terminal = _ServerFrameTerminal(_batch([start_ms + shift, start_ms + 1500 + shift]))
    adapter, gateway = _adapter(terminal)
    try:
        raw = adapter.ticks("fx.eurusd", winter, winter + timedelta(hours=1))
    finally:
        gateway.stop()
    assert raw.time_ms.tolist() == [start_ms, start_ms + 1500]


def test_a_failed_terminal_call_is_unavailable_not_empty() -> None:
    """Unlike bar history, an empty tick hour is a fact ingest records as EMPTY;
    a failed call must not be mistaken for one."""

    adapter, gateway = _adapter(_ServerFrameTerminal(None))
    try:
        with pytest.raises(BrokerUnavailableError):
            adapter.ticks("fx.eurusd", START, END)
    finally:
        gateway.stop()


def test_a_call_that_fails_and_then_succeeds_is_retried() -> None:
    """The terminal's first request for history it has not downloaded often
    answers "Call failed" and succeeds on repeat; that must not abort a backfill."""

    start_ms, shift = epoch_ms(START), OFFSET_SECONDS * 1000
    terminal = _ServerFrameTerminal(None, None, _batch([start_ms + shift]))
    pauses: list[float] = []
    adapter, gateway = _adapter(terminal, pauses)
    try:
        raw = adapter.ticks("fx.eurusd", START, END)
    finally:
        gateway.stop()
    assert raw.time_ms.tolist() == [start_ms]
    assert len(terminal.tick_requests) == 3
    assert pauses == [1.0, 1.0]


def test_three_failed_calls_are_unavailable() -> None:
    terminal = _ServerFrameTerminal(None, None, None, _batch([epoch_ms(START)]))
    adapter, gateway = _adapter(terminal)
    try:
        with pytest.raises(BrokerUnavailableError):
            adapter.ticks("fx.eurusd", START, END)
    finally:
        gateway.stop()
    assert len(terminal.tick_requests) == 3
