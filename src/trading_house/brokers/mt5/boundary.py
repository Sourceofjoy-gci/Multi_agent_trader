"""What the gateway needs from a terminal, expressed without importing MetaTrader5.

These DTOs mirror the fields MetaTrader 5 returns, as plain frozen dataclasses,
so every module except ``terminal.py`` stays importable on Linux and testable
without a broker. The integer constants mirror MetaTrader 5's own enums for the
same reason.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Protocol, runtime_checkable

from trading_house.core.errors import BrokerUnavailableError, ConfigurationError
from trading_house.core.venue import DealEntry

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

# The position guard's one action: modify a live position's SL/TP in place.
# Mirrored here, not in adapter.py, so the request it builds -- position
# ticket required, sl/tp coerced to float -- lives next to the position
# shape it reads, per the same reasoning as every other constant in this
# module (see the module docstring).
TRADE_ACTION_SLTP = 6  # MetaTrader5.TRADE_ACTION_SLTP


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


def establish_utc_offset(
    sample_server_epoch: Callable[[], float | None],
    real_utc_epoch: Callable[[], float],
    sleep: Callable[[float], None],
    *,
    max_attempts: int = 20,
    interval_seconds: float = 1.0,
) -> int:
    """Establish the broker clock offset from a demonstrably live feed.

    Quantising a single reading is not enough on its own. A lag near any
    multiple of the half-hour quantum survives both the range and residual
    checks and yields a wrong but entirely plausible offset -- a Saturday
    probe of a Friday-evening tick can read a UTC+3 broker as UTC-7, and
    nothing downstream could tell.

    The only thing that actually separates a timezone from a closed market is
    whether the clock moves. A stale tick's timestamp is frozen; a live one
    advances. So sample until it does, and refuse if it never does: the
    offset cannot be known while the market is shut, and refusing is the only
    honest answer.
    """

    # The window must exceed the feed's inter-tick gap, not just be "a few
    # seconds": a quiet demo feed was measured ticking about once per five
    # seconds on EURUSD mid-session, which a 5s window rejects as stale.
    first = sample_server_epoch()
    if first is None:
        raise BrokerUnavailableError
    for _ in range(max_attempts):
        sleep(interval_seconds)
        latest = sample_server_epoch()
        if latest is None:
            raise BrokerUnavailableError
        if latest > first:
            # The feed is live, so this reading is at most an interval old.
            return utc_offset_seconds(latest, real_utc_epoch())
    raise BrokerUnavailableError


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
class Mt5Bar:
    """One closed bar, with its timestamp already converted to UTC."""

    event_time: datetime
    open: float
    high: float
    low: float
    close: float
    tick_volume: int
    spread: int
    real_volume: int


# MetaTrader 5's timeframe constants are the minute count only below H1; at and
# above H1 they are bit-flagged, so the minute value cannot be passed straight
# through. This table is the whole of the translation, kept in the boundary so
# terminal.py need not carry the mapping's own logic.
MT5_TIMEFRAME_CODES: dict[int, int] = {
    1: 1,  # M1
    5: 5,  # M5
    15: 15,  # M15
    60: 16385,  # H1  = TIMEFRAME_H1
    240: 16388,  # H4 = TIMEFRAME_H4
    1440: 16408,  # D1 = TIMEFRAME_D1
}


def mt5_timeframe_code(timeframe_minutes: int) -> int:
    """Translate a bar length in minutes into MetaTrader 5's constant.

    The constants are the minute count only below H1; above that they are
    bit-flagged (H1 is 16385, not 60), which is why this table exists rather
    than the value passing straight through. An unmapped length is a caller
    bug, not a broker condition, so it fails as a configuration error rather
    than as a bare KeyError crossing the gateway's actor thread.
    """

    code = MT5_TIMEFRAME_CODES.get(timeframe_minutes)
    if code is None:
        raise ConfigurationError
    return code


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


def sltp_request(
    server_symbol: str, position_ticket: int, stop_loss: Decimal, take_profit: Decimal | None
) -> dict[str, object]:
    """Build a ``TRADE_ACTION_SLTP`` request to move a live position's stop.

    Three documented MT5 traps, all encoded here rather than left for a
    caller to rediscover: ``position`` is required -- ``TRADE_ACTION_SLTP``
    without it modifies nothing at all, silently; ``sl``/``tp`` must be
    genuine floats, since MT5 returns ``None`` with no useful error when
    either arrives as an int; and ``tp=0.0`` means *remove the take-profit*,
    not *no take-profit given* -- there is no third value that means "leave
    it alone". ``take_profit=None`` here therefore always resolves to the
    erase value. It is the caller's job (``Mt5BrokerAdapter.amend_protection``)
    to have already substituted the position's current TP before calling
    this, when that is what "leave it alone" actually requires -- this
    function does not read the position and cannot make that distinction
    itself.
    """

    return {
        "action": TRADE_ACTION_SLTP,
        "symbol": server_symbol,
        "position": int(position_ticket),
        "sl": float(stop_loss),
        "tp": float(take_profit) if take_profit is not None else 0.0,
    }


@dataclass(frozen=True, slots=True)
class Mt5CheckResult:
    retcode: int
    comment: str


@dataclass(frozen=True, slots=True)
class Mt5SendResult:
    retcode: int
    order_ticket: int | None
    position_ticket: int | None
    deal_ticket: int | None
    volume: float
    price: float
    comment: str


@dataclass(frozen=True, slots=True)
class Mt5Deal:
    ticket: int
    order_ticket: int
    position_ticket: int
    magic: int
    server_symbol: str
    volume: float
    price: float
    is_buy: bool
    dealt_at: datetime
    entry: int


# MetaTrader 5's own DEAL_ENTRY_* constants, mirrored so this module never
# needs to import MetaTrader5. DEAL_ENTRY_OUT_BY (a close against an opposing
# position) maps to OUT: it is still a close, and treating it as an open
# would leave a closed position on the books forever.
_DEAL_ENTRY = {0: DealEntry.IN, 1: DealEntry.OUT, 2: DealEntry.INOUT, 3: DealEntry.OUT}


def deal_entry_of(raw: int) -> DealEntry:
    """Map MT5's entry code. An unknown code raises rather than defaulting."""

    entry = _DEAL_ENTRY.get(raw)
    if entry is None:
        raise ConfigurationError()
    return entry


@runtime_checkable
class TerminalPort(Protocol):
    """Every MetaTrader 5 call the gateway makes. Nothing wider."""

    def initialize(self) -> bool: ...
    def shutdown(self) -> None: ...
    def account_trade_mode(self) -> int: ...
    def autotrading_enabled(self) -> bool: ...
    def terminal_connected(self) -> bool: ...
    def server_utc_offset_seconds(self) -> int: ...
    def symbol_info(self, server_symbol: str) -> Mt5SymbolInfo | None: ...
    def symbol_tick(self, server_symbol: str) -> Mt5Tick | None: ...
    def copy_rates_range(
        self, server_symbol: str, timeframe_minutes: int, start: datetime, end: datetime
    ) -> Sequence[Mt5Bar] | None: ...
    # Both of these return None for "could not read", never for "nothing
    # there": absence of evidence is not evidence of absence when what is
    # at stake is whether a live position exists.
    def positions(self) -> Sequence[Mt5Position] | None: ...
    def order_check(self, request: Mapping[str, object]) -> Mt5CheckResult | None: ...
    def send_order(self, request: Mapping[str, object]) -> Mt5SendResult | None: ...
    def history_deals(self, start: datetime, end: datetime) -> Sequence[Mt5Deal] | None: ...
    def last_error(self) -> tuple[int, str]: ...
