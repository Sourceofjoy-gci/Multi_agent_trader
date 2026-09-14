"""Shared MT5 test doubles.

``FakeTerminal`` lives here rather than in any one test module because pytest
runs this tree under ``--import-mode=importlib`` with no ``__init__.py``
anywhere underneath ``tests/`` -- importing a class out of a sibling test
module by its bare module name is unreliable under that mode. A conftest is
always discovered by pytest itself, and its symbols can be reached with an
absolute, rootdir-relative import (``tests.unit.brokers.mt5.conftest``) from
any test module that needs them.
"""

import threading
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

import pytest

from trading_house.brokers.mt5.boundary import (
    ACCOUNT_TRADE_MODE_DEMO,
    Mt5Bar,
    Mt5CheckResult,
    Mt5Deal,
    Mt5Position,
    Mt5SendResult,
    Mt5SymbolInfo,
    Mt5Tick,
)

_OPENED_AT = datetime(2026, 8, 25, tzinfo=UTC)


class FakeTerminal:
    """A TerminalPort that needs no MetaTrader 5 and no Windows."""

    def __init__(
        self,
        *,
        trade_mode: int = ACCOUNT_TRADE_MODE_DEMO,
        initialises: bool = True,
        send_result: Mt5SendResult | None = None,
        deals: Sequence[Mt5Deal] = (),
        positions: Sequence[Mt5Position] = (),
    ) -> None:
        self.trade_mode = trade_mode
        self.initialises = initialises
        self.shutdown_calls = 0
        self.gate = threading.Event()
        self.gate.set()
        self.rate_requests: list[tuple[str, int, datetime, datetime]] = []
        # send_result and deals are plain mutable attributes, not just
        # constructor kwargs, so a test can configure them on an already
        # -built fixture instance (e.g. ``symbol_terminal``) instead of
        # subclassing just to thread one value through __init__.
        self.send_result = send_result
        self.deals = tuple(deals)
        self._positions = tuple(positions)
        self.sent: list[Mapping[str, object]] = []

    def initialize(self) -> bool:
        return self.initialises

    def shutdown(self) -> None:
        self.shutdown_calls += 1

    def account_trade_mode(self) -> int:
        return self.trade_mode

    def terminal_connected(self) -> bool:
        return True

    def server_utc_offset_seconds(self) -> int:
        return 0

    def symbol_info(self, server_symbol: str) -> Mt5SymbolInfo | None:
        self.gate.wait(5)
        return None

    def symbol_tick(self, server_symbol: str) -> Mt5Tick | None:
        return Mt5Tick(bid=1.1, ask=1.2, observed_at=_OPENED_AT)

    def copy_rates_range(
        self, server_symbol: str, timeframe_minutes: int, start: datetime, end: datetime
    ) -> tuple[Mt5Bar, ...]:
        self.rate_requests.append((server_symbol, timeframe_minutes, start, end))
        return ()

    def positions(self) -> Sequence[Mt5Position]:
        return self._positions

    def order_check(self, request: Mapping[str, object]) -> Mt5CheckResult | None:
        return Mt5CheckResult(retcode=0, comment="Done")

    def send_order(self, request: Mapping[str, object]) -> Mt5SendResult | None:
        self.sent.append(request)
        return self.send_result

    def history_deals(self, start: datetime, end: datetime) -> Sequence[Mt5Deal]:
        return self.deals

    def last_error(self) -> tuple[int, str]:
        return 0, "ok"


@pytest.fixture
def symbol_terminal() -> FakeTerminal:
    """A terminal that knows about ``EURUSD`` symbol metadata."""

    class _SymbolTerminal(FakeTerminal):
        def symbol_info(self, server_symbol: str) -> Mt5SymbolInfo | None:
            return Mt5SymbolInfo(
                name=server_symbol,
                digits=5,
                point=0.00001,
                trade_tick_size=0.00001,
                trade_tick_value_loss=1.0,
                volume_min=0.01,
                volume_step=0.01,
                volume_max=100.0,
                trade_stops_level=0,
                trade_freeze_level=0,
                trade_mode=4,  # SYMBOL_TRADE_MODE_FULL
                trade_exemode=2,  # SYMBOL_TRADE_EXECUTION_MARKET
                filling_mode=3,  # SYMBOL_FILLING_FOK | SYMBOL_FILLING_IOC
                currency_base="EUR",
                currency_profit="USD",
            )

    return _SymbolTerminal()


@pytest.fixture
def tickless_terminal() -> FakeTerminal:
    """A terminal with no current tick for any symbol."""

    class _TicklessTerminal(FakeTerminal):
        def symbol_tick(self, server_symbol: str) -> Mt5Tick | None:
            return None

    return _TicklessTerminal()


@pytest.fixture
def position_terminal() -> FakeTerminal:
    """One position inside ``fx_scalp``'s magic range, one in no declared range."""

    class _PositionTerminal(FakeTerminal):
        def positions(self) -> Sequence[Mt5Position]:
            return (
                Mt5Position(
                    ticket=1001,
                    magic=110042,
                    server_symbol="EURUSD",
                    volume=0.1,
                    price_open=1.1000,
                    sl=1.0950,
                    tp=1.1100,
                    is_buy=True,
                    opened_at=_OPENED_AT,
                ),
                Mt5Position(
                    ticket=1002,
                    magic=999999,
                    server_symbol="EURUSD",
                    volume=0.2,
                    price_open=1.2000,
                    sl=1.2050,
                    tp=None,
                    is_buy=False,
                    opened_at=_OPENED_AT,
                ),
            )

    return _PositionTerminal()
