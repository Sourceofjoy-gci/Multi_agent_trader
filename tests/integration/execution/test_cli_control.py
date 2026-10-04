"""Phase 11, driven through Typer: the kill switches, safe mode, clearing,
flattening, and the two places ``order submit`` meets them -- the gate before
the terminal is reached, and safe mode after a venue refusal of authority."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from alembic import command
from pydantic import SecretStr
from typer.testing import CliRunner

from tests.unit.brokers.mt5.conftest import FakeTerminal
from trading_house import cli
from trading_house.brokers.mt5.boundary import (
    Mt5Deal,
    Mt5Position,
    Mt5SendResult,
    Mt5SymbolInfo,
    Mt5Tick,
)
from trading_house.core.schemas import ApprovedRiskDecision
from trading_house.core.values import PositiveQuantity
from trading_house.database.connection import open_runtime_connection
from trading_house.execution.ledger import PostgresIntentLedger

if TYPE_CHECKING:
    from ...conftest import DatabaseHarness

pytestmark = pytest.mark.integration

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
NOW = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)

runner = CliRunner()


class _Account(FakeTerminal):
    """A connected demo account that knows EURUSD, quotes it tight, and
    answers a send with whatever the test configured."""

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

    def symbol_tick(self, server_symbol: str) -> Mt5Tick | None:
        return Mt5Tick(bid=1.09999, ask=1.1, observed_at=datetime.now(UTC))


def _position(ticket: int, magic: int) -> Mt5Position:
    return Mt5Position(
        ticket=ticket,
        magic=magic,
        server_symbol="EURUSD",
        volume=0.1,
        price_open=1.1,
        sl=1.095,
        tp=None,
        is_buy=True,
        opened_at=NOW,
        price_current=1.1,
        profit=0.0,
        swap=0.0,
    )


@pytest.fixture
def _fresh_tables(database: DatabaseHarness) -> Iterator[None]:
    """Both append-only tables this module writes start empty: downgrading to
    before 0004 drops the intent, position and control ledgers together."""

    try:
        yield
    finally:
        command.downgrade(database.alembic_config, "0003_market_bars")
        command.upgrade(database.alembic_config, "head")


@pytest.fixture(autouse=True)
def _runtime_environment(
    database: DatabaseHarness, monkeypatch: pytest.MonkeyPatch, _fresh_tables: None
) -> None:
    monkeypatch.setenv("TRADING_HOUSE_DATABASE_DSN", database.runtime_dsn)
    monkeypatch.delenv("TRADING_HOUSE_ALERT_WEBHOOK_URL", raising=False)
    cli._STATE.debug = False


def _use(monkeypatch: pytest.MonkeyPatch, terminal: FakeTerminal) -> None:
    monkeypatch.setattr(cli, "_mt5_terminal_factory", lambda: lambda _probe: terminal)


def _ok(args: list[str]) -> dict[str, Any]:
    result = runner.invoke(cli.app, args)
    assert result.exit_code == 0, result.stdout + result.stderr
    return dict(json.loads(result.stdout))


def _status() -> dict[str, Any]:
    return _ok(["control", "status"])


def _kill(*extra: str) -> dict[str, Any]:
    return _ok(["control", "kill", "--operator", "ana", "--reason", "desk review", *extra])


def _decision_file(path: Path) -> Path:
    decision = ApprovedRiskDecision(
        verdict="APPROVED",
        proposal_id="proposal-1",
        reasons=(),
        checks_passed=(),
        constitution_version=1,
        approved_quantity=PositiveQuantity(amount=Decimal("0.10"), unit="lots"),
        stop_loss_price=Decimal("1.0950"),
        take_profit_price=None,
        risk_money=Decimal("50"),
        risk_pct_of_book=Decimal("0.17"),
    )
    path.write_text(json.dumps(decision.model_dump(mode="json")), encoding="utf-8")
    return path


def _submit(tmp_path: Path) -> Any:
    return runner.invoke(
        cli.app,
        [
            "order",
            "submit",
            "--intent-id",
            "new-1",
            "--strategy-id",
            "vol_breakout_eurusd_h1",
            "--book",
            "fx_scalp",
            "--instrument",
            "fx.eurusd",
            "--side",
            "BUY",
            "--decision",
            str(_decision_file(tmp_path / "d.json")),
            *BINDING_ARGS,
        ],
    )


def _ledger(database: DatabaseHarness) -> PostgresIntentLedger:
    return PostgresIntentLedger(lambda: open_runtime_connection(SecretStr(database.runtime_dsn)))


# --- switches -----------------------------------------------------------------------------


def test_a_kill_is_listed_until_a_person_clears_it() -> None:
    assert _status() == {
        "status": "ok",
        "halts": [],
        "halted": False,
        "alerting": "unconfigured",
    }

    killed = _kill("--scope", "book", "--target", "fx_scalp")
    halt = killed["halt"]
    assert (halt["kind"], halt["scope"], halt["target"], halt["actor"]) == (
        "kill",
        "book",
        "fx_scalp",
        "ana",
    )
    assert killed["changed"] is True
    # No channel is configured, so the halt says nobody was told.
    assert killed["alerted"] is False
    assert [h["halt_id"] for h in _status()["halts"]] == [halt["halt_id"]]

    cleared = _ok(
        [
            "control",
            "clear",
            "--halt-id",
            halt["halt_id"],
            "--operator",
            "ben",
            "--reason",
            "reviewed",
        ]
    )
    assert cleared["changed"] is True
    assert _status()["halted"] is False


def test_clearing_what_is_not_in_force_is_exit_24() -> None:
    result = runner.invoke(
        cli.app,
        ["control", "clear", "--halt-id", "nope", "--operator", "ben", "--reason", "r"],
    )

    assert result.exit_code == cli.ExitCode.HALT_NOT_ACTIVE


@pytest.mark.parametrize(
    "args",
    [
        ["--scope", "book", "--target", "fx_scalpp"],
        ["--scope", "instrument", "--target", "fx.gbpjpy"],
        ["--scope", "strategy", "--target", "no_such_strategy"],
        ["--scope", "book"],
    ],
    ids=["typo-book", "unbound-instrument", "unregistered-strategy", "no-target"],
)
def test_a_kill_on_something_the_system_does_not_know_is_refused(args: list[str]) -> None:
    result = runner.invoke(
        cli.app,
        ["control", "kill", "--operator", "ana", "--reason", "r", *args, *BINDING_ARGS],
    )

    assert result.exit_code == cli.ExitCode.CONFIGURATION
    assert _status()["halts"] == []


def test_nobody_may_act_as_the_system() -> None:
    result = runner.invoke(
        cli.app, ["control", "safe-mode", "--operator", "system", "--reason", "r"]
    )

    assert result.exit_code == cli.ExitCode.CONFIGURATION


def test_safe_mode_by_hand_is_one_halt_however_often_it_is_asked_for() -> None:
    first = _ok(["control", "safe-mode", "--operator", "ana", "--reason", "news"])
    again = _ok(["control", "safe-mode", "--operator", "ben", "--reason", "news"])

    assert again["halt"]["halt_id"] == first["halt"]["halt_id"]
    assert again["changed"] is False


# --- order submit meets them --------------------------------------------------------------


def test_a_kill_refuses_an_order_before_the_terminal_is_reached(
    database: DatabaseHarness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    halt = _kill("--scope", "strategy", "--target", "vol_breakout_eurusd_h1")["halt"]
    reached: list[str] = []
    monkeypatch.setattr(
        cli, "_mt5_terminal_factory", lambda: reached.append("terminal") or (lambda _: _Account())
    )

    result = _submit(tmp_path)

    assert result.exit_code == cli.ExitCode.TRADING_HALTED, result.stderr
    assert halt["halt_id"] in result.stderr
    assert reached == []
    assert _ledger(database).current_state("new-1") is None


def test_a_venue_refusal_of_authority_enters_safe_mode(
    database: DatabaseHarness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retcode 10027 is autotrading disabled at the client: the next order
    would be refused the same way, and spec 13.2 names it a SAFE_MODE trigger."""

    _use(
        monkeypatch,
        _Account(
            send_result=Mt5SendResult(
                retcode=10027,
                order_ticket=None,
                position_ticket=None,
                deal_ticket=None,
                volume=0.0,
                price=0.0,
                comment="AutoTrading disabled by client",
            )
        ),
    )

    result = _submit(tmp_path)

    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout)["state"] == "REJECTED"
    (halt,) = _status()["halts"]
    assert (halt["kind"], halt["scope"], halt["actor"]) == ("safe_mode", "firm", "system")
    assert halt["reason"] == "venue_refused:trade_disabled"


# --- flatten ------------------------------------------------------------------------------


def test_flatten_refuses_without_a_firm_kill(monkeypatch: pytest.MonkeyPatch) -> None:
    terminal = _Account(positions=(_position(1, 110042),))
    _use(monkeypatch, terminal)
    _kill("--scope", "book", "--target", "fx_scalp")

    result = runner.invoke(cli.app, ["control", "flatten", "--operator", "ana", *BINDING_ARGS])

    assert result.exit_code == cli.ExitCode.HALT_NOT_ACTIVE
    assert terminal.sent == []


def test_flatten_under_a_firm_kill_closes_only_the_systems_own_positions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terminal = _Account(
        positions=(_position(1, 110042), _position(2, 0)),
        send_result=Mt5SendResult(
            retcode=10009,
            order_ticket=11,
            position_ticket=1,
            deal_ticket=21,
            volume=0.1,
            price=1.09999,
            comment="done",
        ),
    )
    _use(monkeypatch, terminal)
    _kill("--scope", "firm")

    payload = _ok(["control", "flatten", "--operator", "ana", *BINDING_ARGS])

    assert [item["position_ticket"] for item in payload["closed"]] == [1]
    assert payload["closed"][0]["accepted"] is True
    (request,) = terminal.sent
    assert request["position"] == 1


# --- check: the latch a scheduler runs -----------------------------------------------------


def _deal(profit: float) -> Mt5Deal:
    return Mt5Deal(
        ticket=31,
        order_ticket=32,
        position_ticket=33,
        magic=110042,
        server_symbol="EURUSD",
        volume=0.1,
        price=1.1,
        is_buy=False,
        # Fixed and in the past, so it falls before the real clock's clear
        # whatever time of day the suite runs.
        dealt_at=datetime(2026, 1, 5, tzinfo=UTC),
        entry=1,
        profit=profit,
        commission=0.0,
        swap=0.0,
        fee=0.0,
    )


def test_check_latches_a_book_at_its_drawdown_halt_and_clearing_restarts_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """fx_scalp's halt is 6% of its 30,000 slice: 1,800. A book that has lost
    exactly that is halted, an order for it is refused, and once a person clears
    the halt the book's curve restarts -- the same loss does not latch again."""

    _use(monkeypatch, _Account(deals=(_deal(-1800.0),)))

    (latched,) = _ok(["control", "check", *BINDING_ARGS])["latched"]
    assert (latched["halt"]["kind"], latched["halt"]["target"]) == ("drawdown_halt", "fx_scalp")
    assert _submit(tmp_path).exit_code == cli.ExitCode.TRADING_HALTED

    _ok(
        [
            "control",
            "clear",
            "--halt-id",
            latched["halt"]["halt_id"],
            "--operator",
            "ben",
            "--reason",
            "reviewed the book",
        ]
    )
    assert _ok(["control", "check", *BINDING_ARGS])["latched"] == []
