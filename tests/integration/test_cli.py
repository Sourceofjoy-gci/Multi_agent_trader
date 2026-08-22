"""Operator commands against a real PostgreSQL audit ledger."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

from trading_house import cli

if TYPE_CHECKING:
    from .conftest import DatabaseHarness

pytestmark = pytest.mark.integration

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_CONFIG = str(PROJECT_ROOT / "alembic.ini")
CONFIG_DIR = PROJECT_ROOT / "config"

runner = CliRunner()


@pytest.fixture(autouse=True)
def _runtime_environment(database: DatabaseHarness, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADING_HOUSE_DATABASE_DSN", database.runtime_dsn)
    monkeypatch.setenv(
        "TRADING_HOUSE_CONSTITUTION_PATH", str(CONFIG_DIR / "risk_constitution.yaml")
    )
    monkeypatch.setenv(
        "TRADING_HOUSE_CONSTITUTION_SIGNATURE_PATH",
        str(CONFIG_DIR / "risk_constitution.yaml.sig"),
    )
    monkeypatch.setenv(
        "TRADING_HOUSE_CONSTITUTION_PUBLIC_KEY_PATH",
        str(CONFIG_DIR / "risk_constitution.public.pem"),
    )
    cli._STATE.debug = False


def test_db_check_reports_head() -> None:
    result = runner.invoke(cli.app, ["db", "check", "--alembic-config", ALEMBIC_CONFIG])

    assert result.exit_code == cli.ExitCode.OK
    assert json.loads(result.stdout)["revision"] == "head"


@pytest.mark.usefixtures("isolated_audit_ledger")
def test_audit_verify_accepts_an_empty_ledger() -> None:
    result = runner.invoke(cli.app, ["audit", "verify"])

    assert result.exit_code == cli.ExitCode.OK
    payload = json.loads(result.stdout)
    assert payload["valid"] is True
    assert payload["checked_entries"] == 0


@pytest.mark.usefixtures("isolated_audit_ledger")
def test_health_reports_ready_and_extends_the_verified_chain() -> None:
    health = runner.invoke(cli.app, ["health", "--alembic-config", ALEMBIC_CONFIG])

    assert health.exit_code == cli.ExitCode.OK
    report = json.loads(health.stdout)
    assert report["ready"] is True
    assert report["constitution_version"] == 1
    assert report["audit_entries_verified"] == 0

    verified = runner.invoke(cli.app, ["audit", "verify"])
    assert verified.exit_code == cli.ExitCode.OK
    assert json.loads(verified.stdout)["checked_entries"] == 2


@pytest.mark.usefixtures("isolated_audit_ledger")
def test_health_output_never_contains_credentials(database: DatabaseHarness) -> None:
    result = runner.invoke(cli.app, ["health", "--alembic-config", ALEMBIC_CONFIG])

    rendered = result.stdout + result.stderr
    assert "integration-runtime-password" not in rendered
    assert database.runtime_dsn not in rendered


def test_unreachable_database_returns_the_database_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "TRADING_HOUSE_DATABASE_DSN", "postgresql://nobody:nothing@127.0.0.1:1/absent"
    )

    result = runner.invoke(cli.app, ["db", "check", "--alembic-config", ALEMBIC_CONFIG])

    assert result.exit_code == cli.ExitCode.DATABASE
    assert "nothing" not in result.stdout + result.stderr


def test_constitution_verify_needs_no_database() -> None:
    result = runner.invoke(cli.app, ["constitution", "verify"])

    assert result.exit_code == cli.ExitCode.OK
    assert json.loads(result.stdout)["constitution_version"] == 1
