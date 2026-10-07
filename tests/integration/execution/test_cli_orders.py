"""The three ``order`` commands, driven through Typer exactly as an operator
drives them.

The phase's headline invariant is that no order is sent while an earlier
intent is unresolved (I-20), and its enforcement point is the wiring inside
``order submit`` -- the gate call that has to happen before an intent is ever
built. A unit test of ``require_clean_ledger`` proves the gate works; only
this proves the command actually calls it. The three commands are also the
only place the *shape* of the gating is visible: placing is gated, diagnosing
and reconciling are not.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import pytest
from alembic import command
from pydantic import SecretStr
from typer.testing import CliRunner

from tests.unit.brokers.mt5.conftest import FakeTerminal
from trading_house import cli
from trading_house.brokers.mt5.boundary import TerminalPort
from trading_house.core.schemas import ApprovedRiskDecision, RejectedRiskDecision
from trading_house.core.values import IntentState, PositiveQuantity, Quantity
from trading_house.database.connection import open_runtime_connection
from trading_house.execution.ledger import PostgresIntentLedger

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
NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)

pytestmark = pytest.mark.integration

runner = CliRunner()


class _DisconnectedTerminal(FakeTerminal):
    """A terminal that initialises but has no link to the trade server.

    Every test here needs one unresolved intent to *stay* unresolved, and a
    blind terminal is the only way to guarantee that without a clock: an
    intent nothing can see is STILL_UNKNOWN however long it has sat there.
    """

    def terminal_connected(self) -> bool:
        return False


@pytest.fixture
def _fresh_ledger(database: DatabaseHarness) -> Iterator[None]:
    """``execution.intent_events`` is append-only against every role, so the
    only way back to an empty table is to recreate it -- the same trick
    ``test_ledger.py`` uses."""

    try:
        yield
    finally:
        command.downgrade(database.alembic_config, "0003_market_bars")
        command.upgrade(database.alembic_config, "head")


@pytest.fixture(autouse=True)
def _runtime_environment(database: DatabaseHarness, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADING_HOUSE_DATABASE_DSN", database.runtime_dsn)
    cli._STATE.debug = False

    def _factory() -> Callable[[ZoneInfo, str], TerminalPort]:
        return lambda _zone, _probe: _DisconnectedTerminal()  # type: ignore[return-value]

    monkeypatch.setattr(cli, "_mt5_terminal_factory", _factory)


def _ledger(database: DatabaseHarness) -> PostgresIntentLedger:
    return PostgresIntentLedger(lambda: open_runtime_connection(SecretStr(database.runtime_dsn)))


def _snapshot() -> dict[str, Any]:
    """A SUBMITTING payload shaped like ``OrderManager``'s own snapshot, so
    the sweep the gate runs reads real submit-time data."""

    return {
        "intent_id": "stuck-1",
        "proposal_id": "proposal-1",
        "strategy_id": "trend_following",
        "book": "fx_scalp",
        "instrument_id": "fx.eurusd",
        "side": "BUY",
        "quantity": "0.10",
        "quantity_unit": "lots",
        "stop_loss": "1.0950",
        "take_profit": None,
        "time_in_force": "GTC",
        "max_slippage_bps": "5",
        "venue_ref": {
            "venue": "mt5",
            "magic": 110042,
            "server_symbol": "EURUSD",
            "order_ticket": None,
            "position_ticket": None,
            "retcode": None,
        },
        "t_submit_utc": NOW.isoformat(),
    }


def _strand_an_intent(database: DatabaseHarness) -> None:
    _ledger(database).append("stuck-1", IntentState.SUBMITTING, NOW, _snapshot())


def _decision_file(path: Path, *, rejected: bool = False) -> Path:
    decision: RejectedRiskDecision | ApprovedRiskDecision
    if rejected:
        decision = RejectedRiskDecision(
            verdict="REJECTED",
            proposal_id="proposal-1",
            reasons=("spread exceeds max_spread_fraction_of_stop",),
            checks_passed=(),
            constitution_version=1,
            approved_quantity=Quantity(amount=Decimal(0), unit="lots"),
            risk_money=Decimal(0),
            risk_pct_of_book=Decimal(0),
        )
    else:
        decision = ApprovedRiskDecision(
            verdict="APPROVED",
            proposal_id="proposal-1",
            reasons=("within per-trade risk",),
            checks_passed=("max_spread_fraction_of_stop",),
            constitution_version=1,
            approved_quantity=PositiveQuantity(amount=Decimal("0.10"), unit="lots"),
            stop_loss_price=Decimal("1.0950"),
            take_profit_price=None,
            risk_money=Decimal("50"),
            risk_pct_of_book=Decimal("0.005"),
        )
    path.write_text(json.dumps(decision.model_dump(mode="json")), encoding="utf-8")
    return path


def _submit_args(decision: Path) -> list[str]:
    return [
        "order",
        "submit",
        "--intent-id",
        "new-1",
        "--strategy-id",
        "trend_following",
        "--book",
        "fx_scalp",
        "--instrument",
        "fx.eurusd",
        "--side",
        "BUY",
        "--decision",
        str(decision),
        *BINDING_ARGS,
    ]


@pytest.mark.usefixtures("_fresh_ledger")
def test_order_submit_refuses_while_an_intent_is_unresolved(
    database: DatabaseHarness, tmp_path: Path
) -> None:
    """I-20 at its enforcement point. The gate is wired into the command
    itself, before an intent is built or a venue is touched -- a gate that
    existed but was never called would pass every unit test and still let the
    second order out."""

    _strand_an_intent(database)

    result = runner.invoke(cli.app, _submit_args(_decision_file(tmp_path / "d.json")))

    assert result.exit_code == cli.ExitCode.UNRESOLVED_INTENTS, result.stdout + result.stderr
    assert "stuck-1" in result.stdout + result.stderr
    assert _ledger(database).current_state("new-1") is None, (
        "a refused submission must leave no trace of a new intent"
    )


@pytest.mark.usefixtures("_fresh_ledger")
def test_order_submit_refuses_a_rejected_decision(tmp_path: Path) -> None:
    """A rejected decision has no approved quantity and no stop. Building an
    intent from one is exactly the unrisked order this command exists to make
    unconstructible."""

    decision = _decision_file(tmp_path / "rejected.json", rejected=True)

    result = runner.invoke(cli.app, _submit_args(decision))

    assert result.exit_code == cli.ExitCode.CONFIGURATION


def test_order_submit_has_no_free_quantity_or_stop_parameters(tmp_path: Path) -> None:
    """Size and stop come from an approved decision or not at all. While
    ``--quantity`` and ``--stop-loss`` exist, ``max_spread_fraction_of_stop``
    -- and every other risk gate -- is bypassable from the only path that can
    place an order."""

    decision = _decision_file(tmp_path / "d.json")

    for option in ("--quantity", "--stop-loss"):
        result = runner.invoke(cli.app, [*_submit_args(decision), option, "1"])

        assert result.exit_code != cli.ExitCode.OK
        assert "No such option" in result.stdout + result.stderr


@pytest.mark.usefixtures("_fresh_ledger")
def test_order_status_still_answers_while_an_intent_is_unresolved(
    database: DatabaseHarness,
) -> None:
    """Section 6.1 gates order-*placing* commands. Gating a read-only
    diagnostic makes the one command that shows why an intent is stuck refuse
    exactly when an intent is stuck."""

    _strand_an_intent(database)

    result = runner.invoke(cli.app, ["order", "status", "--intent-id", "stuck-1"])

    assert result.exit_code == cli.ExitCode.OK
    payload = json.loads(result.stdout)
    assert payload["current_state"] == IntentState.SUBMITTING.value
    assert [event["state"] for event in payload["events"]] == ["SUBMITTING"]


@pytest.mark.usefixtures("_fresh_ledger")
def test_order_reconcile_is_not_gated(database: DatabaseHarness) -> None:
    """This is the command that clears the condition the gate refuses on.
    Gating it would deadlock the system against itself."""

    _strand_an_intent(database)

    result = runner.invoke(cli.app, ["order", "reconcile", *BINDING_ARGS])

    assert result.exit_code == cli.ExitCode.OK
    assert json.loads(result.stdout)["results"] == {"stuck-1": "STILL_UNKNOWN"}


@pytest.mark.usefixtures("_fresh_ledger")
def test_order_reconcile_leaves_an_unseeable_intent_non_terminal(
    database: DatabaseHarness,
) -> None:
    """The terminal here is disconnected, so the sweep cannot see the broker.
    "I cannot see" is never evidence of absence -- writing FAILED would clear
    the gate over a position that may be live."""

    _strand_an_intent(database)

    runner.invoke(cli.app, ["order", "reconcile", *BINDING_ARGS])

    assert _ledger(database).non_terminal() == ("stuck-1",)


@pytest.mark.usefixtures("_fresh_ledger")
def test_order_commands_never_echo_credentials(database: DatabaseHarness, tmp_path: Path) -> None:
    result = runner.invoke(cli.app, _submit_args(_decision_file(tmp_path / "d.json")))

    rendered = result.stdout + result.stderr
    assert database.runtime_dsn not in rendered
    assert "integration-runtime-password" not in rendered
