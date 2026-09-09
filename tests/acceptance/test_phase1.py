"""Phase 1 acceptance: the read-only gateway holds its boundaries.

This file asserts what Phase 1 *promised*: that it reads and never writes.
Import confinement -- that ``MetaTrader5`` is reachable from exactly one
module -- and ``terminal.py``'s statement cap are owned by
``test_architecture.py``, which also carries the guard-the-guard tests
proving those checks can still fail. They are deliberately not repeated here.
"""

from __future__ import annotations

import inspect
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest

from trading_house.brokers.base import BrokerAdapter
from trading_house.brokers.mt5.adapter import Mt5BrokerAdapter
from trading_house.brokers.mt5.boundary import (
    ACCOUNT_TRADE_MODE_CONTEST,
    ACCOUNT_TRADE_MODE_DEMO,
    ACCOUNT_TRADE_MODE_REAL,
)
from trading_house.brokers.mt5.gateway import Mt5Gateway
from trading_house.constitution.binding import parse_venue_binding
from trading_house.core.clock import SystemClock
from trading_house.core.errors import NonDemoAccountError
from trading_house.core.venue import Mt5VenueRef, Venue

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Phase 4 filled in submit() and close(); Phase 5 fills in the last stubbed
# method, amend_protection() -- the position guard. Nothing on the adapter's
# surface unconditionally refuses any more, so this tuple is narrowed to
# empty rather than deleted along with the test that parametrizes over it:
# both stay, so a method stubbed back in with `raise NotImplementedError`
# still gets caught here. What amend_protection's own implementation must
# still never do -- close a position on its own initiative -- is pinned
# below instead, since "refuses in this phase" no longer describes it.
REFUSING_METHODS: tuple[str, ...] = ()

TERMINAL_MODULE = PROJECT_ROOT / "src" / "trading_house" / "brokers" / "mt5" / "terminal.py"
# terminal.py is the one real call, wrapping mt5.order_send(...). Every other
# module -- including boundary.py, whose TerminalPort Protocol method is named
# send_order for exactly this reason -- must be free of the substring
# entirely: an import guard elsewhere stops a module from *importing*
# MetaTrader5, but says nothing about a module reaching into sys.modules for
# an instance another module already imported, so this guard cannot lean on
# that one and must check every file itself.
ORDER_SEND_ALLOWED = frozenset({TERMINAL_MODULE})

BINDING = parse_venue_binding(
    b"""
venue: mt5
books:
  fx_scalp: {magic_range: [110000, 119999]}
instruments:
  fx.eurusd: {server_symbol: "EURUSD"}
"""
)


class _StubTerminal:
    """A TerminalPort needing neither MetaTrader 5 nor Windows."""

    def __init__(self, *, trade_mode: int = ACCOUNT_TRADE_MODE_DEMO) -> None:
        self.trade_mode = trade_mode
        self.shutdown_calls = 0

    def initialize(self) -> bool:
        return True

    def shutdown(self) -> None:
        self.shutdown_calls += 1

    def account_trade_mode(self) -> int:
        return self.trade_mode

    def terminal_connected(self) -> bool:
        return True

    def server_utc_offset_seconds(self) -> int:
        return 0

    def symbol_info(self, server_symbol: str) -> Any:
        return None

    def symbol_tick(self, server_symbol: str) -> Any:
        return None

    def copy_rates_range(
        self, server_symbol: str, timeframe_minutes: int, start: object, end: object
    ) -> tuple[Any, ...]:
        return ()

    def positions(self) -> tuple[Any, ...]:
        return ()

    def order_check(self, request: Any) -> Any:
        return None

    def last_error(self) -> tuple[int, str]:
        return 0, "ok"


def _adapter() -> Mt5BrokerAdapter:
    gateway = Mt5Gateway(cast(Any, _StubTerminal()), clock=SystemClock())
    return Mt5BrokerAdapter(gateway, BINDING, clock=SystemClock())


def test_no_order_send_call_exists_anywhere_in_source() -> None:
    """``order_send`` is the only MT5 call that moves money, and exactly one
    module -- ``brokers/mt5/terminal.py`` -- may name it.

    A substring check rather than an AST walk, deliberately: this must fail
    even if the name appears in a comment, a string, or a getattr lookup.
    """

    for path in sorted((PROJECT_ROOT / "src").rglob("*.py")):
        if path in ORDER_SEND_ALLOWED:
            continue
        assert "order_send" not in path.read_text(encoding="utf-8"), path.name


def test_the_terminal_module_is_where_order_send_actually_lives() -> None:
    """Guard the guard: if order_send moves elsewhere, the exemption is stale."""

    assert TERMINAL_MODULE.exists()
    assert "order_send" in TERMINAL_MODULE.read_text(encoding="utf-8")


@pytest.mark.parametrize("method", REFUSING_METHODS)
def test_every_mutating_method_refuses_in_this_phase(method: str) -> None:
    """Pins the position guard: amending a protective stop is the next
    phase's business, not this one's. The refusal happens before any
    argument is examined, which is why this can be called with nothing
    meaningful."""

    bound = getattr(_adapter(), method)
    arity = len(inspect.signature(bound).parameters)

    with pytest.raises(NotImplementedError):
        bound(*[cast(Any, None)] * arity)


def test_amend_protection_never_closes_a_position_on_its_own_initiative() -> None:
    """What replaces amend_protection in REFUSING_METHODS now that Phase 5
    has implemented it: the guard protects, it does not trade (spec section
    10). A ticket the terminal double below cannot find is refused as a
    normal outcome, not escalated into a close() -- and it couldn't be: this
    double exposes no send_order at all, so any attempt to act on the
    position beyond refusing it would fail loudly, not silently."""

    gateway = Mt5Gateway(cast(Any, _StubTerminal()), clock=SystemClock())
    gateway.start()
    try:
        ref = Mt5VenueRef(venue=Venue.MT5, magic=1, server_symbol="EURUSD", position_ticket=7)
        adapter = Mt5BrokerAdapter(gateway, BINDING, clock=SystemClock())
        outcome = adapter.amend_protection(ref, Decimal("1.0000"), None)
    finally:
        gateway.stop()

    assert outcome.accepted is False


def test_the_adapter_still_presents_the_whole_venue_neutral_surface() -> None:
    """Refusing is not the same as omitting. Phase 3 fills these in without
    any consumer changing shape."""

    assert isinstance(_adapter(), BrokerAdapter)


@pytest.mark.parametrize(
    "trade_mode",
    [ACCOUNT_TRADE_MODE_REAL, ACCOUNT_TRADE_MODE_CONTEST],
    ids=["real", "contest"],
)
def test_only_a_demo_account_is_ever_served(trade_mode: int) -> None:
    """The headline safety property of this phase. There must be no state in
    which the system is connected to a non-demo account and merely not
    trading yet -- so the refusal happens in start(), before the actor thread
    exists, and the terminal is shut down on the way out."""

    terminal = _StubTerminal(trade_mode=trade_mode)
    gateway = Mt5Gateway(cast(Any, terminal), clock=SystemClock())

    with pytest.raises(NonDemoAccountError):
        gateway.start()

    assert terminal.shutdown_calls == 1


def test_the_pure_modules_import_on_any_platform() -> None:
    """CI's coverage-gated job runs on Linux, where MetaTrader5 cannot be
    imported at all. Everything except terminal.py must survive that."""

    from trading_house.brokers.mt5 import adapter, boundary, contracts, gateway, magic, retcodes

    assert all(
        module is not None for module in (adapter, boundary, contracts, gateway, magic, retcodes)
    )


def test_the_package_exposes_the_adapter_without_importing_metatrader5() -> None:
    """``__all__`` is a promise. The lazy ``__getattr__`` behind it is the
    package's public surface, and it must resolve on a platform that has no
    MetaTrader5 at all."""

    import trading_house.brokers.mt5 as mt5_package

    assert mt5_package.__all__ == ["Mt5BrokerAdapter"]
    assert mt5_package.Mt5BrokerAdapter is Mt5BrokerAdapter

    with pytest.raises(AttributeError):
        _ = mt5_package.Mt5Terminal
