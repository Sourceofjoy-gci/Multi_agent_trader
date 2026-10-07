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
