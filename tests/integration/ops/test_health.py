"""The readiness gate against a real PostgreSQL audit ledger."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import psycopg
import pytest
from pydantic import SecretStr

from trading_house.audit.repository import PostgresAuditLedger
from trading_house.brokers.base import ReconciliationReport
from trading_house.constitution.loader import load_constitution
from trading_house.core.clock import SystemClock
from trading_house.core.errors import (
    AuditIntegrityError,
    DatabaseUnavailableError,
    SignatureVerificationError,
)
from trading_house.core.values import BookId
from trading_house.core.venue import Mt5VenueRef, Venue
from trading_house.database.connection import open_runtime_connection
from trading_house.database.migrations import assert_at_head
from trading_house.ops.health import HealthService
from trading_house.settings import RuntimeSettings

if TYPE_CHECKING:
    from ...conftest import DatabaseHarness

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("isolated_audit_ledger"),
]

PROJECT_ROOT = Path(__file__).resolve().parents[3]
UNREACHABLE_DSN = "postgresql://nobody:nothing@127.0.0.1:1/absent"


def _settings(dsn: str, *, signature_name: str = "risk_constitution.yaml.sig") -> RuntimeSettings:
    config_dir = PROJECT_ROOT / "config"
    return RuntimeSettings(
        database_dsn=SecretStr(dsn),
        constitution_path=config_dir / "risk_constitution.yaml",
        constitution_signature_path=config_dir / signature_name,
        constitution_public_key_path=config_dir / "risk_constitution.public.pem",
    )


def _reconciliation(book: BookId, tickets: tuple[int, ...]) -> ReconciliationReport:
    """A venue report shaped the way Phase 1 actually produces them.

    ``positions`` is always empty until Phase 3's intent ledger exists, so
    everything the venue holds arrives as an unmatched ref.
    """

    return ReconciliationReport(
        book=book,
        positions=(),
        unmatched_venue_refs=tuple(
            Mt5VenueRef(venue=Venue.MT5, magic=0, server_symbol="EURUSD", position_ticket=ticket)
            for ticket in tickets
        ),
        reconciled_at=datetime.now(UTC),
    )


def _service(
    database: DatabaseHarness,
    settings: RuntimeSettings,
    *,
    reconcile_books: Any = None,
) -> HealthService:
    def _connect() -> Any:
        return open_runtime_connection(settings.database_dsn)

    def _revision(connection: Any) -> None:
        assert_at_head(connection, database.alembic_config)

    def _no_venue() -> Mapping[BookId, ReconciliationReport]:
        return {}

    return HealthService(
        load_constitution=lambda: load_constitution(
            settings.constitution_path,
            settings.constitution_signature_path,
            settings.constitution_public_key_path,
        ),
        open_connection=_connect,
        assert_revision=_revision,
        ledger=PostgresAuditLedger(_connect),
        reconcile_books=reconcile_books or _no_venue,
        clock=SystemClock(),
        application_version="0.1.0",
    )


def _ledger(database: DatabaseHarness) -> PostgresAuditLedger:
    return PostgresAuditLedger(lambda: open_runtime_connection(SecretStr(database.runtime_dsn)))


def test_gate_reports_ready_and_appends_exactly_two_events(database: DatabaseHarness) -> None:
    report = _service(database, _settings(database.runtime_dsn)).run()

    assert report.ready is True
    assert report.constitution_version == 1
    assert len(report.constitution_sha256) == 64
    assert len(report.public_key_fingerprint) == 64
    assert report.audit_entries_verified == 0

    records = _ledger(database).records()
    assert len(records) == 2
    assert [record.sequence_number for record in records] == [1, 2]


def test_appended_events_are_the_startup_pair(database: DatabaseHarness) -> None:
    _service(database, _settings(database.runtime_dsn)).run()

    records = _ledger(database).records()
    event_types = [record.event_json["event_type"] for record in records]  # type: ignore[index]
    assert event_types == ["startup", "constitution_loaded"]


def test_chain_verifies_after_the_gate_runs(database: DatabaseHarness) -> None:
    _service(database, _settings(database.runtime_dsn)).run()

    integrity = _ledger(database).verify()

    assert integrity.valid is True
    assert integrity.checked_entries == 2
    assert integrity.first_invalid_sequence is None


def test_running_the_gate_twice_extends_a_valid_chain(database: DatabaseHarness) -> None:
    service = _service(database, _settings(database.runtime_dsn))

    first = service.run()
    second = service.run()

    assert first.audit_entries_verified == 0
    assert second.audit_entries_verified == 2

    integrity = _ledger(database).verify()
    assert integrity.valid is True
    assert integrity.checked_entries == 4


def test_unreachable_database_fails_the_gate(database: DatabaseHarness) -> None:
    with pytest.raises(DatabaseUnavailableError):
        _service(database, _settings(UNREACHABLE_DSN)).run()

    assert _ledger(database).records() == ()


def test_missing_signature_fails_before_any_database_work(database: DatabaseHarness) -> None:
    settings = _settings(database.runtime_dsn, signature_name="absent.sig")

    with pytest.raises(SignatureVerificationError):
        _service(database, settings).run()

    assert _ledger(database).records() == ()


def test_gate_failure_message_never_exposes_the_dsn(database: DatabaseHarness) -> None:
    with pytest.raises(DatabaseUnavailableError) as caught:
        _service(database, _settings(UNREACHABLE_DSN)).run()

    assert "nothing" not in str(caught.value)
    assert str(caught.value) == "database connection failed"


@contextmanager
def _administrative_tamper(database: DatabaseHarness) -> Iterator[psycopg.Cursor[Any]]:
    """Temporarily bypass only the row-mutation trigger, restoring it unconditionally."""

    with (
        psycopg.connect(database.test_superuser_dsn, autocommit=True) as connection,
        connection.cursor() as cursor,
    ):
        cursor.execute("ALTER TABLE audit.ledger DISABLE TRIGGER reject_ledger_row_mutation")
        try:
            yield cursor
        finally:
            cursor.execute("ALTER TABLE audit.ledger ENABLE TRIGGER reject_ledger_row_mutation")


def test_detected_tampering_prevents_ready_and_appends_nothing(
    database: DatabaseHarness,
) -> None:
    """Definition of Done: audit corruption is detected *before* ready status."""

    service = _service(database, _settings(database.runtime_dsn))
    service.run()
    assert len(_ledger(database).records()) == 2

    with _administrative_tamper(database) as cursor:
        cursor.execute(
            "UPDATE audit.ledger "
            "SET event_json = pg_catalog.jsonb_set(event_json, '{actor}', '\"attacker\"') "
            "WHERE sequence_number = 1"
        )

    with pytest.raises(AuditIntegrityError):
        service.run()

    records = _ledger(database).records()
    assert len(records) == 2, "a failed gate must not append startup events"
    assert _ledger(database).verify().valid is False


def test_venue_reconciliation_reaches_the_report_without_disturbing_the_chain(
    database: DatabaseHarness,
) -> None:
    """A manually opened position belongs to no book, so every book's report
    carries it. Counting report lengths would report one trade as two."""

    shared_orphan = 88_001

    def reconcile() -> Mapping[BookId, ReconciliationReport]:
        return {
            "fx_scalp": _reconciliation("fx_scalp", (shared_orphan, 110_042)),
            "fx_swing": _reconciliation("fx_swing", (shared_orphan,)),
        }

    report = _service(database, _settings(database.runtime_dsn), reconcile_books=reconcile).run()

    assert report.ready is True
    assert report.books_reconciled == ("fx_scalp", "fx_swing")
    assert report.open_positions == 2
    _ledger(database).verify()


def test_a_venue_holding_positions_never_blocks_readiness(database: DatabaseHarness) -> None:
    """Phase 1 owns no positions and has no ledger to compare against, so a
    demo account with open trades must not make health permanently red."""

    def reconcile() -> Mapping[BookId, ReconciliationReport]:
        return {"fx_scalp": _reconciliation("fx_scalp", (1, 2, 3))}

    report = _service(database, _settings(database.runtime_dsn), reconcile_books=reconcile).run()

    assert report.ready is True
    assert report.open_positions == 3
