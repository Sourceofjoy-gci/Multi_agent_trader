"""Dump every broker capability that decides which strategies are implementable.

Read-only. Connects to a DEMO terminal, reads symbol and account metadata,
and writes a Markdown report. Sends no orders and changes nothing.

Audits the symbols the signed venue binding actually names, unless symbols
are given explicitly.

Usage:  uv run python scripts/broker_audit.py > docs/broker-audit.md
"""

from __future__ import annotations

import statistics
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, fields
from pathlib import Path

from trading_house.brokers.mt5.boundary import ACCOUNT_TRADE_MODE_DEMO
from trading_house.constitution.binding import load_venue_binding

SPREAD_SAMPLES = 30
SPREAD_INTERVAL_SECONDS = 0.2
CONFIG = Path("config")


@dataclass(frozen=True, slots=True)
class AuditRow:
    server_symbol: str
    trade_mode: int
    trade_exemode: int
    filling_mode: int
    trade_stops_level: int
    trade_freeze_level: int
    volume_min: float
    volume_step: float
    volume_max: float
    trade_tick_value_loss: float
    trade_tick_size: float
    digits: int
    swap_long: float
    swap_short: float
    median_spread_points: float


def format_audit_report(rows: Sequence[AuditRow]) -> str:
    """Render the audit as Markdown, flagging values that constrain the design."""

    lines = ["# Broker capability audit", ""]
    for row in rows:
        lines.append(f"## {row.server_symbol}")
        lines.append("")
        for field in fields(row):
            if field.name == "server_symbol":
                continue
            lines.append(f"- `{field.name}`: {getattr(row, field.name)}")
        notes: list[str] = []
        if row.trade_stops_level == 0:
            notes.append("zero stops_level: min_stop_distance floors to one price increment")
        if row.trade_mode != 4:
            notes.append(f"trade_mode is {row.trade_mode}, not FULL — restricted trading")
        if row.filling_mode == 0:
            notes.append("no FOK/IOC filling bits — RETURN only")
        if notes:
            lines.append("")
            lines.append("**Notes:**")
            lines.extend(f"- {note}" for note in notes)
        lines.append("")
    return "\n".join(lines)


def _collect(mt5, server_symbol: str) -> AuditRow:
    info = mt5.symbol_info(server_symbol)
    if info is None:
        raise SystemExit(f"symbol not found: {server_symbol}")
    spreads: list[float] = []
    for _ in range(SPREAD_SAMPLES):
        tick = mt5.symbol_info_tick(server_symbol)
        if tick is not None and tick.ask > 0 and tick.bid > 0:
            spreads.append((tick.ask - tick.bid) / info.point)
        time.sleep(SPREAD_INTERVAL_SECONDS)
    return AuditRow(
        server_symbol=server_symbol,
        trade_mode=info.trade_mode,
        trade_exemode=info.trade_exemode,
        filling_mode=info.filling_mode,
        trade_stops_level=info.trade_stops_level,
        trade_freeze_level=info.trade_freeze_level,
        volume_min=info.volume_min,
        volume_step=info.volume_step,
        volume_max=info.volume_max,
        trade_tick_value_loss=info.trade_tick_value_loss,
        trade_tick_size=info.trade_tick_size,
        digits=info.digits,
        swap_long=info.swap_long,
        swap_short=info.swap_short,
        median_spread_points=statistics.median(spreads) if spreads else float("nan"),
    )


def main(symbols: Sequence[str]) -> int:
    import MetaTrader5 as mt5  # script-local, outside the src ban

    if not mt5.initialize():
        print(f"initialize failed: {mt5.last_error()}", file=sys.stderr)
        return 1
    try:
        account = mt5.account_info()
        if account is None:
            print("no account info", file=sys.stderr)
            return 1
        if account.trade_mode != ACCOUNT_TRADE_MODE_DEMO:
            print(
                f"REFUSING: account trade_mode is {account.trade_mode}, not demo",
                file=sys.stderr,
            )
            return 2
        for symbol in symbols:
            mt5.symbol_select(symbol, True)
        rows = [_collect(mt5, symbol) for symbol in symbols]
    finally:
        mt5.shutdown()
    print(format_audit_report(rows))
    return 0


def bound_symbols() -> list[str]:
    """The server symbols this system will actually use, from the binding.

    Auditing a hardcoded guess instead is exactly how a signed binding naming
    a symbol the broker does not have survives an audit that looks healthy:
    the report is full of numbers, and none of them are for the symbol the
    gateway will ask for.
    """

    binding = load_venue_binding(
        CONFIG / "venue_binding.mt5.yaml",
        CONFIG / "venue_binding.mt5.yaml.sig",
        CONFIG / "risk_constitution.public.pem",
    )
    return [bound.server_symbol for bound in binding.instruments.values()]


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:] or bound_symbols()))
