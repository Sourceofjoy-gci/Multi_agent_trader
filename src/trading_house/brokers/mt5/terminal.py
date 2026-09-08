"""The only module in this codebase that imports MetaTrader5.

Everything here is IPC plus type conversion. Logic belongs in the pure modules,
which are covered by tests; this file is omitted from coverage and capped in
size precisely so it cannot become a home for untested behaviour.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

import MetaTrader5 as mt5

from trading_house.brokers.mt5.boundary import (
    Mt5Bar,
    Mt5CheckResult,
    Mt5Deal,
    Mt5Position,
    Mt5SendResult,
    Mt5SymbolInfo,
    Mt5Tick,
    establish_utc_offset,
    mt5_timeframe_code,
    server_time_to_utc,
)


class Mt5Terminal:
    """A thin, typed shell over the MetaTrader 5 IPC surface."""

    def __init__(self, probe_symbol: str = "EURUSD") -> None:
        # Which symbol to read the server clock from. Brokers that suffix
        # their symbols (EURUSD.m, EURUSD_i) have no plain "EURUSD", and a
        # hardcoded one makes the clock probe fail on them.
        self._probe_symbol = probe_symbol
        self._offset: int | None = None

    def initialize(self) -> bool:
        return bool(mt5.initialize())

    def shutdown(self) -> None:
        mt5.shutdown()

    def terminal_connected(self) -> bool:
        """Whether the terminal has a live link to the trade server.

        ``last_error()`` answers a different question -- it reports this
        library's last in-process error and succeeds whether or not the
        terminal can reach the broker.
        """

        info = mt5.terminal_info()
        return False if info is None else bool(info.connected)

    def account_trade_mode(self) -> int:
        account = mt5.account_info()
        return -1 if account is None else int(account.trade_mode)

    def autotrading_enabled(self) -> bool:
        # terminal_info().trade_allowed is the AutoTrading toolbar toggle;
        # account_info().trade_allowed is a different, account-level
        # permission that reads True even while AutoTrading is off, and
        # would not have caught the condition this method exists for.
        info = mt5.terminal_info()
        return False if info is None else bool(info.trade_allowed)

    def server_utc_offset_seconds(self) -> int:
        offset = establish_utc_offset(
            self._probe_tick_time, lambda: datetime.now(UTC).timestamp(), time.sleep
        )
        self._offset = offset
        return offset

    def _probe_tick_time(self) -> float | None:
        mt5.symbol_select(self._probe_symbol, True)
        tick = mt5.symbol_info_tick(self._probe_symbol)
        return None if tick is None else float(tick.time)

    def _to_utc(self, server_epoch: float) -> datetime:
        return server_time_to_utc(float(server_epoch), self._offset)

    def symbol_info(self, server_symbol: str) -> Mt5SymbolInfo | None:
        mt5.symbol_select(server_symbol, True)
        info = mt5.symbol_info(server_symbol)
        if info is None:
            return None
        return Mt5SymbolInfo(
            name=info.name,
            digits=int(info.digits),
            point=float(info.point),
            trade_tick_size=float(info.trade_tick_size),
            trade_tick_value_loss=float(info.trade_tick_value_loss),
            volume_min=float(info.volume_min),
            volume_step=float(info.volume_step),
            volume_max=float(info.volume_max),
            trade_stops_level=int(info.trade_stops_level),
            trade_freeze_level=int(info.trade_freeze_level),
            trade_mode=int(info.trade_mode),
            trade_exemode=int(info.trade_exemode),
            filling_mode=int(info.filling_mode),
            currency_base=info.currency_base,
            currency_profit=info.currency_profit,
        )

    def symbol_tick(self, server_symbol: str) -> Mt5Tick | None:
        # MetaTrader 5 serves ticks only for symbols in Market Watch. Without
        # this, describe_instrument works while snapshot silently drops the
        # same symbol, indistinguishable from a genuinely tickless one.
        mt5.symbol_select(server_symbol, True)
        tick = mt5.symbol_info_tick(server_symbol)
        if tick is None:
            return None
        return Mt5Tick(
            bid=float(tick.bid), ask=float(tick.ask), observed_at=self._to_utc(tick.time)
        )

    def copy_rates_range(
        self, server_symbol: str, timeframe_minutes: int, start: datetime, end: datetime
    ) -> Sequence[Mt5Bar] | None:
        # Same reason as symbol_tick: MT5 serves history only for symbols in
        # Market Watch.
        mt5.symbol_select(server_symbol, True)
        timeframe_code = mt5_timeframe_code(timeframe_minutes)
        rates = mt5.copy_rates_range(server_symbol, timeframe_code, start, end)
        if rates is None:
            return None
        return tuple(
            Mt5Bar(
                event_time=self._to_utc(row[0]),
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
                tick_volume=int(row[5]),
                spread=int(row[6]),
                real_volume=int(row[7]),
            )
            for row in rates
        )

    def positions(self) -> Sequence[Mt5Position]:
        raw = mt5.positions_get()
        if raw is None:
            return ()
        return tuple(
            Mt5Position(
                ticket=int(p.ticket),
                magic=int(p.magic),
                server_symbol=p.symbol,
                volume=float(p.volume),
                price_open=float(p.price_open),
                sl=float(p.sl),
                tp=float(p.tp) or None,
                is_buy=int(p.type) == 0,
                opened_at=self._to_utc(p.time),
            )
            for p in raw
        )

    def order_check(self, request: Mapping[str, object]) -> Mt5CheckResult | None:
        result = mt5.order_check(dict(request))
        if result is None:
            return None
        return Mt5CheckResult(retcode=int(result.retcode), comment=str(result.comment))

    def send_order(self, request: Mapping[str, object]) -> Mt5SendResult | None:
        result = mt5.order_send(dict(request))
        if result is None:
            return None
        return Mt5SendResult(
            retcode=int(result.retcode),
            order_ticket=int(result.order) or None,
            position_ticket=int(getattr(result, "position", 0)) or None,
            deal_ticket=int(result.deal) or None,
            volume=float(result.volume),
            price=float(result.price),
            comment=str(result.comment),
        )

    def history_deals(self, start: datetime, end: datetime) -> Sequence[Mt5Deal]:
        raw = mt5.history_deals_get(start, end)
        if raw is None:
            return ()
        return tuple(
            Mt5Deal(
                ticket=int(d.ticket),
                order_ticket=int(d.order),
                position_ticket=int(d.position_id),
                magic=int(d.magic),
                server_symbol=d.symbol,
                volume=float(d.volume),
                price=float(d.price),
                is_buy=int(d.type) == 0,
                dealt_at=self._to_utc(d.time),
            )
            for d in raw
        )

    def last_error(self) -> tuple[int, str]:
        code, description = mt5.last_error()
        return int(code), str(description)
