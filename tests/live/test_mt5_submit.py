"""Live test: open, confirm, and close one minimum-lot position on a real
MT5 demo account.

Marked ``mt5`` and skipped whenever the terminal, account, or market is not
in a state that makes this test's actions meaningful. Never run in CI.

This test trades, unlike the read-only tests in ``test_mt5_bars.py``, whose
module-level ``_SKIP = skip_reason()`` decides liveness once at collection
and never re-checks it. That is tolerable for a read-only test racing a
flickering feed; it is not tolerable here, where a stale "live" decision
would have this test attempt a real order against a market that closed
minutes after collection. So liveness is probed at call time, in the
``adapter`` fixture, not at import -- an unreachable terminal or a closed
market skips before any connection is even attempted.

The test body then re-probes again, immediately before it sends anything,
using the gateway ``adapter`` already holds open rather than a second call to
``skip_reason()``: that helper does its own ``mt5.initialize()`` /
``mt5.shutdown()`` cycle, and MetaTrader5 is one global session per process,
not one per ``Mt5Terminal`` instance -- calling it again here would tear down
the very connection this test is about to send an order over. ``_feed_still_live``
re-runs the same clock-advances check over that live connection instead.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from pydantic import JsonValue

from trading_house.brokers.mt5.boundary import TerminalPort
from trading_house.brokers.mt5.gateway import Mt5Gateway, Priority
from trading_house.brokers.mt5.magic import derive_magic
from trading_house.constitution.binding import parse_venue_binding
from trading_house.core.clock import SystemClock
from trading_house.core.schemas import OrderIntent, Side
from trading_house.core.values import IntentState, PositiveQuantity, TimeInForce
from trading_house.core.venue import Mt5VenueRef, Venue

from .conftest import clock_advances, skip_reason

if TYPE_CHECKING:
    from trading_house.brokers.mt5.adapter import Mt5BrokerAdapter

INSTRUMENT_ID = "fx.eurusd"
BOOK = "fx_scalp"

BINDING = parse_venue_binding(
    b"""
venue: mt5
server_timezone: "Europe/Athens"
books:
  fx_scalp: {magic_range: [110000, 119999]}
instruments:
  fx.eurusd: {server_symbol: "EURUSD"}
"""
)


def _terminal() -> TerminalPort:
    """Imported here, not at module scope: terminal.py cannot even be
    imported off Windows, and this module must still collect there to be
    skipped, not error."""

    from trading_house.brokers.mt5.terminal import Mt5Terminal

    return Mt5Terminal(BINDING.server_zone)


def _feed_still_live(adapter: Mt5BrokerAdapter) -> bool:
    """Re-probe market liveness immediately before sending, over the
    connection this test is about to use.

    The same judgement ``skip_reason()`` makes elsewhere -- a live feed's
    clock advances, a closed market's does not -- but taken on ``adapter``'s
    own already-open gateway rather than by calling ``skip_reason()`` again:
    that helper's ``mt5.shutdown()`` would tear down this very connection
    (MetaTrader5 is one global session per process, not one per instance).
    """

    def quote_time() -> float | None:
        quotes = adapter.snapshot([INSTRUMENT_ID]).quotes
        return quotes[0].observed_at.timestamp() if quotes else None

    return clock_advances(quote_time)


def _autotrading_enabled(adapter: Mt5BrokerAdapter) -> bool:
    """Re-probe the terminal's AutoTrading toggle over the connection this
    test already holds open -- the same reason ``_feed_still_live`` reuses
    ``adapter`` rather than a fresh ``_terminal()``: MetaTrader5 is one
    global session per process, and a second initialize/shutdown cycle here
    would tear down the very connection this test is about to send over.

    A precondition, not a post-hoc classification of a rejection: retcode
    10027 (``TRADE_RETCODE_CLIENT_DISABLES_AT``) is a *submission* reply
    code, and ``order_check`` -- the only other read of trading permission
    this boundary exposes -- does not share that vocabulary (see
    ``retcodes.py``), so a precheck cannot be trusted to surface this. The
    terminal's own AutoTrading toggle is the direct read -- not
    ``account_info().trade_allowed``, which is a different, account-level
    permission that stays true even while AutoTrading is off.
    """

    return adapter._gateway.call(Priority.MARKET_DATA, lambda t: t.autotrading_enabled())


@pytest.fixture
def demo_events() -> list[str]:
    return []


@pytest.fixture
def adapter(demo_events: list[str]) -> Iterator[Mt5BrokerAdapter]:
    """A real gateway and adapter, opened fresh for this test only.

    ``skip_reason()`` runs here, at fixture setup -- not once at module
    import -- so an unreachable terminal or a closed market skips cleanly
    instead of failing the connection attempt below. The test body re-probes
    it again immediately before it sends anything: that second probe, not
    this one, is what actually stands between it and a real order.
    """

    from trading_house.brokers.mt5.adapter import Mt5BrokerAdapter

    reason = skip_reason()
    if reason is not None:
        pytest.skip(reason)

    events = demo_events

    def on_event(event_type: str, payload: Mapping[str, JsonValue]) -> None:
        del payload
        events.append(event_type)

    with Mt5Gateway(_terminal(), clock=SystemClock(), on_event=on_event) as gateway:
        yield Mt5BrokerAdapter(gateway, BINDING, clock=SystemClock())


@pytest.fixture
def close_everything_after(adapter: Mt5BrokerAdapter) -> Iterator[None]:
    """Teardown runs even if the test body raises. A test that opens a real
    position and dies must not leave one open.

    ``reconcile()`` called with no ledger attached reports every position in
    range as unmatched -- exactly "every open position" -- so this closes by
    venue ref without inventing a second way to list positions.
    """

    yield
    report = adapter.reconcile(BOOK)
    for ref in report.unmatched_venue_refs:
        adapter.close(ref, None)


@pytest.mark.mt5
def test_a_minimum_lot_position_opens_confirms_and_closes(
    adapter: Mt5BrokerAdapter,
    demo_events: list[str],
    close_everything_after: None,
) -> None:
    """Open a minimum-lot position, confirm it, then close it again -- the
    one live test that puts real money-shaped state on the demo account.
    """

    # Re-probe immediately before sending anything. Never trust the
    # fixture's own probe as still current by the time execution reaches
    # here: a stale probe is exactly how this test would attempt a real
    # order against a feed it decided was live minutes earlier.
    if not _feed_still_live(adapter):
        pytest.skip("the market is closed: the broker clock is not advancing")

    # A terminal with AutoTrading off is a valid demo terminal in a
    # configuration where submit() cannot possibly succeed -- an
    # environment precondition, the same class as an unreachable terminal
    # or a closed market, not a code defect. Re-probed here rather than
    # trusted from collection, for the same reason as the feed check above.
    if not _autotrading_enabled(adapter):
        pytest.skip("AutoTrading is disabled in the terminal; the write path cannot be proven")

    # The demo guard runs inside Mt5Gateway.start(); this is its record.
    # This test must never be the thing that discovers the gateway failed
    # to enforce it.
    assert "gateway.demo_verified" in demo_events, "the demo guard did not run before this test"

    contract = adapter.describe_instrument(INSTRUMENT_ID)
    snapshot = adapter.snapshot([INSTRUMENT_ID])
    assert snapshot.quotes, "no live EURUSD quote available"
    quote = snapshot.quotes[0]

    intent_id = f"live-submit-{uuid4()}"
    magic = derive_magic(intent_id, BINDING.books[BOOK].magic_range)
    venue_ref = Mt5VenueRef(venue=Venue.MT5, magic=magic, server_symbol="EURUSD")
    # Comfortably clear of the broker's own minimum stop distance -- this
    # test is about the round trip, not about probing that boundary.
    stop_loss = quote.bid - (contract.min_stop_distance * 3)

    intent = OrderIntent(
        intent_id=intent_id,
        proposal_id=f"live-proposal-{uuid4()}",
        book=BOOK,
        instrument_id=INSTRUMENT_ID,
        side=Side.BUY,
        quantity=PositiveQuantity(amount=contract.quantity_min, unit="lots"),
        stop_loss=stop_loss,
        take_profit=None,
        time_in_force=TimeInForce.GTC,
        max_slippage_bps=Decimal("20"),
        state=IntentState.SUBMITTING,
        t_submit_utc=SystemClock().now(),
        venue_ref=venue_ref,
    )

    outcome = adapter.submit(intent)
    assert outcome.accepted, outcome.reject_reason

    # The send result never carries a usable position ticket (see
    # ``Mt5BrokerAdapter.submit``'s own docstring) -- the confirming deal
    # does, via ``reconcile()``, which is also how ``close()`` must be
    # reached: ``close()`` refuses a ref with no ``position_ticket``.
    report = adapter.reconcile(BOOK)
    matches = [ref for ref in report.unmatched_venue_refs if ref.magic == magic]
    assert len(matches) == 1, "expected exactly one open position for this test's magic"

    close_outcome = adapter.close(matches[0], None)
    assert close_outcome.accepted, close_outcome.reject_reason
