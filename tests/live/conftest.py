"""Shared fixtures and utilities for live MetaTrader 5 tests."""

import sys
import time
from collections.abc import Callable

from trading_house.brokers.mt5.boundary import ACCOUNT_TRADE_MODE_DEMO

LIVENESS_WINDOW_SECONDS = 20


def clock_advances(sample: Callable[[], float | None]) -> bool:
    """Whether the broker's clock moves within ``LIVENESS_WINDOW_SECONDS``.

    Returns as soon as it does. A fixed two-second pair skipped live markets:
    this demo feed was measured ticking about once per five seconds, and on
    2026-10-04 twenty runs in a row skipped after the open. The window is the
    one ``establish_utc_offset`` uses for the same question. ``None`` from
    ``sample`` means no tick at all, which is not live.
    """

    first = sample()
    if first is None:
        return False
    for _ in range(LIVENESS_WINDOW_SECONDS):
        time.sleep(1)
        latest = sample()
        if latest is None:
            return False
        if latest > first:
            return True
    return False


def skip_reason() -> str | None:
    """Why these tests cannot run here, or None when they can.

    Checks platform, MT5 installation, terminal availability, demo account status,
    and whether the market is currently open (clock is advancing).
    """

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

        if mt5.symbol_info_tick("EURUSD") is None:
            return "EURUSD tick data unavailable"

        def tick_time() -> float | None:
            tick = mt5.symbol_info_tick("EURUSD")
            return None if tick is None else float(tick.time_msc)

        if not clock_advances(tick_time):
            return "the market is closed: the broker clock is not advancing"
    finally:
        mt5.shutdown()
    return None
