"""Phase 0 acceptance: the whole foundation, end to end, on a real database.

This is the gate Phase 1 may not start without. It exercises the migrator and
runtime authority separation, the checked-in signed constitution, a runtime
append, independent chain verification, the readiness gate, and the absence of
any trading or agent dependency.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import NAMESPACE_URL, uuid5

import psycopg
import pytest
from pydantic import SecretStr

from trading_house import __version__
from trading_house.audit.models import AuditEvent
from trading_house.audit.repository import PostgresAuditLedger
from trading_house.constitution.loader import load_constitution
from trading_house.core.clock import SystemClock
from trading_house.core.errors import SignatureVerificationError
from trading_house.database.connection import open_runtime_connection
from trading_house.database.migrations import assert_at_head
from trading_house.ops.health import HealthService

if TYPE_CHECKING:
    from ..conftest import DatabaseHarness

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("isolated_audit_ledger"),
]

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = PROJECT_ROOT / "config"
CONSTITUTION = CONFIG_DIR / "risk_constitution.yaml"
SIGNATURE = CONFIG_DIR / "risk_constitution.yaml.sig"
PUBLIC_KEY = CONFIG_DIR / "risk_constitution.public.pem"
VENUE_BINDING = CONFIG_DIR / "venue_binding.mt5.yaml"
VENUE_BINDING_SIGNATURE = CONFIG_DIR / "venue_binding.mt5.yaml.sig"
FORBIDDEN_TOP_LEVEL_IMPORTS = frozenset({"MetaTrader5", "langgraph", "openai", "anthropic", "ccxt"})


def _runtime_factory(database: DatabaseHarness):
    return lambda: open_runtime_connection(SecretStr(database.runtime_dsn))


def _fixture_event() -> AuditEvent:
    return AuditEvent(
        schema_version=1,
        event_id=uuid5(NAMESPACE_URL, "phase-0-acceptance"),
        event_type="acceptance.fixture",
        occurred_at=datetime(2026, 8, 22, 9, 0, tzinfo=UTC),
        actor="acceptance-suite",
        actor_type="test",
        payload={"phase": 0, "trading": False},
        source_component="acceptance",
    )


def test_checked_in_verification_artifacts_exist() -> None:
    """A missing signature or public key must fail loudly, not be skipped."""

    assert CONSTITUTION.is_file()
    assert SIGNATURE.is_file()
    assert PUBLIC_KEY.is_file()
    assert VENUE_BINDING.is_file()
    assert VENUE_BINDING_SIGNATURE.is_file()
    assert not list(CONFIG_DIR.glob("*private*")), "no private key may be checked in"


def test_phase0_foundation_accepts_end_to_end(database: DatabaseHarness) -> None:
    # 1. Migrations were applied with migrator credentials by the harness.
    with psycopg.connect(database.runtime_dsn) as connection:
        assert_at_head(connection, database.alembic_config)

    # 2. The checked-in constitution verifies against the checked-in key.
    loaded = load_constitution(CONSTITUTION, SIGNATURE, PUBLIC_KEY)
    assert loaded.constitution.version == 1
    assert loaded.constitution.signature_required is True

    # 3. The runtime role can append through the security-definer function.
    ledger = PostgresAuditLedger(_runtime_factory(database))
    record = ledger.append(_fixture_event())
    assert record.sequence_number == 1

    # 4. The chain verifies independently in Python.
    first = ledger.verify()
    assert first.valid is True
    assert first.checked_entries == 1

    # 5. The readiness gate runs and reports ready.
    def _connect() -> Any:
        return open_runtime_connection(SecretStr(database.runtime_dsn))

    report = HealthService(
        load_constitution=lambda: load_constitution(CONSTITUTION, SIGNATURE, PUBLIC_KEY),
        open_connection=_connect,
        assert_revision=lambda conn: assert_at_head(conn, database.alembic_config),
        ledger=ledger,
        clock=SystemClock(),
        application_version=__version__,
    ).run()
    assert report.ready is True
    assert report.constitution_sha256 == loaded.constitution_sha256
    assert report.public_key_fingerprint == loaded.public_key_fingerprint

    # 6. The chain still verifies after the gate appended its two events.
    final = ledger.verify()
    assert final.valid is True
    assert final.checked_entries == 3


def test_runtime_role_cannot_write_the_ledger_directly(database: DatabaseHarness) -> None:
    """Append authority exists only through the audited function."""

    with (
        psycopg.connect(database.runtime_dsn) as connection,
        connection.cursor() as cursor,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        cursor.execute("INSERT INTO audit.ledger (sequence_number) VALUES (99)")


def test_a_missing_signature_prevents_startup(tmp_path: Path) -> None:
    with pytest.raises(SignatureVerificationError):
        load_constitution(CONSTITUTION, tmp_path / "absent.sig", PUBLIC_KEY)


def test_phase0_ships_no_trading_or_agent_dependency() -> None:
    declared = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8").lower()
    for package in FORBIDDEN_TOP_LEVEL_IMPORTS:
        assert package.lower() not in declared


def test_no_source_module_imports_a_trading_or_agent_package() -> None:
    offenders: list[str] = []
    for path in sorted((PROJECT_ROOT / "src" / "trading_house").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module.split(".")[0]]
            else:
                continue
            if set(names) & FORBIDDEN_TOP_LEVEL_IMPORTS:
                offenders.append(path.relative_to(PROJECT_ROOT).as_posix())

    assert offenders == []
