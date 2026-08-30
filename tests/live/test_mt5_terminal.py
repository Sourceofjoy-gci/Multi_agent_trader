"""Tests that need a running MetaTrader 5 terminal on a DEMO account.

Marked ``mt5`` and skipped when no suitable terminal is present, so a bare
``uv run pytest`` stays green on any machine. Never run in CI.

The availability probe checks the account is a demo before reporting the
terminal usable. That is stricter than it needs to be for safety -- the
gateway refuses a non-demo account in ``start()`` regardless -- but it means
these tests never so much as open a session against a live account, and the
skip reason says which condition was not met rather than a bare "no
terminal".
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from trading_house.brokers.mt5.boundary import ACCOUNT_TRADE_MODE_DEMO, TerminalPort
from trading_house.brokers.mt5.contracts import to_instrument_contract
from trading_house.brokers.mt5.gateway import Mt5Gateway, Priority
from trading_house.core.clock import SystemClock

PROBE_SYMBOL = "EURUSD"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG = PROJECT_ROOT / "config"


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


def test_gateway_starts_against_a_demo_account() -> None:
    with Mt5Gateway(_terminal(), clock=SystemClock()) as gateway:
        tick = gateway.call(Priority.MARKET_DATA, lambda t: t.symbol_tick(PROBE_SYMBOL))

    assert tick is not None, f"the terminal has no tick for {PROBE_SYMBOL}"
    assert tick.ask >= tick.bid
    assert tick.observed_at.tzinfo is not None


def test_a_real_symbol_maps_to_a_valid_contract() -> None:
    """The whole point of this phase: prove the translation against a real
    broker's numbers rather than against our own assumptions about them."""

    with Mt5Gateway(_terminal(), clock=SystemClock()) as gateway:
        info = gateway.call(Priority.MARKET_DATA, lambda t: t.symbol_info(PROBE_SYMBOL))

    assert info is not None, f"the terminal does not know the symbol {PROBE_SYMBOL}"
    contract = to_instrument_contract(info, instrument_id="fx.eurusd")
    assert contract.price_increment > 0
    assert contract.min_stop_distance >= 0
    assert len(contract.supported_fills) >= 1


def test_server_time_offset_is_computed_and_timestamps_land_in_utc() -> None:
    """A broker running on its own timezone is the normal case, so an offset
    of zero proves nothing on its own -- what matters is that the converted
    timestamp is close to real UTC now."""

    from datetime import UTC, datetime

    with Mt5Gateway(_terminal(), clock=SystemClock()) as gateway:
        offset = gateway.server_utc_offset_seconds
        tick = gateway.call(Priority.MARKET_DATA, lambda t: t.symbol_tick(PROBE_SYMBOL))

    assert isinstance(offset, int)
    assert tick is not None
    drift = abs((datetime.now(UTC) - tick.observed_at).total_seconds())
    assert drift < 3600, f"converted tick time is {drift}s from UTC now; offset was {offset}"


def test_reconciliation_reports_without_claiming_attribution() -> None:
    """Phase 1 has no intent ledger, so every position a demo account holds
    must arrive unmatched rather than as an invented PositionState."""

    from trading_house.brokers.mt5.adapter import Mt5BrokerAdapter
    from trading_house.constitution.binding import load_venue_binding
    from trading_house.core.clock import SystemClock as Clock

    binding = load_venue_binding(
        CONFIG / "venue_binding.mt5.yaml",
        CONFIG / "venue_binding.mt5.yaml.sig",
        CONFIG / "risk_constitution.public.pem",
    )

    with Mt5Gateway(_terminal(), clock=SystemClock()) as gateway:
        adapter = Mt5BrokerAdapter(gateway, binding, clock=Clock())
        report = adapter.reconcile(next(iter(binding.books)))

    assert report.positions == ()
