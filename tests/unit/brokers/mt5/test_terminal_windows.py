"""terminal.py sends every time window to MetaTrader 5 in the broker's frame.

MetaTrader 5 compares a passed datetime's epoch against its own server-frame
epochs, for bars and deals alike. Measured on FBS-Demo (UTC+3), 2026-10-04:
an unshifted UTC window around a deal's true time found none of five sampled
deals, the same window shifted by the offset found all five. An unshifted
window fetches the history of ``offset`` hours earlier, which ingest would
then refuse (migration 0009) and the reconciler would read as missing deals.

terminal.py is the one module that imports MetaTrader5, and it cannot be
imported where MetaTrader5 is absent, so the live tests are its only other
check and CI never runs them. Here the real module is loaded against a
stand-in ``MetaTrader5`` that records what it was asked, under a private name
so the real ``trading_house.brokers.mt5.terminal`` is never replaced.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pytest

OFFSET = 3 * 3600  # Europe/Athens in summer, which START falls in
START = datetime(2026, 9, 30, 10, 0, tzinfo=UTC)
END = START + timedelta(hours=2)
_TICKS = np.array(
    [(0, 1.1, 1.2, 0.0, 0, 1000, 6, 0.0)],
    dtype=[
        ("time", "<i8"),
        ("bid", "<f8"),
        ("ask", "<f8"),
        ("last", "<f8"),
        ("volume", "<u8"),
        ("time_msc", "<i8"),
        ("flags", "<u4"),
        ("volume_real", "<f8"),
    ],
)
TERMINAL_PY = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "trading_house"
    / "brokers"
    / "mt5"
    / "terminal.py"
)


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, list[tuple[str, datetime, datetime]]]:
    """A terminal on an EU-rules server clock, and the windows it sends."""

    calls: list[tuple[str, datetime, datetime]] = []
    fake = types.ModuleType("MetaTrader5")
    fake.symbol_select = lambda *_: True  # type: ignore[attr-defined]
    fake.copy_rates_range = lambda _s, _tf, start, end: calls.append(("rates", start, end)) or []  # type: ignore[attr-defined]
    fake.history_deals_get = lambda start, end: calls.append(("deals", start, end)) or []  # type: ignore[attr-defined]
    fake.COPY_TICKS_ALL = -1  # type: ignore[attr-defined]
    fake.copy_ticks_range = lambda _s, start, end, _f: calls.append(("ticks", start, end)) or _TICKS  # type: ignore[attr-defined]
    fake.error = (1, "Success")  # type: ignore[attr-defined]
    fake.last_error = lambda: fake.error  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "MetaTrader5", fake)

    spec = importlib.util.spec_from_file_location("_terminal_under_test", TERMINAL_PY)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    terminal = module.Mt5Terminal(ZoneInfo("Europe/Athens"))
    return terminal, calls


def test_a_bar_window_is_sent_in_the_brokers_frame(sent: tuple[Any, list[Any]]) -> None:
    terminal, calls = sent

    terminal.copy_rates_range("EURUSD", 15, START, END)

    assert [(kind, s.timestamp(), e.timestamp()) for kind, s, e in calls] == [
        (
            "rates",
            (START + timedelta(seconds=OFFSET)).timestamp(),
            (END + timedelta(seconds=OFFSET)).timestamp(),
        )
    ]


def test_a_deal_window_is_sent_in_the_brokers_frame(sent: tuple[Any, list[Any]]) -> None:
    terminal, calls = sent

    terminal.history_deals(START, END)

    assert [(kind, s.timestamp(), e.timestamp()) for kind, s, e in calls] == [
        (
            "deals",
            (START + timedelta(seconds=OFFSET)).timestamp(),
            (END + timedelta(seconds=OFFSET)).timestamp(),
        )
    ]


def test_a_tick_window_is_sent_in_the_brokers_frame(sent: tuple[Any, list[Any]]) -> None:
    terminal, calls = sent

    batch = terminal.copy_ticks_range("EURUSD", START, END)

    assert batch is not None
    assert batch.time_msc.tolist() == [1000]
    assert [(kind, s.timestamp(), e.timestamp()) for kind, s, e in calls] == [
        (
            "ticks",
            (START + timedelta(seconds=OFFSET)).timestamp(),
            (END + timedelta(seconds=OFFSET)).timestamp(),
        )
    ]


def test_ticks_returned_with_an_error_set_are_a_failed_call(sent: tuple[Any, list[Any]]) -> None:
    """A range the terminal is still downloading can come back partial with an
    error set; storing it would record a short day as COMPLETE."""

    terminal, _calls = sent
    sys.modules["MetaTrader5"].error = (-1, "Terminal: Call failed")  # type: ignore[attr-defined]

    assert terminal.copy_ticks_range("EURUSD", START, END) is None
