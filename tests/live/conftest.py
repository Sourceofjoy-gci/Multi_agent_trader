"""Shared fixtures and utilities for live MetaTrader 5 tests."""

import sys
import time

ACCOUNT_TRADE_MODE_DEMO = 1


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

        # Check if the market is open by sampling the broker clock over 2 seconds
        tick1 = mt5.symbol_info_tick("EURUSD")
        if tick1 is None:
            return "EURUSD tick data unavailable"
        first_time = tick1.time_msc

        time.sleep(2)

        tick2 = mt5.symbol_info_tick("EURUSD")
        if tick2 is None:
            return "EURUSD tick data unavailable"
        second_time = tick2.time_msc

        if first_time == second_time:
            return "the market is closed: the broker clock is not advancing"
    finally:
        mt5.shutdown()
    return None
