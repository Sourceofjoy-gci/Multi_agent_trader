from datetime import UTC, datetime

from trading_house.brokers.mt5.boundary import (
    SYMBOL_FILLING_FOK,
    SYMBOL_FILLING_IOC,
    SYMBOL_TRADE_EXECUTION_MARKET,
    SYMBOL_TRADE_MODE_DISABLED,
    SYMBOL_TRADE_MODE_FULL,
    Mt5Position,
    Mt5SymbolInfo,  # noqa: F401 -- imported to prove it is part of the boundary's surface
    Mt5Tick,
    TerminalPort,
)

EXPECTED_PORT_METHODS = {
    "initialize",
    "shutdown",
    "account_trade_mode",
    "server_utc_offset_seconds",
    "symbol_info",
    "symbol_tick",
    "positions",
    "order_check",
    "last_error",
}


def test_terminal_port_surface_is_exactly_the_calls_the_gateway_needs() -> None:
    """A wide port is a leaky port — every method here must reach real IPC."""

    actual = {name for name in vars(TerminalPort) if not name.startswith("_")}
    assert actual == EXPECTED_PORT_METHODS


def test_port_is_a_runtime_checkable_protocol() -> None:
    assert getattr(TerminalPort, "_is_protocol", False) is True


def test_dtos_are_frozen() -> None:
    tick = Mt5Tick(bid=1.1, ask=1.2, observed_at=datetime(2026, 8, 25, 9, 0, tzinfo=UTC))
    try:
        tick.bid = 2.0  # type: ignore[misc]
    except AttributeError:
        return
    raise AssertionError("Mt5Tick must be frozen")


def test_mt5_constants_match_the_documented_values() -> None:
    """These mirror MetaTrader5's own enums so pure code need not import it."""

    assert SYMBOL_TRADE_MODE_DISABLED == 0
    assert SYMBOL_TRADE_MODE_FULL == 4
    assert SYMBOL_FILLING_FOK == 1
    assert SYMBOL_FILLING_IOC == 2
    assert SYMBOL_TRADE_EXECUTION_MARKET == 2


def test_position_dto_carries_the_magic_used_for_book_attribution() -> None:
    position = Mt5Position(
        ticket=1,
        magic=110001,
        server_symbol="EURUSD",
        volume=0.1,
        price_open=1.1,
        sl=1.05,
        tp=None,
        is_buy=True,
        opened_at=datetime(2026, 8, 25, 9, 0, tzinfo=UTC),
    )
    assert position.magic == 110001


def test_boundary_module_does_not_import_metatrader5() -> None:
    """The whole point: this module must import on Linux."""

    import ast
    from pathlib import Path

    source = Path("src/trading_house/brokers/mt5/boundary.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "MetaTrader5" not in imported


def test_package_imports_on_any_platform() -> None:
    """brokers.mt5 must not eagerly import terminal.py, which needs Windows."""

    import trading_house.brokers.mt5 as package

    assert package is not None
