"""Phase 1 acceptance: the read-only gateway holds its boundaries.

This file asserts what Phase 1 *promised*: that it reads and never writes.
Import confinement -- that ``MetaTrader5`` is reachable from exactly one
module -- and ``terminal.py``'s statement cap are owned by
``test_architecture.py``, which also carries the guard-the-guard tests
proving those checks can still fail. They are deliberately not repeated here.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
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
    TRADE_ACTION_SLTP,
    Mt5Position,
    Mt5SendResult,
)
from trading_house.brokers.mt5.gateway import Mt5Gateway
from trading_house.constitution.binding import parse_venue_binding
from trading_house.core.clock import SystemClock
from trading_house.core.errors import NonDemoAccountError
from trading_house.core.venue import Mt5VenueRef, Venue

# Mirrored the same way adapter.py mirrors it (see that module's docstring):
# this test needs to prove amend_protection never sends this action, and
# adapter.py's own copy is private.
_TRADE_ACTION_DEAL = 1  # MetaTrader5.TRADE_ACTION_DEAL

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Phase 4 filled in submit() and close(); Phase 5 fills in the last stubbed
# method, amend_protection(). Nothing on the adapter's surface refuses
# unconditionally any more, so the REFUSING_METHODS tuple and the test that
# parametrized over it are gone rather than emptied -- an empty parametrize
# does not run its body once, it reports a skip, which reads as a passing
# guard in the summary line while asserting nothing. A later phase that stubs
# a method back out writes its own refusal test; it would have had to anyway,
# since an empty tuple would have caught nothing. What amend_protection must
# still never do -- close a position on its own initiative -- is pinned below.

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


class _TerminalWithAPosition(_StubTerminal):
    """A double that actually has a position to amend, and records what
    gets sent to it -- so a real amend can be exercised, not just the
    not-found refusal (which a double with no ``send_order`` at all cannot
    tell apart from a real close attempt: both would fail loudly the same
    way)."""

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[Mapping[str, object]] = []

    def positions(self) -> tuple[Mt5Position, ...]:
        return (
            Mt5Position(
                ticket=7,
                magic=1,
                server_symbol="EURUSD",
                volume=0.1,
                price_open=1.10000,
                sl=1.09700,
                tp=None,
                is_buy=True,
                opened_at=datetime(2026, 8, 25, tzinfo=UTC),
            ),
        )

    def send_order(self, request: Mapping[str, object]) -> Mt5SendResult:
        self.sent.append(request)
        return Mt5SendResult(
            retcode=10009,
            order_ticket=None,
            position_ticket=None,
            deal_ticket=1,
            volume=0.1,
            price=1.09800,
            comment="Done",
        )


def test_amend_protection_never_closes_a_position_on_its_own_initiative() -> None:
    """What survives now that Phase 5 has implemented amend_protection: the
    guard protects, it does not trade (spec section 10). Given a real
    position to amend, the single request it sends is a TRADE_ACTION_SLTP,
    never a TRADE_ACTION_DEAL (the action that opens or closes a position) --
    so amending a stop cannot, itself, close the position out from under it."""

    terminal = _TerminalWithAPosition()
    gateway = Mt5Gateway(cast(Any, terminal), clock=SystemClock())
    gateway.start()
    try:
        ref = Mt5VenueRef(venue=Venue.MT5, magic=1, server_symbol="EURUSD", position_ticket=7)
        adapter = Mt5BrokerAdapter(gateway, BINDING, clock=SystemClock())
        outcome = adapter.amend_protection(ref, Decimal("1.09800"), None)
    finally:
        gateway.stop()

    assert outcome.accepted is True
    assert len(terminal.sent) == 1
    assert terminal.sent[0]["action"] == TRADE_ACTION_SLTP
    assert all(request["action"] != _TRADE_ACTION_DEAL for request in terminal.sent)


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
