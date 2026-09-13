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
from trading_house.execution.loop import SYSTEM_TICKET
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
    """SIGINT sets the stop event rather than killing the process, so the
    daemon leaves a dated row behind instead of a gap nobody can explain."""

    runner.invoke(cli.app, RUN_ARGS)

    row = _store(database).latest(SYSTEM_TICKET)
    assert row is not None
    assert row["lifecycle"] == "GUARD_STOPPED"


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
def test_guard_status_shows_an_escalated_position_and_needs_no_terminal(
    database: DatabaseHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The command an operator reaches for after an escalation -- which is
    exactly when the broker may be the thing that is broken, so it must not
    need one."""

    _record(
        database,
        stop_loss="1.09500",
        escalated="true",
        escalation_reason="restore already failed twice",
    )
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
            "stop_loss": "1.09500",
            "escalated": True,
            "escalation_reason": "restore already failed twice",
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
