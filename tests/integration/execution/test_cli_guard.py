"""The two ``guard`` commands, driven through Typer exactly as an operator
drives them.

The cycle itself is covered exhaustively by ``tests/unit/execution/test_loop.py``.
What only this file can prove is the composition root: that ``guard run``
builds a real store, a real ``ProtectionPort`` over the MT5 adapter, and a
real escalator, and that the stop the ledger holds actually reaches the
terminal. It is also the only place the *shape* of the gating is visible --
``order submit`` is gated by the unresolved-intent check and ``guard run``
deliberately is not (D-6).
"""

from __future__ import annotations

import json
import signal
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from alembic import command
from pydantic import SecretStr
from typer.testing import CliRunner

from tests.unit.brokers.mt5.conftest import FakeTerminal
from trading_house import cli
from trading_house.brokers.mt5.boundary import (
    Mt5Position,
    Mt5SendResult,
    Mt5SymbolInfo,
    TerminalPort,
)
from trading_house.core.values import IntentState
from trading_house.database.connection import open_runtime_connection
from trading_house.execution.ledger import PostgresIntentLedger
from trading_house.execution.loop import SYSTEM_TICKET, PositionGuard
from trading_house.execution.positions import PostgresPositionStore

if TYPE_CHECKING:
    from ...conftest import DatabaseHarness

PROJECT_ROOT = Path(__file__).resolve().parents[3]
CONFIG_DIR = PROJECT_ROOT / "config"
BINDING_ARGS = [
    "--venue-binding",
    str(CONFIG_DIR / "venue_binding.mt5.yaml"),
    "--venue-binding-signature",
    str(CONFIG_DIR / "venue_binding.mt5.yaml.sig"),
    "--venue-binding-public-key",
    str(CONFIG_DIR / "risk_constitution.public.pem"),
]
RUN_ARGS = ["guard", "run", "--interval-seconds", "0", *BINDING_ARGS]
NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
TICKET = 1001

pytestmark = pytest.mark.integration

runner = CliRunner()


class _GuardTerminal(FakeTerminal):
    """A terminal with one live position whose protective stop has vanished,
    and enough symbol metadata for the contract read the daemon makes at
    startup."""

    def __init__(self, *, sl: float = 0.0) -> None:
        super().__init__(
            send_result=Mt5SendResult(
                retcode=10009,
                order_ticket=None,
                position_ticket=TICKET,
                deal_ticket=None,
                volume=0.1,
                price=1.1,
                comment="Done",
            )
        )
        self._sl = sl

    def positions(self) -> Sequence[Mt5Position]:
        return (
            Mt5Position(
                ticket=TICKET,
                magic=110042,  # inside fx_scalp's declared range
                server_symbol="EURUSD",
                volume=0.1,
                price_open=1.1000,
                sl=self._sl,  # 0.0 is MT5 for "no stop at all"
                tp=0.0,
                is_buy=True,
                opened_at=NOW,
                price_current=1.1000,
                profit=0.0,
                swap=0.0,
            ),
        )

    def symbol_info(self, server_symbol: str) -> Mt5SymbolInfo | None:
        return Mt5SymbolInfo(
            name=server_symbol,
            digits=5,
            point=0.00001,
            trade_tick_size=0.00001,
            trade_tick_value_loss=1.0,
            volume_min=0.01,
            volume_step=0.01,
            volume_max=100.0,
            trade_stops_level=0,
            trade_freeze_level=0,
            trade_mode=4,
            trade_exemode=2,
            filling_mode=3,
            currency_base="EUR",
            currency_profit="USD",
        )


class _PartiallyBlindTerminal(_GuardTerminal):
    """XAUUSD's contract cannot be read -- ``symbol_info`` returns ``None``
    for it, the same as a real terminal that does not know the symbol --
    while EURUSD's still can. Both are bound in ``config/venue_binding.mt5.yaml``."""

    def symbol_info(self, server_symbol: str) -> Mt5SymbolInfo | None:
        if server_symbol == "XAUUSD":
            return None
        return super().symbol_info(server_symbol)


class _SelfSettingStop(threading.Event):
    """An event that sets itself the first time the daemon waits on it, so
    ``run()`` performs exactly one cycle and then exits through its own
    shutdown path. Nothing here sleeps or spins."""

    def wait(self, timeout: float | None = None) -> bool:
        self.set()
        return True


@pytest.fixture
def _isolated_position_events(database: DatabaseHarness) -> Iterator[None]:
    """``execution.position_events`` is append-only by trigger against every
    role, so downgrading past 0006 and back is the only way to an empty
    table -- the same reset ``test_positions.py`` uses."""

    try:
        yield
    finally:
        command.downgrade(database.alembic_config, "0005_one_submitting_per_intent")
        command.upgrade(database.alembic_config, "head")


@pytest.fixture
def terminal() -> _GuardTerminal:
    return _GuardTerminal()


@pytest.fixture(autouse=True)
def _runtime_environment(
    database: DatabaseHarness, terminal: _GuardTerminal, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TRADING_HOUSE_DATABASE_DSN", database.runtime_dsn)
    cli._STATE.debug = False

    def _factory() -> Callable[[str], TerminalPort]:
        return lambda _probe: terminal  # type: ignore[return-value]

    monkeypatch.setattr(cli, "_mt5_terminal_factory", _factory)
    monkeypatch.setattr(cli, "_stop_event", _SelfSettingStop)


def _store(database: DatabaseHarness) -> PostgresPositionStore:
    return PostgresPositionStore(lambda: open_runtime_connection(SecretStr(database.runtime_dsn)))


def _record(database: DatabaseHarness, **payload: str) -> None:
    _store(database).append(TICKET, "OPEN_PROTECTED", NOW, payload)


def _sltp_requests(terminal: _GuardTerminal) -> list[Mapping[str, object]]:
    return [request for request in terminal.sent if "sl" in request]


@pytest.mark.usefixtures("_isolated_position_events")
def test_guard_run_puts_the_recorded_stop_back_on_a_live_position(
    database: DatabaseHarness, terminal: _GuardTerminal
) -> None:
    """The phase's one promise, end to end through the real composition root:
    the broker has lost the stop, the ledger still holds it, and one cycle
    puts it back."""

    _record(database, stop_loss="1.09500")

    result = runner.invoke(cli.app, RUN_ARGS)

    assert result.exit_code == cli.ExitCode.OK
    assert [request["sl"] for request in _sltp_requests(terminal)] == [1.095]


@pytest.mark.usefixtures("_isolated_position_events")
def test_guard_run_records_its_own_shutdown(database: DatabaseHarness) -> None:
    """``PositionGuard.run``'s ``finally`` writes a dated ``GUARD_STOPPED`` row
    on any exit from the loop -- this test reaches it through the
    ``_SelfSettingStop`` seam, not a raised SIGINT. The SIGINT wiring itself
    (the handler is installed, restores the prior one, and sets the stop
    event when invoked) is
    ``test_sigint_handler_sets_the_stop_event_and_is_restored_afterward``
    below."""

    runner.invoke(cli.app, RUN_ARGS)

    row = _store(database).latest(SYSTEM_TICKET)
    assert row is not None
    assert row["lifecycle"] == "GUARD_STOPPED"


def test_sigint_handler_sets_the_stop_event_and_is_restored_afterward(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``guard run`` installs a SIGINT handler around its loop and restores
    the prior one in a ``finally`` (``cli.py``'s ``guard_run``). Proved here
    without raising a real signal -- this suite raises none -- by spying on
    ``signal.signal`` to capture the exact handler object installed, then:

    1. calling it directly and checking it sets the daemon's stop event, and
    2. confirming ``signal.getsignal(SIGINT)`` is back to what it was before
       the command ran.

    ``PositionGuard.run`` is stubbed to a no-op so the stop event is touched
    by nothing except the handler under test -- otherwise ``run()``'s own
    shutdown path (or the self-setting seam other tests use) would set it
    first, and the assertion would pass for the wrong reason."""

    installed: list[Callable[[int, object], None]] = []
    real_signal = signal.signal

    def _spy(
        signalnum: int, handler: Callable[[int, object], None]
    ) -> Callable[[int, object], None]:
        if signalnum == signal.SIGINT and not installed:
            installed.append(handler)
        return real_signal(signalnum, handler)

    monkeypatch.setattr(signal, "signal", _spy)
    monkeypatch.setattr(PositionGuard, "run", lambda self, stop, interval_seconds=1.0: None)
    stop_event = threading.Event()
    monkeypatch.setattr(cli, "_stop_event", lambda: stop_event)

    before = signal.getsignal(signal.SIGINT)
    result = runner.invoke(cli.app, RUN_ARGS)

    assert result.exit_code == cli.ExitCode.OK
    assert signal.getsignal(signal.SIGINT) is before

    assert len(installed) == 1
    assert not stop_event.is_set()  # the no-op run() never touched it
    installed[0](signal.SIGINT, None)
    assert stop_event.is_set()


@pytest.mark.usefixtures("_isolated_position_events")
def test_guard_run_is_not_gated_by_an_unresolved_intent(
    database: DatabaseHarness, terminal: _GuardTerminal
) -> None:
    """D-6. The guard opens nothing, and a guard that stopped protecting live
    positions because an unrelated intent was stuck would abandon money at the
    worst possible moment. ``order submit`` refuses in exactly this state."""

    ledger = PostgresIntentLedger(lambda: open_runtime_connection(SecretStr(database.runtime_dsn)))
    ledger.append("stuck-1", IntentState.SUBMITTING, NOW, {"intent_id": "stuck-1"})
    _record(database, stop_loss="1.09500")
    try:
        result = runner.invoke(cli.app, RUN_ARGS)
    finally:
        command.downgrade(database.alembic_config, "0003_market_bars")
        command.upgrade(database.alembic_config, "head")

    assert result.exit_code == cli.ExitCode.OK
    assert [request["sl"] for request in _sltp_requests(terminal)] == [1.095]


@pytest.mark.usefixtures("_isolated_position_events")
def test_guard_run_skips_an_unreadable_instrument_instead_of_refusing_to_start(
    database: DatabaseHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 8: XAUUSD's contract can't be read at startup, but that must
    not take every OTHER bound instrument's guarding down with it -- the
    daemon skips XAUUSD, says so in its own output, and still restores
    EURUSD's stop in the same cycle."""

    blind_terminal = _PartiallyBlindTerminal()

    def _factory() -> Callable[[str], TerminalPort]:
        return lambda _probe: blind_terminal  # type: ignore[return-value]

    monkeypatch.setattr(cli, "_mt5_terminal_factory", _factory)
    _record(database, stop_loss="1.09500")

    result = runner.invoke(cli.app, RUN_ARGS)

    assert result.exit_code == cli.ExitCode.OK
    payload = json.loads(result.stdout)
    assert payload["skipped_instruments"] == ["XAUUSD"]
    assert [request["sl"] for request in _sltp_requests(blind_terminal)] == [1.095]


@pytest.mark.usefixtures("_isolated_position_events")
def test_guard_status_shows_an_escalated_position_and_needs_no_terminal(
    database: DatabaseHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The command an operator reaches for after an escalation -- which is
    exactly when the broker may be the thing that is broken, so it must not
    need one.

    The escalated row comes from a real cycle, not a hand-seeded payload: the
    record has no stop and the broker has none either (``_GuardTerminal``'s
    default ``sl=0.0``), which is ``decide()``'s "no stop anywhere to
    restore". Writer (``loop.py``) and reader (``cli.py``) of the
    ``escalated``/``escalation_reason`` payload keys are both real production
    code paths here, so a rename on either side breaks this test instead of
    leaving ``guard status`` silently reporting ``escalated: 0`` forever with
    every test green (Finding 4)."""

    _record(database)  # no recorded stop, and the broker has none either
    run_result = runner.invoke(cli.app, RUN_ARGS)
    assert run_result.exit_code == cli.ExitCode.OK

    monkeypatch.setattr(cli, "_mt5_terminal_factory", lambda: None)
    result = runner.invoke(cli.app, ["guard", "status"])

    assert result.exit_code == cli.ExitCode.OK
    payload = json.loads(result.stdout)
    assert payload["open_positions"] == 1
    assert payload["escalated"] == 1
    assert payload["positions"] == [
        {
            "position_ticket": TICKET,
            "lifecycle": "OPEN_PROTECTED",
            "stop_loss": None,
            "escalated": True,
            "escalation_reason": "no stop anywhere to restore",
        }
    ]


@pytest.mark.usefixtures("_isolated_position_events")
def test_guard_commands_never_echo_credentials(database: DatabaseHarness) -> None:
    _record(database, stop_loss="1.09500")

    rendered = ""
    for args in (RUN_ARGS, ["guard", "status"]):
        result = runner.invoke(cli.app, args)
        rendered += result.stdout + result.stderr

    assert database.runtime_dsn not in rendered
    assert "integration-runtime-password" not in rendered
