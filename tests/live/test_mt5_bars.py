"""Live test: real H1 history through Mt5BrokerAdapter, needs a demo terminal.

Marked ``mt5`` and skipped when no suitable terminal is present, so a bare
``uv run pytest`` stays green on any machine. Never run in CI.

The ``_skip_reason()`` probe is reused verbatim from ``test_mt5_terminal.py``
rather than imported, so this module collects (and is correctly skipped) on
any platform without depending on another test module's import-time side
effects.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from trading_house.brokers.mt5.boundary import ACCOUNT_TRADE_MODE_DEMO, TerminalPort
from trading_house.brokers.mt5.gateway import Mt5Gateway
from trading_house.constitution.binding import parse_venue_binding
from trading_house.core.clock import SystemClock
from trading_house.marketdata.models import Timeframe

PROBE_SYMBOL = "EURUSD"
PROJECT_ROOT = Path(__file__).resolve().parents[2]

BINDING = parse_venue_binding(
    b"""
venue: mt5
books:
  fx_scalp: {magic_range: [110000, 119999]}
instruments:
  fx.eurusd: {server_symbol: "EURUSD"}
"""
)


def _skip_reason() -> str | None:
    """Why these tests cannot run here, or ``None`` when they can."""

    if sys.platform != "win32":
        return "MetaTrader 5 runs only on Windows"
    try:
        import MetaTrader5 as mt5
    except ImportError:
        return "MetaTrader5 is not installed"
    if not mt5.initialize():
        return f"no MetaTrader 5 terminal: {mt5.last_error()}"
    try:
        account = mt5.account_info()
        if account is None:
            return "a terminal is running but no account is logged in"
        if int(account.trade_mode) != ACCOUNT_TRADE_MODE_DEMO:
            return "refusing to run live tests against a non-demo account"
    finally:
        mt5.shutdown()
    return None


_SKIP = _skip_reason()

pytestmark = [
    pytest.mark.mt5,
    pytest.mark.skipif(_SKIP is not None, reason=_SKIP or ""),
]


def _terminal() -> TerminalPort:
    """Imported here, not at module scope: terminal.py cannot be imported at
    all off Windows, and this module must still collect there to be skipped."""

    from trading_house.brokers.mt5.terminal import Mt5Terminal

    return Mt5Terminal()


def test_a_real_window_of_h1_bars_satisfies_utc_and_ohlc_guarantees() -> None:
    """The whole point of this phase: prove the timestamp and OHLC-ordering
    guarantees against a real broker's numbers, not just synthetic fixtures.

    ``Mt5BrokerAdapter.history`` returns ``Mt5Bar`` -- the boundary DTO, not
    the canonical ``Bar`` -- so this checks the two properties that are true
    at that boundary before ``quality.assess`` and ``Bar`` construction ever
    happen: the timestamp conversion is real UTC, and a real broker's OHLC
    values are never internally contradictory.
    """

    from trading_house.brokers.mt5.adapter import Mt5BrokerAdapter

    end = datetime.now(UTC)
    start = end - timedelta(days=10)

    with Mt5Gateway(_terminal(), clock=SystemClock()) as gateway:
        adapter = Mt5BrokerAdapter(gateway, BINDING, clock=SystemClock())
        bars = adapter.history("fx.eurusd", Timeframe.H1, start, end)

    assert len(bars) > 0, "expected at least one closed H1 bar in the last 10 days"
    for bar in bars:
        assert bar.event_time.tzinfo is not None
        assert bar.event_time.utcoffset() == timedelta(0)
        assert bar.low <= min(bar.open, bar.close) <= max(bar.open, bar.close) <= bar.high
        assert bar.low > 0
