"""What the gateway needs from a terminal, expressed without importing MetaTrader5.

These DTOs mirror the fields MetaTrader 5 returns, as plain frozen dataclasses,
so every module except ``terminal.py`` stays importable on Linux and testable
without a broker. The integer constants mirror MetaTrader 5's own enums for the
same reason.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable

from trading_house.core.errors import BrokerUnavailableError

SYMBOL_TRADE_MODE_DISABLED = 0
SYMBOL_TRADE_MODE_LONGONLY = 1
SYMBOL_TRADE_MODE_SHORTONLY = 2
SYMBOL_TRADE_MODE_CLOSEONLY = 3
SYMBOL_TRADE_MODE_FULL = 4

SYMBOL_FILLING_FOK = 1
SYMBOL_FILLING_IOC = 2

SYMBOL_TRADE_EXECUTION_REQUEST = 0
SYMBOL_TRADE_EXECUTION_INSTANT = 1
SYMBOL_TRADE_EXECUTION_MARKET = 2
SYMBOL_TRADE_EXECUTION_EXCHANGE = 3

ACCOUNT_TRADE_MODE_DEMO = 0
ACCOUNT_TRADE_MODE_CONTEST = 1
ACCOUNT_TRADE_MODE_REAL = 2


MAX_PLAUSIBLE_OFFSET_SECONDS = 14 * 3600
OFFSET_QUANTUM_SECONDS = 1800
MAX_TICK_STALENESS_SECONDS = 60


def utc_offset_seconds(server_epoch: float, real_utc_epoch: float) -> int:
    """How far the broker's clock runs ahead of UTC, in whole seconds.

    MetaTrader 5 reports every timestamp in the broker server's own timezone,
    with no indication of what that timezone is. The only way to recover UTC is
    to compare a server clock reading against a real one taken at the same
    moment -- but that reading cannot simply be trusted.

    The only view of the server clock MetaTrader 5 offers is the timestamp of
    the *last* tick, which is current while a market is open and days old once
    it closes. A tick from Friday's close, probed on a Sunday, yields an offset
    near -48h, and every timestamp in the system would then be shifted by two
    days with nothing to reveal it.

    Real broker offsets are whole half-hours within a day of UTC, so a fresh
    tick lands within seconds of one. A reading that does not is a closed
    market being mistaken for a timezone, and it is refused.
    """

    raw = server_epoch - real_utc_epoch
    quantised = round(raw / OFFSET_QUANTUM_SECONDS) * OFFSET_QUANTUM_SECONDS
    if abs(quantised) > MAX_PLAUSIBLE_OFFSET_SECONDS:
        raise BrokerUnavailableError
    if abs(raw - quantised) > MAX_TICK_STALENESS_SECONDS:
        raise BrokerUnavailableError
    return int(quantised)


def server_time_to_utc(server_epoch: float, offset_seconds: int | None) -> datetime:
    """Convert a broker-server epoch to a timezone-aware UTC instant (I-10).

    ``offset_seconds`` is ``None`` until the server clock has been probed. That
    case raises rather than assuming zero: a terminal that has not established
    the offset would otherwise pass broker-local time off as UTC, silently, and
    every timestamp it recorded would be wrong by the broker's timezone.
    """

    if offset_seconds is None:
        raise BrokerUnavailableError
    return datetime.fromtimestamp(server_epoch, UTC) - timedelta(seconds=offset_seconds)


@dataclass(frozen=True, slots=True)
class Mt5SymbolInfo:
    """The subset of ``symbol_info()`` that determines tradability and sizing."""

    name: str
    digits: int
    point: float
    trade_tick_size: float
    trade_tick_value_loss: float
    volume_min: float
    volume_step: float
    volume_max: float
    trade_stops_level: int
    trade_freeze_level: int
    trade_mode: int
    trade_exemode: int
    filling_mode: int
    currency_base: str
    currency_profit: str


@dataclass(frozen=True, slots=True)
class Mt5Tick:
    bid: float
    ask: float
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class Mt5Position:
    ticket: int
    magic: int
    server_symbol: str
    volume: float
    price_open: float
    sl: float
    tp: float | None
    is_buy: bool
    opened_at: datetime


@dataclass(frozen=True, slots=True)
class Mt5CheckResult:
    retcode: int
    comment: str


@runtime_checkable
class TerminalPort(Protocol):
    """Every MetaTrader 5 call the gateway makes. Nothing wider."""

    def initialize(self) -> bool: ...
    def shutdown(self) -> None: ...
    def account_trade_mode(self) -> int: ...
    def terminal_connected(self) -> bool: ...
    def server_utc_offset_seconds(self) -> int: ...
    def symbol_info(self, server_symbol: str) -> Mt5SymbolInfo | None: ...
    def symbol_tick(self, server_symbol: str) -> Mt5Tick | None: ...
    def positions(self) -> Sequence[Mt5Position]: ...
    def order_check(self, request: Mapping[str, object]) -> Mt5CheckResult | None: ...
    def last_error(self) -> tuple[int, str]: ...
