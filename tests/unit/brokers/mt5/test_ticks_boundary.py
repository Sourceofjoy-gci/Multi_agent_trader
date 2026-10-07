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


def test_a_failed_terminal_call_is_unavailable_not_empty() -> None:
    """Unlike bar history, an empty tick hour is a fact ingest records as EMPTY;
    a failed call must not be mistaken for one."""

    adapter, gateway = _adapter(_ServerFrameTerminal(None))
    try:
        with pytest.raises(BrokerUnavailableError):
            adapter.ticks("fx.eurusd", START, END)
    finally:
        gateway.stop()
