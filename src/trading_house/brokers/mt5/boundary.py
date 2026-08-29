"""What the gateway needs from a terminal, expressed without importing MetaTrader5.

These DTOs mirror the fields MetaTrader 5 returns, as plain frozen dataclasses,
so every module except ``terminal.py`` stays importable on Linux and testable
without a broker. The integer constants mirror MetaTrader 5's own enums for the
same reason.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

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
    def server_utc_offset_seconds(self) -> int: ...
    def symbol_info(self, server_symbol: str) -> Mt5SymbolInfo | None: ...
    def symbol_tick(self, server_symbol: str) -> Mt5Tick | None: ...
    def positions(self) -> Sequence[Mt5Position]: ...
    def order_check(self, request: Mapping[str, object]) -> Mt5CheckResult | None: ...
    def last_error(self) -> tuple[int, str]: ...
