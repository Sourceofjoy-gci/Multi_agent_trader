from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from tests.unit.brokers.mt5.conftest import FakeTerminal
from trading_house.brokers.base import BrokerAdapter
from trading_house.brokers.mt5.adapter import ConfirmedIntentSource, Mt5BrokerAdapter
from trading_house.brokers.mt5.boundary import (
    TRADE_ACTION_SLTP,
    Mt5CheckResult,
    Mt5Deal,
    Mt5Position,
    Mt5SendResult,
)
from trading_house.brokers.mt5.gateway import Mt5Gateway
from trading_house.brokers.mt5.magic import derive_magic
from trading_house.brokers.mt5.retcodes import RETCODE_REJECT_REASON
from trading_house.constitution.binding import parse_venue_binding
from trading_house.core.clock import SystemClock
from trading_house.core.errors import BrokerError, BrokerUnavailableError, ConfigurationError
from trading_house.core.schemas import OrderIntent, Side
from trading_house.core.values import IntentState, PositiveQuantity, TimeInForce
from trading_house.core.venue import Mt5VenueRef, PositionRecord, RejectReason, Venue
from trading_house.marketdata.models import Timeframe

_MAGIC_RANGE = (110000, 119999)  # fx_scalp's declared range, below

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
    # Every other test in this module cares about something other than
    # venue_ref, so it defaults to a magic derived the same way the real
    # caller derives one -- present, but only ever asserted on by the tests
    # that are actually about stamping.
    kwargs.setdefault(
        "venue_ref",
        Mt5VenueRef(
            venue=Venue.MT5,
            magic=derive_magic(str(kwargs["intent_id"]), _MAGIC_RANGE),
            server_symbol="EURUSD",
        ),
    )
    return OrderIntent(**kwargs)  # type: ignore[arg-type]


def _adapter(
    terminal: object, *, ledger: ConfirmedIntentSource | None = None
) -> tuple[Mt5BrokerAdapter, Mt5Gateway]:
    gateway = Mt5Gateway(terminal, clock=SystemClock(), request_timeout_seconds=5.0)  # type: ignore[arg-type]
    gateway.start()
    return Mt5BrokerAdapter(gateway, BINDING, clock=SystemClock(), ledger=ledger), gateway


def test_adapter_satisfies_the_broker_protocol(symbol_terminal: FakeTerminal) -> None:
    adapter, gateway = _adapter(symbol_terminal)
    try:
        assert isinstance(adapter, BrokerAdapter)
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


# --- submit() ----------------------------------------------------------


def _send_result(**overrides: object) -> Mt5SendResult:
    fields: dict[str, object] = {
        "retcode": 10009,
        "order_ticket": 1,
        "position_ticket": None,
        "deal_ticket": 3,
        "volume": 0.1,
        "price": 1.10000,
        "comment": "Done",
    }
    fields.update(overrides)
    return Mt5SendResult(**fields)  # type: ignore[arg-type]


def test_submit_builds_a_request_with_float_prices(symbol_terminal: FakeTerminal) -> None:
    """MT5 returns None with no useful error when sl or tp is an int. This is
    a documented, commonly-hit trap and the reason every price crossing the
    boundary is coerced to float."""

    symbol_terminal.send_result = _send_result(volume=0.25)
    adapter, gateway = _adapter(symbol_terminal)
    try:
        adapter.submit(_intent())
    finally:
        gateway.stop()

    request = symbol_terminal.sent[0]
    assert isinstance(request["sl"], float)
    assert isinstance(request["volume"], float)


def test_submit_stamps_the_request_with_the_intents_own_magic(
    symbol_terminal: FakeTerminal,
) -> None:
    """Reconciliation finds the deal by magic. An unstamped order -- or one
    re-derived instead of read from the intent -- is unrecoverable after a
    lost response."""

    symbol_terminal.send_result = _send_result()
    intent = _intent()
    adapter, gateway = _adapter(symbol_terminal)
    try:
        adapter.submit(intent)
    finally:
        gateway.stop()

    assert intent.venue_ref is not None
    assert symbol_terminal.sent[0]["magic"] == intent.venue_ref.magic
    assert intent.venue_ref.magic == derive_magic(intent.intent_id, _MAGIC_RANGE)


def test_a_success_retcode_yields_an_accepted_outcome(symbol_terminal: FakeTerminal) -> None:
    symbol_terminal.send_result = _send_result(volume=0.25, price=1.10050)
    adapter, gateway = _adapter(symbol_terminal)
    try:
        outcome = adapter.submit(_intent())
    finally:
        gateway.stop()

    assert outcome.accepted is True
    assert outcome.reject_reason is None
    assert outcome.filled_quantity is not None
    assert outcome.filled_quantity.amount == Decimal("0.25")
    assert outcome.fill_price == Decimal("1.1005")


def test_a_rejection_retcode_maps_through_the_taxonomy(symbol_terminal: FakeTerminal) -> None:
    symbol_terminal.send_result = _send_result(retcode=10019, volume=0.0, price=0.0)
    adapter, gateway = _adapter(symbol_terminal)
    try:
        outcome = adapter.submit(_intent())
    finally:
        gateway.stop()

    assert outcome.accepted is False
    assert outcome.reject_reason is RejectReason.INSUFFICIENT_FUNDS


def test_a_none_result_raises_rather_than_returning_a_rejection(
    symbol_terminal: FakeTerminal,
) -> None:
    """A None result is NOT a rejection -- the order may have executed. If
    the adapter returned ExecutionOutcome(accepted=False) here, the manager
    would record REJECTED and the position would be orphaned forever, with
    nothing ever looking for it again."""

    symbol_terminal.send_result = None
    adapter, gateway = _adapter(symbol_terminal)
    try:
        with pytest.raises(BrokerError):
            adapter.submit(_intent())
    finally:
        gateway.stop()


@pytest.mark.parametrize("retcode", sorted(RETCODE_REJECT_REASON))
def test_every_mapped_retcode_produces_its_own_rejection_reason(
    retcode: int, symbol_terminal: FakeTerminal
) -> None:
    """Exhaustive over the taxonomy, so a code added to the map without
    handling here fails immediately."""

    symbol_terminal.send_result = _send_result(retcode=retcode, volume=0.0, price=0.0)
    adapter, gateway = _adapter(symbol_terminal)
    try:
        outcome = adapter.submit(_intent())
    finally:
        gateway.stop()

    assert outcome.accepted is False
    assert outcome.reject_reason is RETCODE_REJECT_REASON[retcode]


# --- reconcile() against the intent ledger ------------------------------


class _FakeLedger:
    """A ``ConfirmedIntentSource`` needing no real intent ledger."""

    def __init__(self, payloads: Sequence[Mapping[str, Any]] = ()) -> None:
        self._payloads = tuple(payloads)

    def confirmed_intents(self) -> Sequence[Mapping[str, Any]]:
        return self._payloads


def _confirmed_payload(**overrides: object) -> Mapping[str, Any]:
    payload: dict[str, Any] = {
        "intent_id": "intent-1",
        "strategy_id": "strat-1",
        "book": "fx_scalp",
        "instrument_id": "fx.eurusd",
        "side": "BUY",
        "quantity": "0.1",
        "quantity_unit": "lots",
        "stop_loss": "1.09700",
        "venue_ref": {"venue": "mt5", "magic": 110042, "server_symbol": "EURUSD"},
    }
    payload.update(overrides)
    return payload


class _SinglePositionTerminal(FakeTerminal):
    """One open position, magic 110042 -- inside fx_scalp's declared range."""

    def positions(self) -> tuple[Mt5Position, ...]:
        return (
            Mt5Position(
                ticket=1001,
                magic=110042,
                server_symbol="EURUSD",
                volume=0.1,
                price_open=1.10000,
                sl=1.09500,
                tp=1.11000,
                is_buy=True,
                opened_at=datetime(2026, 8, 25, tzinfo=UTC),
            ),
        )


def test_a_position_whose_magic_matches_a_confirmed_intent_is_reported_matched() -> None:
    adapter, gateway = _adapter(
        _SinglePositionTerminal(), ledger=_FakeLedger([_confirmed_payload()])
    )
    try:
        report = adapter.reconcile("fx_scalp")
    finally:
        gateway.stop()

    assert len(report.positions) == 1
    assert report.unmatched_venue_refs == ()


def test_a_position_with_no_matching_intent_stays_unmatched() -> None:
    """A manually opened position, or one from another system sharing the
    account. Adopting it would put a position we never sized under our own
    risk accounting."""

    adapter, gateway = _adapter(_SinglePositionTerminal(), ledger=_FakeLedger())
    try:
        report = adapter.reconcile("fx_scalp")
    finally:
        gateway.stop()

    assert report.positions == ()
    assert len(report.unmatched_venue_refs) == 1


def test_a_matched_position_carries_the_stop_the_intent_recorded() -> None:
    """initial_risk_distance is what every R-multiple derives from, and spec
    8.2 says it is fixed at entry and never changes. Reading it from the live
    position's current sl (1.09500, 0.00500 away) rather than the intent's
    recorded one (1.09700, 0.00300 away) would silently redefine R the moment
    that stop moved."""

    adapter, gateway = _adapter(
        _SinglePositionTerminal(),
        ledger=_FakeLedger([_confirmed_payload(stop_loss="1.09700")]),
    )
    try:
        report = adapter.reconcile("fx_scalp")
    finally:
        gateway.stop()

    assert report.positions[0].initial_risk_distance == Decimal("0.00300")


# --- the lot-size guard (I-6) ------------------------------------------------


def test_submit_refuses_a_quantity_that_is_not_in_lots(symbol_terminal: FakeTerminal) -> None:
    """MT5's ``volume`` field is lots, always. ``--quantity 100
    --quantity-unit shares`` would otherwise send a hundred-lot order, and
    nothing downstream could tell."""

    symbol_terminal.send_result = _send_result()
    adapter, gateway = _adapter(symbol_terminal)
    try:
        with pytest.raises(ConfigurationError):
            adapter.submit(_intent(quantity=PositiveQuantity(amount=Decimal("100"), unit="shares")))
    finally:
        gateway.stop()

    assert symbol_terminal.sent == [], "nothing may reach the venue on a refused unit"


def test_close_refuses_a_quantity_that_is_not_in_lots() -> None:
    """The same field on the same request: closing 100 "shares" would send a
    hundred-lot close."""

    adapter, gateway = _adapter(_SinglePositionTerminal())
    try:
        with pytest.raises(ConfigurationError):
            adapter.close(
                Mt5VenueRef(
                    venue=Venue.MT5,
                    magic=110042,
                    server_symbol="EURUSD",
                    position_ticket=1001,
                ),
                PositiveQuantity(amount=Decimal("100"), unit="shares"),
            )
    finally:
        gateway.stop()


# --- amend_protection(): the outer of the two layers enforcing I-8 (D-3) ----


def _ref(*, position_ticket: int) -> Mt5VenueRef:
    return Mt5VenueRef(
        venue=Venue.MT5, magic=110042, server_symbol="EURUSD", position_ticket=position_ticket
    )


def _position(*, ticket: int, sl: float, is_buy: bool) -> Mt5Position:
    return Mt5Position(
        ticket=ticket,
        magic=110042,
        server_symbol="EURUSD",
        volume=0.1,
        price_open=1.10000,
        sl=sl,
        tp=None,
        is_buy=is_buy,
        opened_at=datetime(2026, 8, 25, tzinfo=UTC),
    )


def test_a_widening_stop_is_refused_at_the_boundary() -> None:
    """The SECOND enforcement layer. decide() cannot emit a widening, but this
    must refuse one handed to it directly -- otherwise a future caller, or a
    bug, could widen a live stop with nothing objecting. The layers must be
    tested separately: an earlier phase claimed enforcement by grant AND
    trigger while only ever exercising the grant."""

    terminal = FakeTerminal(positions=[_position(ticket=7, sl=1.09700, is_buy=True)])
    adapter, gateway = _adapter(terminal)
    try:
        outcome = adapter.amend_protection(_ref(position_ticket=7), Decimal("1.09600"), None)
    finally:
        gateway.stop()

    assert not outcome.accepted
    assert terminal.sent == []  # nothing was even attempted


def test_a_tightening_stop_is_sent() -> None:
    terminal = FakeTerminal(
        positions=[_position(ticket=7, sl=1.09700, is_buy=True)], send_result=_send_result()
    )
    adapter, gateway = _adapter(terminal)
    try:
        outcome = adapter.amend_protection(_ref(position_ticket=7), Decimal("1.09800"), None)
    finally:
        gateway.stop()

    assert outcome.accepted
    assert terminal.sent[0]["position"] == 7


def test_the_request_carries_float_prices_and_the_position_ticket() -> None:
    """MT5 returns None with no useful error when sl or tp is an int, and
    TRADE_ACTION_SLTP without a `position` modifies nothing at all. Both are
    documented traps."""

    terminal = FakeTerminal(
        positions=[_position(ticket=7, sl=1.09700, is_buy=True)], send_result=_send_result()
    )
    adapter, gateway = _adapter(terminal)
    try:
        adapter.amend_protection(_ref(position_ticket=7), Decimal("1.09800"), None)
    finally:
        gateway.stop()

    request = terminal.sent[0]
    assert isinstance(request["sl"], float)
    assert isinstance(request["tp"], float)
    assert request["position"] == 7
    assert request["action"] == TRADE_ACTION_SLTP


def test_a_none_result_raises_rather_than_reporting_a_rejection() -> None:
    """Same rule as submit: a None result may mean the modification landed, so
    reporting a rejection would record a stop as unchanged when it moved."""

    terminal = FakeTerminal(
        positions=[_position(ticket=7, sl=1.09700, is_buy=True)], send_result=None
    )
    adapter, gateway = _adapter(terminal)
    try:
        with pytest.raises(BrokerError):
            adapter.amend_protection(_ref(position_ticket=7), Decimal("1.09800"), None)
    finally:
        gateway.stop()


def test_a_position_the_broker_does_not_have_is_refused() -> None:
    """Amending a ticket that no longer exists must not be reported as done."""

    adapter, gateway = _adapter(FakeTerminal(positions=[]))
    try:
        outcome = adapter.amend_protection(_ref(position_ticket=7), Decimal("1.09800"), None)
    finally:
        gateway.stop()

    assert not outcome.accepted


# --- reads that failed are not reads that found nothing (C-2) ----------------


def test_deals_since_reports_an_unreadable_history_as_none() -> None:
    """``history_deals_get`` returns None on error, not on an empty window.
    Flattening that to () tells the reconciler the order never happened."""

    class _BlindTerminal(FakeTerminal):
        def history_deals(self, start: datetime, end: datetime) -> Sequence[Mt5Deal] | None:
            return None

    adapter, gateway = _adapter(_BlindTerminal())
    try:
        assert adapter.deals_since(datetime(2026, 8, 25, tzinfo=UTC)) is None
    finally:
        gateway.stop()


def test_positions_now_reports_an_unreadable_terminal_as_none() -> None:
    class _BlindTerminal(FakeTerminal):
        def positions(self) -> Sequence[Mt5Position] | None:
            return None

    adapter, gateway = _adapter(_BlindTerminal())
    try:
        assert adapter.positions_now() is None
    finally:
        gateway.stop()


def test_positions_now_maps_open_positions_to_neutral_records(
    position_terminal: FakeTerminal,
) -> None:
    """The reconciler's second source of evidence, and ``execution/`` may not
    import ``brokers/`` -- so ``Mt5Position`` crosses into ``PositionRecord``
    here."""

    adapter, gateway = _adapter(position_terminal)
    try:
        records = adapter.positions_now()
    finally:
        gateway.stop()

    assert records is not None
    assert records[0] == PositionRecord(
        magic=110042,
        server_symbol="EURUSD",
        volume=Decimal("0.1"),
        position_ticket=1001,
        stop_loss=Decimal("1.095"),
        open_price=Decimal("1.1"),
        is_buy=True,
        opened_at=datetime(2026, 8, 25, tzinfo=UTC),
    )


def test_reconcile_refuses_when_positions_cannot_be_read() -> None:
    """Reporting "no positions" from a failed read would empty
    ``unmatched_venue_refs`` and declare the account clean."""

    class _BlindTerminal(FakeTerminal):
        def positions(self) -> Sequence[Mt5Position] | None:
            return None

    adapter, gateway = _adapter(_BlindTerminal())
    try:
        with pytest.raises(BrokerUnavailableError):
            adapter.reconcile("fx_scalp")
    finally:
        gateway.stop()
