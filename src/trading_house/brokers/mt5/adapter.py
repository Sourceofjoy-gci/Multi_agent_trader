"""The read-only half of BrokerAdapter, backed by the MetaTrader 5 gateway.

Phase 1 sends no orders. ``submit``, ``amend_protection`` and ``close`` refuse
until Phase 3, when the intent ledger exists to make a lost response
recoverable. Symbol identity comes from the signed venue binding, never from a
broker string, so a renamed symbol is a config change rather than a code one.

``reconcile`` cannot populate ``ReconciliationReport.positions`` in this
phase either: ``PositionState`` requires ``strategy_id``, ``lifecycle``, the
R-multiples and ``initial_risk_distance``, none of which are derivable from a
raw MT5 position without the intent ledger that arrives in Phase 3. See
``reconcile``'s docstring for what it reports instead.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from functools import partial

from trading_house.brokers.base import MarketSnapshot, Quote, ReconciliationReport, VenueHealth
from trading_house.brokers.mt5.boundary import Mt5Bar, Mt5Tick, TerminalPort, mt5_timeframe_code
from trading_house.brokers.mt5.contracts import decimal_of, to_instrument_contract
from trading_house.brokers.mt5.gateway import Mt5Gateway, Priority
from trading_house.brokers.mt5.retcodes import check_passed, reject_reason_for
from trading_house.constitution.binding import VenueBinding
from trading_house.core.clock import Clock
from trading_house.core.errors import BrokerUnavailableError, ConfigurationError
from trading_house.core.instruments import InstrumentContract
from trading_house.core.schemas import OrderIntent, Side
from trading_house.core.values import BookId, InstrumentId, PositiveQuantity, Price
from trading_house.core.venue import ExecutionOutcome, PrecheckResult, Venue, VenueRef
from trading_house.marketdata.models import Timeframe, duration

_PHASE_3 = "order submission arrives in Phase 3 with the intent ledger"

# MetaTrader 5's own enum values, mirrored so this module never needs to
# import MetaTrader5 (only terminal.py may). ``order_check`` is a simulation
# call, not a mutation, so Phase 1 is permitted to build this request.
_TRADE_ACTION_DEAL = 1  # MetaTrader5.TRADE_ACTION_DEAL
_ORDER_TYPE_BUY = 0  # MetaTrader5.ORDER_TYPE_BUY
_ORDER_TYPE_SELL = 1  # MetaTrader5.ORDER_TYPE_SELL


def _tick_for(server_symbol: str, terminal: TerminalPort) -> Mt5Tick | None:
    """Free function so ``snapshot`` can bind the symbol with ``partial``
    instead of closing over a loop variable."""

    return terminal.symbol_tick(server_symbol)


class Mt5BrokerAdapter:
    """Read-only BrokerAdapter over one MetaTrader 5 terminal."""

    def __init__(self, gateway: Mt5Gateway, binding: VenueBinding, *, clock: Clock) -> None:
        self._gateway = gateway
        self._clock = clock
        self._server_symbols = {
            instrument_id: bound.server_symbol
            for instrument_id, bound in binding.instruments.items()
        }
        self._magic_ranges = {book: bound.magic_range for book, bound in binding.books.items()}
        self._started_at = clock.now()
        self._last_quote_at: datetime | None = None

    def _server_symbol_for(self, instrument_id: InstrumentId) -> str:
        server_symbol = self._server_symbols.get(instrument_id)
        if server_symbol is None:
            raise ConfigurationError()
        return server_symbol

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
        code = mt5_timeframe_code(int(duration(timeframe).total_seconds() // 60))
        bars = self._gateway.call(
            Priority.MARKET_DATA,
            lambda t: t.copy_rates_range(server_symbol, code, start, end),
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
            "volume": float(intent.quantity.amount),
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
        raise NotImplementedError(_PHASE_3)

    def amend_protection(
        self, ref: VenueRef, stop_loss: Price, take_profit: Price | None
    ) -> ExecutionOutcome:
        raise NotImplementedError(_PHASE_3)

    def close(self, ref: VenueRef, quantity: PositiveQuantity | None) -> ExecutionOutcome:
        raise NotImplementedError(_PHASE_3)

    def reconcile(self, book: BookId) -> ReconciliationReport:
        """Report every venue position relevant to ``book``, all unmatched.

        Phase 1 has no intent ledger, so none of the fields ``PositionState``
        requires -- ``strategy_id``, ``lifecycle``, ``r_multiple_open``,
        ``mae_r``, ``mfe_r``, ``initial_risk_distance`` -- are derivable from
        a raw MT5 position. Inventing them would be the same mistake this
        project refuses to make for a fabricated quote, so ``positions`` is
        always empty here. Every position this call is responsible for is
        reported in ``unmatched_venue_refs`` instead: an unmatched venue
        position is exactly what that field means with no ledger to match
        against. Phase 3's intent ledger is what turns an unmatched ref into
        a matched ``PositionState``.

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

        positions = self._gateway.call(Priority.RECONCILE, lambda t: t.positions())
        unmatched: list[VenueRef] = []
        for position in positions:
            if any(low <= position.magic <= high for low, high in other_ranges):
                continue
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

        return ReconciliationReport(
            book=book,
            positions=(),
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
