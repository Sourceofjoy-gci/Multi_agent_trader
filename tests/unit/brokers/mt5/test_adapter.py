from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from tests.unit.brokers.mt5.conftest import FakeTerminal
from trading_house.brokers.base import BrokerAdapter
from trading_house.brokers.mt5.adapter import Mt5BrokerAdapter
from trading_house.brokers.mt5.boundary import Mt5CheckResult, Mt5Position
from trading_house.brokers.mt5.gateway import Mt5Gateway
from trading_house.constitution.binding import parse_venue_binding
from trading_house.core.clock import SystemClock
from trading_house.core.errors import BrokerUnavailableError, ConfigurationError
from trading_house.core.schemas import OrderIntent, Side
from trading_house.core.values import IntentState, PositiveQuantity, TimeInForce
from trading_house.core.venue import Mt5VenueRef, RejectReason, Venue
from trading_house.marketdata.models import Timeframe

BINDING = parse_venue_binding(
    b"""
venue: mt5
books:
  fx_scalp: {magic_range: [110000, 119999]}
  fx_swing: {magic_range: [120000, 129999]}
instruments:
  fx.eurusd: {server_symbol: "EURUSD"}
"""
)

REF = Mt5VenueRef(venue=Venue.MT5, magic=110042, server_symbol="EURUSD")

_INTENT_KWARGS = {
    "intent_id": "intent-1",
    "proposal_id": "proposal-1",
    "book": "fx_scalp",
    "instrument_id": "fx.eurusd",
    "side": Side.BUY,
    "stop_loss": Decimal("1.0950"),
    "take_profit": None,
    "time_in_force": TimeInForce.GTC,
    "max_slippage_bps": Decimal("5"),
    "state": IntentState.SUBMITTING,
    "t_submit_utc": datetime(2026, 8, 25, tzinfo=UTC),
}


def _intent(**overrides: object) -> OrderIntent:
    kwargs: dict[str, object] = {
        **_INTENT_KWARGS,
        "quantity": PositiveQuantity(amount=Decimal("0.1"), unit="lots"),
        **overrides,
    }
    return OrderIntent(**kwargs)  # type: ignore[arg-type]


def _adapter(terminal: object) -> tuple[Mt5BrokerAdapter, Mt5Gateway]:
    gateway = Mt5Gateway(terminal, clock=SystemClock(), request_timeout_seconds=5.0)  # type: ignore[arg-type]
    gateway.start()
    return Mt5BrokerAdapter(gateway, BINDING, clock=SystemClock()), gateway


def test_adapter_satisfies_the_broker_protocol(symbol_terminal: FakeTerminal) -> None:
    adapter, gateway = _adapter(symbol_terminal)
    try:
        assert isinstance(adapter, BrokerAdapter)
    finally:
        gateway.stop()


@pytest.mark.parametrize("method", ["submit", "amend_protection", "close"])
def test_mutating_methods_refuse_in_this_phase(method: str, symbol_terminal: FakeTerminal) -> None:
    """Phase 1 sends no orders. These arrive with Phase 3's intent ledger."""

    adapter, gateway = _adapter(symbol_terminal)
    calls: dict[str, Callable[[], object]] = {
        "submit": lambda: adapter.submit(_intent()),
        "amend_protection": lambda: adapter.amend_protection(REF, Decimal("1.0900"), None),
        "close": lambda: adapter.close(REF, None),
    }
    try:
        with pytest.raises(NotImplementedError, match="Phase 3"):
            calls[method]()
    finally:
        gateway.stop()


def test_an_unbound_instrument_is_rejected(symbol_terminal: FakeTerminal) -> None:
    """The signed venue binding is authoritative for symbol identity."""

    adapter, gateway = _adapter(symbol_terminal)
    try:
        with pytest.raises(ConfigurationError):
            adapter.describe_instrument("fx.gbpusd")
    finally:
        gateway.stop()


def test_describe_instrument_maps_a_terminal_symbol(symbol_terminal: FakeTerminal) -> None:
    adapter, gateway = _adapter(symbol_terminal)
    try:
        contract = adapter.describe_instrument("fx.eurusd")
    finally:
        gateway.stop()

    assert contract.instrument_id == "fx.eurusd"
    assert contract.price_increment == Decimal("0.00001")


def test_describe_instrument_raises_when_the_terminal_does_not_know_the_symbol() -> None:
    """Bound in the signed binding is not the same as known to this terminal."""

    adapter, gateway = _adapter(FakeTerminal())
    try:
        with pytest.raises(ConfigurationError):
            adapter.describe_instrument("fx.eurusd")
    finally:
        gateway.stop()


def test_snapshot_skips_instruments_with_no_tick(tickless_terminal: FakeTerminal) -> None:
    """Fabricating a quote for a symbol the terminal has no tick for would put
    an invented price into the risk engine."""

    adapter, gateway = _adapter(tickless_terminal)
    try:
        snapshot = adapter.snapshot(["fx.eurusd"])
    finally:
        gateway.stop()

    assert snapshot.quotes == ()


def test_snapshot_builds_a_quote_from_the_terminals_tick(symbol_terminal: FakeTerminal) -> None:
    adapter, gateway = _adapter(symbol_terminal)
    try:
        snapshot = adapter.snapshot(["fx.eurusd"])
    finally:
        gateway.stop()

    assert len(snapshot.quotes) == 1
    quote = snapshot.quotes[0]
    assert quote.instrument_id == "fx.eurusd"
    assert quote.bid == Decimal("1.1")
    assert quote.ask == Decimal("1.2")


def test_reconcile_reports_this_books_positions_as_unmatched(
    position_terminal: FakeTerminal,
) -> None:
    """Phase 1 has no intent ledger, so no position can be attributed yet.
    Reporting them as unmatched is honest; inventing an R-multiple is not."""

    adapter, gateway = _adapter(position_terminal)
    try:
        report = adapter.reconcile("fx_scalp")
    finally:
        gateway.stop()

    assert report.book == "fx_scalp"
    assert report.positions == ()
    assert {ref.magic for ref in report.unmatched_venue_refs} == {110042, 999999}


def test_reconcile_excludes_positions_belonging_to_another_book(
    position_terminal: FakeTerminal,
) -> None:
    """110042 is fx_scalp's. Only the orphan is fx_swing's business."""

    adapter, gateway = _adapter(position_terminal)
    try:
        report = adapter.reconcile("fx_swing")
    finally:
        gateway.stop()

    assert {ref.magic for ref in report.unmatched_venue_refs} == {999999}


def test_reconcile_reports_a_manually_opened_position_as_unmatched() -> None:
    """MT5 uses magic=0 to mean "no magic set" -- a manually opened or foreign
    position. ``Mt5VenueRef.magic`` must accept it without reconcile crashing."""

    class _ManualPositionTerminal(FakeTerminal):
        def positions(self) -> tuple[Mt5Position, ...]:
            return (
                Mt5Position(
                    ticket=7,
                    magic=0,
                    server_symbol="EURUSD",
                    volume=0.1,
                    price_open=1.1000,
                    sl=1.0950,
                    tp=None,
                    is_buy=True,
                    opened_at=datetime(2026, 8, 25, tzinfo=UTC),
                ),
            )

    adapter, gateway = _adapter(_ManualPositionTerminal())
    try:
        report = adapter.reconcile("fx_scalp")
    finally:
        gateway.stop()

    assert report.positions == ()
    assert {ref.magic for ref in report.unmatched_venue_refs} == {0}


def test_reconcile_rejects_an_undeclared_book(symbol_terminal: FakeTerminal) -> None:
    adapter, gateway = _adapter(symbol_terminal)
    try:
        with pytest.raises(ConfigurationError):
            adapter.reconcile("not_a_declared_book")
    finally:
        gateway.stop()


def test_precheck_reports_would_accept_on_a_success_retcode(
    symbol_terminal: FakeTerminal,
) -> None:
    adapter, gateway = _adapter(symbol_terminal)
    try:
        result = adapter.precheck(_intent())
    finally:
        gateway.stop()

    assert result.would_accept is True
    assert result.reject_reason is None


def test_precheck_maps_a_rejecting_retcode(symbol_terminal: FakeTerminal) -> None:
    class _RejectingTerminal(FakeTerminal):
        def order_check(self, request: object) -> Mt5CheckResult:
            return Mt5CheckResult(retcode=10019, comment="no money")

    adapter, gateway = _adapter(_RejectingTerminal())
    try:
        result = adapter.precheck(_intent())
    finally:
        gateway.stop()

    assert result.would_accept is False
    assert result.reject_reason is RejectReason.INSUFFICIENT_FUNDS


def test_precheck_refuses_to_check_without_a_current_price(
    tickless_terminal: FakeTerminal,
) -> None:
    """A precheck built against no current price would be a guess, not a check."""

    adapter, gateway = _adapter(tickless_terminal)
    try:
        with pytest.raises(BrokerUnavailableError):
            adapter.precheck(_intent())
    finally:
        gateway.stop()


def test_precheck_raises_when_the_terminal_returns_no_check_result(
    symbol_terminal: FakeTerminal,
) -> None:
    class _NoCheckTerminal(FakeTerminal):
        def order_check(self, request: object) -> Mt5CheckResult | None:
            return None

    adapter, gateway = _adapter(_NoCheckTerminal())
    try:
        with pytest.raises(BrokerUnavailableError):
            adapter.precheck(_intent())
    finally:
        gateway.stop()


def test_health_reports_connectivity_and_server_offset(symbol_terminal: FakeTerminal) -> None:
    adapter, gateway = _adapter(symbol_terminal)
    try:
        health = adapter.health()
    finally:
        gateway.stop()

    assert health.connected is True
    assert health.server_utc_offset_seconds == 0
    assert health.last_quote_age_seconds >= 0


def test_health_reports_disconnected_when_the_gateway_is_unreachable(
    symbol_terminal: FakeTerminal,
) -> None:
    adapter, gateway = _adapter(symbol_terminal)
    gateway.stop()

    health = adapter.health()

    assert health.connected is False


# --- order_check speaks a different retcode vocabulary than submission -------


class _CheckTerminal(FakeTerminal):
    def __init__(self, retcode: int) -> None:
        super().__init__()
        self._retcode = retcode

    def order_check(self, request: object) -> Mt5CheckResult:
        return Mt5CheckResult(retcode=self._retcode, comment="c")


def _precheck_with(retcode: int) -> object:
    adapter, gateway = _adapter(_CheckTerminal(retcode))
    try:
        return adapter.precheck(_intent())
    finally:
        gateway.stop()


def test_order_check_reports_an_acceptable_request_as_retcode_zero() -> None:
    """A passing simulation returns 0 with comment "Done" -- not one of the
    1000x codes, which belong to the submission path."""

    assert _precheck_with(0).would_accept is True


def test_a_submission_success_code_is_not_a_passing_check() -> None:
    """The discriminating half: if precheck reused the submission success set,
    10009 would read as acceptance and 0 as an unknown rejection. Both wrong."""

    result = _precheck_with(10009)

    assert result.would_accept is False
    assert result.reject_reason is not None


def test_a_real_rejection_still_maps_to_its_reason() -> None:
    assert _precheck_with(10019).reject_reason is RejectReason.INSUFFICIENT_FUNDS


# --- health must measure what its field names claim -------------------------


class _DisconnectedTerminal(FakeTerminal):
    def terminal_connected(self) -> bool:
        return False


def test_health_reports_a_terminal_that_lost_its_trade_server() -> None:
    """``last_error()`` succeeds whether or not the terminal can reach the
    broker, so probing it would report a disconnected venue as connected."""

    adapter, gateway = _adapter(_DisconnectedTerminal())
    try:
        report = adapter.health()
    finally:
        gateway.stop()

    assert report.connected is False


def test_health_ages_the_newest_quote_not_the_actor_heartbeat() -> None:
    """The fake's ticks carry a fixed 2026-08-25 timestamp. The actor
    heartbeat advances every 100ms, so an implementation reading the heartbeat
    reports ~0 seconds however stale the market data actually is."""

    adapter, gateway = _adapter(FakeTerminal())
    try:
        adapter.snapshot(["fx.eurusd"])
        report = adapter.health()
    finally:
        gateway.stop()

    assert report.last_quote_age_seconds > 86_400


def test_history_rejects_an_unbound_instrument(symbol_terminal: FakeTerminal) -> None:
    """The signed venue binding is authoritative for symbol identity here too."""

    adapter, gateway = _adapter(symbol_terminal)
    try:
        with pytest.raises(ConfigurationError):
            adapter.history(
                "fx.gbpusd",
                Timeframe.M1,
                datetime(2026, 8, 24, tzinfo=UTC),
                datetime(2026, 8, 25, tzinfo=UTC),
            )
    finally:
        gateway.stop()


def test_history_returns_an_empty_tuple_when_the_terminal_call_fails() -> None:
    """``None`` means the call failed; ``()`` means the history is genuinely
    absent. This adapter deliberately collapses both to the empty tuple
    rather than surfacing the distinction: Task 10's ingest retries once on
    an empty page before concluding exhaustion, so a failed call and a
    genuinely empty window are handled identically and conservatively either
    way. Handing back ``None`` itself would just push that same collapse onto
    every caller."""

    class _FailedHistoryTerminal(FakeTerminal):
        def copy_rates_range(
            self, server_symbol: str, timeframe_minutes: int, start: object, end: object
        ) -> None:
            return None

    adapter, gateway = _adapter(_FailedHistoryTerminal())
    try:
        bars = adapter.history(
            "fx.eurusd",
            Timeframe.M1,
            datetime(2026, 8, 24, tzinfo=UTC),
            datetime(2026, 8, 25, tzinfo=UTC),
        )
    finally:
        gateway.stop()

    assert bars == ()


def test_the_terminal_receives_minutes_not_an_already_translated_code() -> None:
    """H1 is 60 minutes and MetaTrader 5 code 16385. The terminal does its own
    translation, so an adapter that pre-translates sends 16385 into a
    parameter expecting 60 -- and the second translation rejects it. M1, M5
    and M15 hide this because their minute counts equal their own codes."""

    start = datetime(2026, 8, 24, tzinfo=UTC)
    end = datetime(2026, 8, 25, tzinfo=UTC)
    terminal = FakeTerminal()
    adapter, gateway = _adapter(terminal)
    try:
        adapter.history("fx.eurusd", Timeframe.H1, start, end)
        adapter.history("fx.eurusd", Timeframe.D1, start, end)
    finally:
        gateway.stop()

    assert [req[1] for req in terminal.rate_requests] == [60, 1440]
