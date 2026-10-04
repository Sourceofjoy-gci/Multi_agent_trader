"""``BrokerAdapter`` over the MetaTrader 5 gateway: reads, and now writes.

Symbol identity comes from the signed venue binding, never from a broker
string, so a renamed symbol is a config change rather than a code one.

``submit`` and ``close`` arrived in Phase 4, alongside the intent ledger that
makes a lost response recoverable (see ``submit``'s docstring). ``amend_protection``
is the position guard's outer enforcement layer (Phase 5, D-3): it refuses any
stop that would widen against the position's current broker-side one, before
anything reaches the terminal.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from functools import partial
from typing import Any, Protocol

from trading_house.brokers.base import MarketSnapshot, Quote, ReconciliationReport, VenueHealth
from trading_house.brokers.mt5.boundary import (
    Mt5Bar,
    Mt5Position,
    Mt5Tick,
    TerminalPort,
    deal_entry_of,
    mt5_timeframe_code,
    sltp_request,
)
from trading_house.brokers.mt5.contracts import (
    deal_money_of,
    decimal_of,
    position_mark_of,
    position_record_of,
    to_instrument_contract,
)
from trading_house.brokers.mt5.gateway import Mt5Gateway, Priority
from trading_house.brokers.mt5.retcodes import SUCCESS_RETCODES, check_passed, reject_reason_for
from trading_house.constitution.binding import VenueBinding
from trading_house.core.clock import Clock
from trading_house.core.errors import BrokerError, BrokerUnavailableError, ConfigurationError
from trading_house.core.instruments import FillPolicy, InstrumentContract
from trading_house.core.schemas import OrderIntent, PositionState, Side
from trading_house.core.values import BookId, InstrumentId, PositiveQuantity, Price, QuantityUnit
from trading_house.core.venue import (
    DealMoney,
    DealRecord,
    ExecutionOutcome,
    PositionMark,
    PositionRecord,
    PrecheckResult,
    RejectReason,
    Venue,
    VenueRef,
)
from trading_house.marketdata.models import Timeframe, duration

# MetaTrader 5's own enum values, mirrored so this module never needs to
# import MetaTrader5 (only terminal.py may). ``order_check`` is a simulation
# call, not a mutation, so Phase 1 was permitted to build this request; the
# same mirroring now covers the values a real submission needs too.
_TRADE_ACTION_DEAL = 1  # MetaTrader5.TRADE_ACTION_DEAL
_ORDER_TYPE_BUY = 0  # MetaTrader5.ORDER_TYPE_BUY
_ORDER_TYPE_SELL = 1  # MetaTrader5.ORDER_TYPE_SELL
_ORDER_FILLING_FOK = 0  # MetaTrader5.ORDER_FILLING_FOK
_ORDER_FILLING_IOC = 1  # MetaTrader5.ORDER_FILLING_IOC
_ORDER_FILLING_RETURN = 2  # MetaTrader5.ORDER_FILLING_RETURN

_FILL_POLICY_TO_MT5 = {
    FillPolicy.IOC: _ORDER_FILLING_IOC,
    FillPolicy.FOK: _ORDER_FILLING_FOK,
    FillPolicy.RETURN: _ORDER_FILLING_RETURN,
}
# Preference order for a market order (TRADE_ACTION_DEAL): IOC and FOK both
# settle immediately; RETURN can leave a remainder working and exists mainly
# for pending orders, so it is only picked when neither immediate policy is
# on offer.
_FILL_PREFERENCE = (FillPolicy.IOC, FillPolicy.FOK, FillPolicy.RETURN)

_BPS_DIVISOR = Decimal(10_000)


def _type_filling_for(supported: frozenset[FillPolicy]) -> int:
    """The MT5 filling constant for the first preferred policy this broker
    actually offers. ``InstrumentContract`` guarantees at least one, so the
    fallback below is unreachable, not a real code path."""

    for policy in _FILL_PREFERENCE:
        if policy in supported:
            return _FILL_POLICY_TO_MT5[policy]
    raise ConfigurationError()


def _deviation_points(
    max_slippage_bps: Decimal, reference_price: Decimal, point_size: Decimal
) -> int:
    """Convert the neutral basis-points slippage tolerance into MT5's
    ``deviation`` -- an integer count of points from ``reference_price``.
    Truncating rather than rounding is the conservative direction: it never
    grants more tolerance than the intent actually asked for."""

    price_tolerance = reference_price * max_slippage_bps / _BPS_DIVISOR
    return int(price_tolerance / point_size)


def _lots(quantity: PositiveQuantity) -> float:
    """MT5's ``volume`` field is lots, always, whatever unit the intent used.

    Sending ``float(amount)`` for a quantity denominated in shares or base
    units would submit that number of *lots* -- ``--quantity 100
    --quantity-unit shares`` becomes a hundred-lot order. Nothing downstream
    can detect that, so it is refused here, at the one place every order
    request is built.
    """

    if quantity.unit != "lots":
        raise ConfigurationError()
    return float(quantity.amount)


def _tick_for(server_symbol: str, terminal: TerminalPort) -> Mt5Tick | None:
    """Free function so ``snapshot`` can bind the symbol with ``partial``
    instead of closing over a loop variable."""

    return terminal.symbol_tick(server_symbol)


def _improves_on(current_sl: float, is_buy: bool, new_stop: Decimal) -> bool:
    """Whether ``new_stop`` moves a position's protection toward profit.

    MT5 reports "no stop" as 0.0 (see ``position_record_of``); any real stop
    is strictly an improvement over none. Otherwise a BUY's stop may only
    rise and a SELL's may only fall -- equal counts as no improvement
    either, so retrying the position's own current stop is refused rather
    than sent. This is the outer of the two layers enforcing I-8's
    stop-widening prohibition (D-3): the inner one is ``decide()``, which
    must never emit a widening in the first place.
    """

    if current_sl == 0.0:
        return True
    current = decimal_of(current_sl)
    return new_stop > current if is_buy else new_stop < current


def _refused(reason: RejectReason) -> ExecutionOutcome:
    """An outcome that never reached the broker -- refused at this boundary."""

    return ExecutionOutcome(
        accepted=False, venue_ref=None, filled_quantity=None, fill_price=None, reject_reason=reason
    )


class ConfirmedIntentSource(Protocol):
    """The one thing ``reconcile`` needs from the intent ledger: every
    CONFIRMED intent's SUBMITTING snapshot (see ``execution.manager``'s
    ``_snapshot``). Structural, not an import of ``execution.ledger`` --
    ``brokers/`` must not import ``execution/``.
    """

    def confirmed_intents(self) -> Sequence[Mapping[str, Any]]: ...


def _position_state_from(payload: Mapping[str, Any], position: Mt5Position) -> PositionState:
    """Build the matched ``PositionState`` from the intent's own SUBMITTING
    snapshot plus the live venue position.

    ``initial_risk_distance`` is taken from the intent's *recorded* stop, not
    the position's current ``sl`` -- spec 8.2 fixes it at entry, and reading
    it off a live stop would silently redefine R the moment that stop moved.
    Excursion tracking (``r_multiple_open``, ``mae_r``, ``mfe_r``) is the next
    phase's job, so all three are reported as 0.0 rather than invented.
    """

    open_price = decimal_of(position.price_open)
    stop_loss = Decimal(payload["stop_loss"])
    return PositionState(
        intent_id=payload["intent_id"],
        strategy_id=payload["strategy_id"],
        book=payload["book"],
        instrument_id=payload["instrument_id"],
        side=Side(payload["side"]),
        quantity=PositiveQuantity(
            amount=Decimal(payload["quantity"]), unit=payload["quantity_unit"]
        ),
        open_price=open_price,
        current_sl=decimal_of(position.sl),
        current_tp=decimal_of(position.tp) if position.tp is not None else None,
        opened_at_utc=position.opened_at,
        lifecycle="OPEN_PROTECTED",
        r_multiple_open=0.0,
        mae_r=0.0,
        mfe_r=0.0,
        initial_risk_distance=abs(open_price - stop_loss),
        venue_ref=VenueRef(
            venue=Venue.MT5,
            magic=position.magic,
            server_symbol=position.server_symbol,
            position_ticket=position.ticket,
        ),
    )


class Mt5BrokerAdapter:
    """``BrokerAdapter`` over one MetaTrader 5 terminal."""

    def __init__(
        self,
        gateway: Mt5Gateway,
        binding: VenueBinding,
        *,
        clock: Clock,
        ledger: ConfirmedIntentSource | None = None,
    ) -> None:
        self._gateway = gateway
        self._clock = clock
        self._ledger = ledger
        self._server_symbols = {
            instrument_id: bound.server_symbol
            for instrument_id, bound in binding.instruments.items()
        }
        # The reverse of the map above, needed only to turn a raw venue
        # position's server symbol back into our own instrument id when
        # closing one -- ``close`` is handed a ``VenueRef``, which carries
        # the server symbol, not the neutral id.
        self._instrument_ids = {
            server_symbol: instrument_id
            for instrument_id, server_symbol in self._server_symbols.items()
        }
        self._magic_ranges = {book: bound.magic_range for book, bound in binding.books.items()}
        self._started_at = clock.now()
        self._last_quote_at: datetime | None = None

    def _server_symbol_for(self, instrument_id: InstrumentId) -> str:
        server_symbol = self._server_symbols.get(instrument_id)
        if server_symbol is None:
            raise ConfigurationError()
        return server_symbol

    def _instrument_id_for(self, server_symbol: str) -> InstrumentId:
        instrument_id = self._instrument_ids.get(server_symbol)
        if instrument_id is None:
            raise ConfigurationError()
        return instrument_id

    def describe_instrument(self, instrument_id: InstrumentId) -> InstrumentContract:
        server_symbol = self._server_symbol_for(instrument_id)
        info = self._gateway.call(Priority.MARKET_DATA, lambda t: t.symbol_info(server_symbol))
        if info is None:
            raise ConfigurationError()
        return to_instrument_contract(info, instrument_id=instrument_id)

    def snapshot(self, instrument_ids: Sequence[InstrumentId]) -> MarketSnapshot:
        quotes: list[Quote] = []
        for instrument_id in instrument_ids:
            server_symbol = self._server_symbol_for(instrument_id)
            # A plain closure over ``server_symbol`` here would be a classic
            # late-binding loop-variable bug once the gateway call were ever
            # deferred; ``partial`` binds the current value immediately.
            tick = self._gateway.call(Priority.MARKET_DATA, partial(_tick_for, server_symbol))
            if tick is None:
                # Fabricating a quote for a symbol with no current tick would
                # put an invented price into the risk engine. Skip it.
                continue
            self._note_quote(tick.observed_at)
            quotes.append(
                Quote(
                    instrument_id=instrument_id,
                    bid=decimal_of(tick.bid),
                    ask=decimal_of(tick.ask),
                    observed_at=tick.observed_at,
                )
            )
        return MarketSnapshot(quotes=tuple(quotes), taken_at=self._clock.now())

    def history(
        self, instrument_id: InstrumentId, timeframe: Timeframe, start: datetime, end: datetime
    ) -> Sequence[Mt5Bar]:
        """Fetch closed bars at the lowest priority band.

        A multi-hour backfill must never delay a protection call, which is the
        entire reason the gateway's queue is prioritised.
        """

        server_symbol = self._server_symbol_for(instrument_id)
        minutes = int(duration(timeframe).total_seconds() // 60)
        mt5_timeframe_code(minutes)  # fail fast on an unsupported timeframe
        bars = self._gateway.call(
            Priority.MARKET_DATA,
            lambda t: t.copy_rates_range(server_symbol, minutes, start, end),
        )
        return () if bars is None else tuple(bars)

    def precheck(self, intent: OrderIntent) -> PrecheckResult:
        """Ask the venue whether it would accept ``intent``, without acting.

        ``order_check`` is a simulation call, explicitly permitted in a
        read-only phase. It needs a current price to evaluate margin against;
        rather than guess one, a missing tick raises instead of fabricating a
        precheck result.
        """

        server_symbol = self._server_symbol_for(intent.instrument_id)
        tick = self._gateway.call(Priority.ORDER, lambda t: t.symbol_tick(server_symbol))
        if tick is None:
            raise BrokerUnavailableError()
        self._note_quote(tick.observed_at)

        is_buy = intent.side is Side.BUY
        request: dict[str, object] = {
            "action": _TRADE_ACTION_DEAL,
            "symbol": server_symbol,
            "volume": _lots(intent.quantity),
            "type": _ORDER_TYPE_BUY if is_buy else _ORDER_TYPE_SELL,
            "price": tick.ask if is_buy else tick.bid,
            "sl": float(intent.stop_loss),
            "tp": float(intent.take_profit) if intent.take_profit is not None else 0.0,
        }
        result = self._gateway.call(Priority.ORDER, lambda t: t.order_check(request))
        if result is None:
            raise BrokerUnavailableError()
        if check_passed(result.retcode):
            return PrecheckResult(would_accept=True, reject_reason=None)
        return PrecheckResult(would_accept=False, reject_reason=reject_reason_for(result.retcode))

    def submit(self, intent: OrderIntent) -> ExecutionOutcome:
        """Send one order exactly once.

        A ``None`` result from the terminal is NOT a rejection -- it means
        the round trip to the broker library was lost, and the order may
        have executed anyway. Reporting ``ExecutionOutcome(accepted=False)``
        here would let the caller (``execution.manager.OrderManager``)
        record REJECTED for a position that actually exists, and nothing
        would ever go looking for it again. Raising ``BrokerError`` instead
        leaves the intent UNKNOWN, which is exactly the state reconciliation
        exists to resolve.

        The position ticket is deliberately never read off the send result:
        verified against the installed MetaTrader5 5.0.6147, its
        ``OrderSendResult`` carries no ``position`` field at all, so
        ``Mt5SendResult.position_ticket`` is structurally always ``None``. A
        reader treating that ``None`` as "no position exists" would conclude
        the opposite of the truth. The real position ticket comes from the
        confirming deal, via ``deals_since`` and ``reconcile``, not from here.

        ``intent.venue_ref`` is expected to already carry the magic and
        server symbol -- stamped by the caller before submission -- and is
        used as-is rather than derived a second time here.
        """

        venue_ref = intent.venue_ref
        if venue_ref is None:
            raise ConfigurationError()

        contract = self.describe_instrument(intent.instrument_id)
        tick = self._gateway.call(Priority.ORDER, lambda t: t.symbol_tick(venue_ref.server_symbol))
        if tick is None:
            raise BrokerUnavailableError()
        self._note_quote(tick.observed_at)

        is_buy = intent.side is Side.BUY
        reference_price = tick.ask if is_buy else tick.bid
        request: dict[str, object] = {
            "action": _TRADE_ACTION_DEAL,
            "symbol": venue_ref.server_symbol,
            "volume": _lots(intent.quantity),
            "type": _ORDER_TYPE_BUY if is_buy else _ORDER_TYPE_SELL,
            "price": reference_price,
            "sl": float(intent.stop_loss),
            "tp": float(intent.take_profit) if intent.take_profit is not None else 0.0,
            "magic": venue_ref.magic,
            "deviation": _deviation_points(
                intent.max_slippage_bps, decimal_of(reference_price), contract.point_size
            ),
            "type_filling": _type_filling_for(contract.supported_fills),
        }
        return self._send(
            request,
            magic=venue_ref.magic,
            server_symbol=venue_ref.server_symbol,
            fallback_unit=intent.quantity.unit,
        )

    def amend_protection(
        self, ref: VenueRef, stop_loss: Price, take_profit: Price | None
    ) -> ExecutionOutcome:
        """Move a live position's protective stop, refusing to widen it.

        The outer of the two layers enforcing I-8's stop-widening
        prohibition (D-3): ``decide()`` is the inner one and must never emit
        a widening in the first place, but this layer refuses one handed to
        it directly -- by a future caller, or by a bug upstream -- before
        anything is sent to the broker. The two are tested independently, so
        neither can mask a hole in the other.

        ``take_profit=None`` means *leave the position's existing take-profit
        alone*, resolved from the position already read here -- it is not a
        request to remove one, and there is deliberately no way to remove a
        take-profit through this method, because the guard does not manage
        take-profits at all. MT5's ``TRADE_ACTION_SLTP`` treats ``tp=0.0`` as
        *remove the take-profit* (the same convention this codebase already
        relies on for ``sl`` -- see ``position_record_of``), so resolving
        ``None`` to the current TP, rather than passing it straight through
        to ``sltp_request``, is what stops a routine stop tighten from
        silently erasing a live position's exit target.

        A ``None`` result from the terminal is NOT a rejection -- same
        reasoning as ``submit()``: the modification may already have
        landed, and reporting ``ExecutionOutcome(accepted=False)`` here
        would record a stop as unchanged when it actually moved.
        """

        if ref.position_ticket is None:
            raise ConfigurationError()
        positions = self._gateway.call(Priority.ORDER, lambda t: t.positions())
        if positions is None:
            raise BrokerUnavailableError()
        position = next((p for p in positions if p.ticket == ref.position_ticket), None)
        if position is None:
            return _refused(RejectReason.UNKNOWN)
        if not _improves_on(position.sl, position.is_buy, stop_loss):
            return _refused(RejectReason.INVALID_STOPS)

        # take_profit=None means leave the position's existing TP alone --
        # the guard does not manage take-profits at all (see this method's
        # docstring). Resolving it here, from the position already read
        # above, is what keeps sltp_request's own None -> 0.0 reachable only
        # when the position genuinely has none to preserve.
        tp = (
            take_profit
            if take_profit is not None
            else (decimal_of(position.tp) if position.tp is not None else None)
        )
        request = sltp_request(position.server_symbol, position.ticket, stop_loss, tp)
        # TOCTOU ceiling: the broker-side sl read above can change between
        # this read and the send below (MT5 offers no compare-and-swap), so
        # a strictly-worse stop could in principle still land. Inherent, not
        # retried or re-checked here.
        result = self._gateway.call(Priority.ORDER, lambda t: t.send_order(request))
        if result is None:
            raise BrokerError()
        venue_ref = VenueRef(
            venue=Venue.MT5,
            magic=position.magic,
            server_symbol=position.server_symbol,
            position_ticket=position.ticket,
            retcode=result.retcode,
        )
        if result.retcode not in SUCCESS_RETCODES:
            return ExecutionOutcome(
                accepted=False,
                venue_ref=venue_ref,
                filled_quantity=None,
                fill_price=None,
                reject_reason=reject_reason_for(result.retcode),
            )
        return ExecutionOutcome(
            accepted=True,
            venue_ref=venue_ref,
            filled_quantity=None,
            fill_price=None,
            reject_reason=None,
        )

    def close(self, ref: VenueRef, quantity: PositiveQuantity | None) -> ExecutionOutcome:
        """Close all or part of one open position with an opposite-side deal.

        MT5 has no dedicated close call: closing IS a deal, sent the same way
        an opening one is, with ``position`` set to the ticket being closed
        and the side reversed from what is *currently* open. ``VenueRef``
        carries no side, so the position's current side and remaining volume
        are read fresh from the terminal rather than assumed -- a ref minted
        at submission time could otherwise go stale against a position
        already partially closed elsewhere.
        """

        if ref.position_ticket is None:
            raise ConfigurationError()
        positions = self._gateway.call(Priority.ORDER, lambda t: t.positions())
        if positions is None:
            raise BrokerUnavailableError()
        position = next((p for p in positions if p.ticket == ref.position_ticket), None)
        if position is None:
            raise BrokerUnavailableError()

        contract = self.describe_instrument(self._instrument_id_for(position.server_symbol))
        tick = self._gateway.call(Priority.ORDER, lambda t: t.symbol_tick(position.server_symbol))
        if tick is None:
            raise BrokerUnavailableError()
        self._note_quote(tick.observed_at)

        close_is_buy = not position.is_buy
        volume = _lots(quantity) if quantity is not None else position.volume
        reference_price = tick.ask if close_is_buy else tick.bid
        request: dict[str, object] = {
            "action": _TRADE_ACTION_DEAL,
            "symbol": position.server_symbol,
            "position": position.ticket,
            "volume": volume,
            "type": _ORDER_TYPE_BUY if close_is_buy else _ORDER_TYPE_SELL,
            "price": reference_price,
            "sl": 0.0,
            "tp": 0.0,
            "magic": position.magic,
            "type_filling": _type_filling_for(contract.supported_fills),
        }
        return self._send(
            request,
            magic=position.magic,
            server_symbol=position.server_symbol,
            fallback_unit=quantity.unit if quantity is not None else "lots",
        )

    def _send(
        self,
        request: Mapping[str, object],
        *,
        magic: int,
        server_symbol: str,
        fallback_unit: QuantityUnit,
    ) -> ExecutionOutcome:
        """Shared submit/close tail: call the terminal once, and turn its
        reply into a neutral outcome. Never returns a rejection for a
        ``None`` result -- see ``submit``'s docstring."""

        result = self._gateway.call(Priority.ORDER, lambda t: t.send_order(request))
        if result is None:
            raise BrokerError()

        # Never Mt5SendResult.position_ticket (see submit's docstring): the
        # confirming deal is the only trustworthy source, read later by
        # reconcile(), not here.
        result_ref = VenueRef(
            venue=Venue.MT5,
            magic=magic,
            server_symbol=server_symbol,
            order_ticket=result.order_ticket,
            position_ticket=None,
            retcode=result.retcode,
        )
        if result.retcode in SUCCESS_RETCODES:
            return ExecutionOutcome(
                accepted=True,
                venue_ref=result_ref,
                filled_quantity=PositiveQuantity(
                    amount=decimal_of(result.volume), unit=fallback_unit
                ),
                fill_price=decimal_of(result.price),
                reject_reason=None,
            )
        return ExecutionOutcome(
            accepted=False,
            venue_ref=result_ref,
            filled_quantity=None,
            fill_price=None,
            reject_reason=reject_reason_for(result.retcode),
        )

    def deals_since(self, start: datetime) -> tuple[DealRecord, ...] | None:
        """Every broker deal from ``start`` to now, as neutral ``DealRecord``s,
        or ``None`` when the history could not be read at all.

        ``execution/`` may not import ``brokers/``, so this is where
        ``Mt5Deal`` crosses into the execution-owned ``DealRecord`` shape --
        once, here, rather than at every caller. The ``None`` is passed
        through rather than flattened to ``()``: a failed history query that
        looked like an empty one would let the reconciler call a live order
        FAILED.
        """

        deals = self._gateway.call(
            Priority.RECONCILE, lambda t: t.history_deals(start, self._clock.now())
        )
        if deals is None:
            return None
        return tuple(
            DealRecord(
                magic=deal.magic,
                server_symbol=deal.server_symbol,
                volume=decimal_of(deal.volume),
                position_ticket=deal.position_ticket,
                dealt_at=deal.dealt_at,
                entry=deal_entry_of(deal.entry),
            )
            for deal in deals
        )

    def positions_now(self) -> tuple[PositionRecord, ...] | None:
        """Every open position as a neutral ``PositionRecord``, or ``None``
        when the terminal could not be read.

        The reconciler's second source of evidence, and the stronger one: a
        deal can be missing from history for reasons that have nothing to do
        with whether a position exists.
        """

        positions = self._gateway.call(Priority.RECONCILE, lambda t: t.positions())
        if positions is None:
            return None
        return tuple(position_record_of(position) for position in positions)

    def account_equity(self) -> Decimal | None:
        """The account's equity, or ``None`` when it could not be read.

        Phase 10's portfolio measures every percentage limit against this, so
        an unreadable equity is a refusal at the gate, never a zero.
        """

        equity = self._gateway.call(Priority.RECONCILE, lambda t: t.account_equity())
        return None if equity is None else decimal_of(equity)

    def deal_money_since(self, start: datetime) -> tuple[DealMoney, ...] | None:
        """Every deal's money from ``start`` to now, or ``None`` when the
        history could not be read -- the same blindness ``deals_since`` keeps."""

        deals = self._gateway.call(
            Priority.RECONCILE, lambda t: t.history_deals(start, self._clock.now())
        )
        if deals is None:
            return None
        return tuple(deal_money_of(deal) for deal in deals)

    def position_marks(self) -> tuple[PositionMark, ...] | None:
        """Every open position as the broker marks it now, or ``None``."""

        positions = self._gateway.call(Priority.RECONCILE, lambda t: t.positions())
        if positions is None:
            return None
        return tuple(position_mark_of(position) for position in positions)

    def reconcile(self, book: BookId) -> ReconciliationReport:
        """Match every venue position relevant to ``book`` against the intent
        ledger, and report the rest as unmatched.

        A position matches when its magic equals a CONFIRMED intent's magic.
        The matched ``PositionState`` is built from that intent's own
        SUBMITTING snapshot, never invented from the live position -- see
        ``_position_state_from``. A position with no matching CONFIRMED
        intent -- manually opened, or belonging to another system sharing
        the account -- is reported in ``unmatched_venue_refs`` instead:
        adopting it would put a position we never sized under our own risk
        accounting. With no ``ledger`` supplied at all, every position is
        unmatched, which is this method's original (Phase 1) behaviour.

        Scope: a position is this call's business when its magic falls
        inside ``book``'s declared range, or inside no declared range at all
        (a manually opened position, or any other trade MT5 did not tag with
        one of our magics -- MT5 defaults an unset magic to 0). A position
        whose magic falls inside *another* book's declared range belongs to
        that call instead, and is excluded here.
        """

        this_range = self._magic_ranges.get(book)
        if this_range is None:
            raise ConfigurationError()
        other_ranges = [
            magic_range
            for other_book, magic_range in self._magic_ranges.items()
            if other_book != book
        ]

        confirmed_by_magic: dict[int, Mapping[str, Any]] = {}
        if self._ledger is not None:
            for payload in self._ledger.confirmed_intents():
                venue_ref = payload.get("venue_ref")
                if venue_ref is not None:
                    confirmed_by_magic[venue_ref["magic"]] = payload

        positions = self._gateway.call(Priority.RECONCILE, lambda t: t.positions())
        if positions is None:
            raise BrokerUnavailableError()
        matched: list[PositionState] = []
        unmatched: list[VenueRef] = []
        for position in positions:
            if any(low <= position.magic <= high for low, high in other_ranges):
                continue
            matched_payload = confirmed_by_magic.get(position.magic)
            if matched_payload is None:
                unmatched.append(
                    VenueRef(
                        venue=Venue.MT5,
                        magic=position.magic,
                        server_symbol=position.server_symbol,
                        order_ticket=None,
                        position_ticket=position.ticket,
                        retcode=None,
                    )
                )
                continue
            matched.append(_position_state_from(matched_payload, position))

        return ReconciliationReport(
            book=book,
            positions=tuple(matched),
            unmatched_venue_refs=tuple(unmatched),
            reconciled_at=self._clock.now(),
        )

    def health(self) -> VenueHealth:
        """Report connectivity and how stale our newest quote is.

        ``connected`` asks the terminal whether it has a live link to the
        trade server, round-tripped through the gateway so that a wedged actor
        also reads as disconnected. ``last_quote_age_seconds`` measures the
        newest tick this adapter has actually observed -- not the actor's
        heartbeat, which advances every 100ms while the actor is merely idle
        and would report a market-data feed frozen for an hour as zero
        seconds old.

        When no quote has ever been observed, the age is measured from when
        this adapter started, which is the truthful statement that our quote
        knowledge is at least that stale.
        """

        connected = False
        try:
            connected = self._gateway.call(Priority.MARKET_DATA, lambda t: t.terminal_connected())
            probe = next(iter(self._server_symbols.values()))
            self._gateway.call(Priority.MARKET_DATA, lambda t: self._observe(t, probe))
        except BrokerUnavailableError:
            connected = False

        newest = self._last_quote_at or self._started_at
        age_seconds = max(0, int((self._clock.now() - newest).total_seconds()))
        return VenueHealth(
            connected=connected,
            server_utc_offset_seconds=self._gateway.server_utc_offset_seconds,
            last_quote_age_seconds=age_seconds,
        )

    def _observe(self, terminal: TerminalPort, server_symbol: str) -> None:
        """Read one tick and record its age, without producing a Quote."""

        tick = terminal.symbol_tick(server_symbol)
        if tick is not None:
            self._note_quote(tick.observed_at)

    def _note_quote(self, observed_at: datetime) -> None:
        if self._last_quote_at is None or observed_at > self._last_quote_at:
            self._last_quote_at = observed_at
