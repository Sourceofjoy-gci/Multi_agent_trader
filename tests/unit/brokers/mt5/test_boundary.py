from datetime import UTC, datetime, timedelta

import pytest

from trading_house.brokers.mt5.boundary import (
    SYMBOL_FILLING_FOK,
    SYMBOL_FILLING_IOC,
    SYMBOL_TRADE_EXECUTION_MARKET,
    SYMBOL_TRADE_MODE_DISABLED,
    SYMBOL_TRADE_MODE_FULL,
    Mt5Bar,
    Mt5Deal,
    Mt5Position,
    Mt5SendResult,
    Mt5SymbolInfo,  # noqa: F401 -- imported to prove it is part of the boundary's surface
    Mt5Tick,
    TerminalPort,
    establish_utc_offset,
    mt5_timeframe_code,
    server_time_to_utc,
    utc_offset_seconds,
)
from trading_house.core.errors import BrokerUnavailableError, ConfigurationError

EXPECTED_PORT_METHODS = {
    "initialize",
    "shutdown",
    "account_trade_mode",
    "autotrading_enabled",
    "terminal_connected",
    "server_utc_offset_seconds",
    "symbol_info",
    "symbol_tick",
    "positions",
    "order_check",
    "send_order",
    "history_deals",
    "last_error",
    "copy_rates_range",
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


def test_a_bar_dto_is_frozen_and_carries_no_metatrader_types() -> None:
    from dataclasses import FrozenInstanceError

    bar = Mt5Bar(
        event_time=datetime(2026, 8, 25, 9, 0, tzinfo=UTC),
        open=1.1,
        high=1.2,
        low=1.0,
        close=1.15,
        tick_volume=42,
        spread=9,
        real_volume=0,
    )

    assert bar.event_time.tzinfo is not None
    with pytest.raises(FrozenInstanceError):
        bar.open = 2.0  # type: ignore[misc]


def test_every_supported_bar_length_maps_to_a_metatrader_constant() -> None:
    """The constants are not minute counts above H1 -- H1 is 16385, not 60 --
    so a table that silently lost an entry would send the wrong timeframe."""

    assert mt5_timeframe_code(1) == 1
    assert mt5_timeframe_code(60) == 16385
    assert mt5_timeframe_code(240) == 16388
    assert mt5_timeframe_code(1440) == 16408


def test_an_unmapped_bar_length_is_a_configuration_error_not_a_key_error() -> None:
    """M30 and H2 are real MetaTrader 5 timeframes this system does not
    ingest. Asking for one is a caller bug and should say so."""

    with pytest.raises(ConfigurationError):
        mt5_timeframe_code(30)


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


def test_send_result_carries_every_ticket_mt5_can_return() -> None:
    """A submission can yield an order ticket, a position ticket and a deal
    ticket, and reconciliation needs all three: the deal proves execution, the
    position is what the guard will later modify, and the order is what a
    pending request is cancelled by."""

    result = Mt5SendResult(
        retcode=10009,
        order_ticket=1,
        position_ticket=2,
        deal_ticket=3,
        volume=0.25,
        price=1.10000,
        comment="Done",
    )

    assert (result.order_ticket, result.position_ticket, result.deal_ticket) == (1, 2, 3)


def test_send_result_tickets_are_optional_because_a_rejection_has_none() -> None:
    """A rejected send returns a retcode and nothing else. Making the tickets
    required would force the terminal layer to invent zeros, and zero is a
    meaningful magic value elsewhere in this codebase."""

    result = Mt5SendResult(
        retcode=10019,
        order_ticket=None,
        position_ticket=None,
        deal_ticket=None,
        volume=0.0,
        price=0.0,
        comment="No money",
    )

    assert result.order_ticket is None


def test_deal_carries_the_fields_reconciliation_matches_on() -> None:
    """Matching is by magic AND symbol AND volume AND time, because magic is a
    locator that collides, not an identity."""

    dealt = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)
    deal = Mt5Deal(
        ticket=9,
        order_ticket=8,
        position_ticket=7,
        magic=110042,
        server_symbol="EURUSD",
        volume=0.25,
        price=1.10000,
        is_buy=True,
        dealt_at=dealt,
    )

    assert (deal.magic, deal.server_symbol, deal.volume, deal.dealt_at) == (
        110042,
        "EURUSD",
        0.25,
        dealt,
    )


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


# --- server-clock arithmetic -------------------------------------------------
#
# MetaTrader 5 reports every timestamp in the broker server's timezone and never
# says which one it is. These two functions are the whole of the correction, so
# a sign error here would silently shift every timestamp the system records.


@pytest.mark.parametrize(
    ("offset_hours", "label"),
    [(0, "broker on UTC"), (3, "broker ahead of UTC"), (-5, "broker behind UTC")],
)
def test_offset_recovers_the_real_utc_instant(offset_hours: int, label: str) -> None:
    real_utc = datetime(2026, 8, 25, 9, 30, tzinfo=UTC)
    real_epoch = real_utc.timestamp()
    server_epoch = real_epoch + offset_hours * 3600

    offset = utc_offset_seconds(server_epoch, real_epoch)

    assert offset == offset_hours * 3600, label
    assert server_time_to_utc(server_epoch, offset) == real_utc, label


def test_converted_timestamps_are_timezone_aware() -> None:
    """I-10: a naive datetime must never escape the broker boundary."""

    converted = server_time_to_utc(1_756_112_400.0, 10800)

    assert converted.tzinfo is not None
    assert converted.utcoffset() == timedelta(0)


def test_a_broker_ahead_of_utc_reads_as_an_earlier_utc_instant() -> None:
    """Pins the sign: a server clock reading 12:00 at UTC 09:00 is UTC 09:00."""

    server_noon = datetime(2026, 8, 25, 12, 0, tzinfo=UTC).timestamp()

    assert server_time_to_utc(server_noon, 3 * 3600) == datetime(2026, 8, 25, 9, 0, tzinfo=UTC)


def test_converting_before_the_offset_is_known_raises_rather_than_guessing() -> None:
    """An unset offset must fail loud, not pass broker time off as UTC."""

    with pytest.raises(BrokerUnavailableError):
        server_time_to_utc(1_756_112_400.0, None)


def test_the_same_conversion_succeeds_once_the_offset_is_established() -> None:
    assert server_time_to_utc(1_756_112_400.0, 3600).tzinfo is not None


# --- the offset must not be believed when the tick is stale ------------------


def test_a_few_seconds_of_tick_lag_still_yields_the_exact_offset() -> None:
    """Broker offsets are whole half-hours, so quantisation absorbs the
    sub-minute lag between a tick and the clock reading beside it."""

    real = datetime(2026, 8, 25, 9, 30, tzinfo=UTC).timestamp()

    assert utc_offset_seconds(real + 3 * 3600 - 4, real) == 3 * 3600


def test_a_tick_from_a_closed_market_is_refused_not_believed() -> None:
    """A Friday-close tick probed on a Sunday reads as an offset near -48h.
    Believing it would shift every timestamp in the system by two days."""

    real = datetime(2026, 8, 30, 12, 0, tzinfo=UTC).timestamp()

    with pytest.raises(BrokerUnavailableError):
        utc_offset_seconds(real - 2 * 24 * 3600, real)


def test_a_moderately_stale_tick_is_refused_rather_than_rounded() -> None:
    """Twenty minutes of staleness is small enough to survive the plausible
    range but large enough to quantise to the wrong half-hour."""

    real = datetime(2026, 8, 25, 9, 30, tzinfo=UTC).timestamp()

    with pytest.raises(BrokerUnavailableError):
        utc_offset_seconds(real + 3 * 3600 - 20 * 60, real)


def test_an_offset_that_is_not_a_broker_offset_is_refused() -> None:
    """No broker runs its server 7 minutes off UTC. That is a stale tick."""

    real = datetime(2026, 8, 25, 9, 30, tzinfo=UTC).timestamp()

    with pytest.raises(BrokerUnavailableError):
        utc_offset_seconds(real + 7 * 60, real)


# --- only a moving clock distinguishes a timezone from a closed market ------


def test_a_live_feed_establishes_the_offset() -> None:
    real = datetime(2026, 8, 25, 9, 30, tzinfo=UTC).timestamp()
    server = real + 3 * 3600
    samples = iter([server, server + 1.0])

    offset = establish_utc_offset(lambda: next(samples), lambda: real, lambda _s: None)

    assert offset == 3 * 3600


def test_a_frozen_clock_is_refused_however_plausible_its_offset_looks() -> None:
    """The case quantisation alone cannot catch. A Saturday probe of a tick
    exactly one quantum stale reads a UTC+3 broker as UTC+2:30 -- inside the
    plausible range, zero residual, and completely wrong. Only the fact that
    the clock is not moving reveals it."""

    real = datetime(2026, 8, 29, 7, 0, tzinfo=UTC).timestamp()
    frozen = real + 3 * 3600 - 1800

    with pytest.raises(BrokerUnavailableError):
        establish_utc_offset(lambda: frozen, lambda: real, lambda _s: None)


def test_a_probe_symbol_with_no_tick_at_all_is_refused() -> None:
    with pytest.raises(BrokerUnavailableError):
        establish_utc_offset(lambda: None, lambda: 0.0, lambda _s: None)


def test_the_probe_gives_up_instead_of_waiting_forever() -> None:
    sleeps: list[float] = []

    with pytest.raises(BrokerUnavailableError):
        establish_utc_offset(lambda: 1.0, lambda: 0.0, sleeps.append, max_attempts=3)

    assert len(sleeps) == 3
