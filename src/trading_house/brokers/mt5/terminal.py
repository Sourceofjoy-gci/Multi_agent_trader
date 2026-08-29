# src/trading_house/brokers/mt5/terminal.py
"""The only module in this codebase that imports MetaTrader5.

Everything here is IPC plus type conversion. Logic belongs in the pure modules,
which are covered by tests; this file is omitted from coverage and capped in
size precisely so it cannot become a home for untested behaviour.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta

import MetaTrader5 as mt5

from trading_house.brokers.mt5.boundary import (
    Mt5CheckResult,
    Mt5Position,
    Mt5SymbolInfo,
    Mt5Tick,
)


class Mt5Terminal:
    """A thin, typed shell over the MetaTrader 5 IPC surface."""

    def __init__(self) -> None:
        self._offset = timedelta(0)

    def initialize(self) -> bool:
        return bool(mt5.initialize())

    def shutdown(self) -> None:
        mt5.shutdown()

    def account_trade_mode(self) -> int:
        account = mt5.account_info()
        return -1 if account is None else int(account.trade_mode)

    def server_utc_offset_seconds(self) -> int:
        tick = mt5.symbol_info_tick("EURUSD")
        if tick is None:
            return 0
        offset = round(float(tick.time) - datetime.now(UTC).timestamp())
        self._offset = timedelta(seconds=offset)
        return offset

    def _to_utc(self, server_epoch: float) -> datetime:
        return datetime.fromtimestamp(float(server_epoch), UTC) - self._offset

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
        tick = mt5.symbol_info_tick(server_symbol)
        if tick is None:
            return None
        return Mt5Tick(
            bid=float(tick.bid), ask=float(tick.ask), observed_at=self._to_utc(tick.time)
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

    def last_error(self) -> tuple[int, str]:
        code, description = mt5.last_error()
        return int(code), str(description)
