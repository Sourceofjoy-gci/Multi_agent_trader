from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import NAMESPACE_URL, uuid5

import psycopg
import pytest
from pydantic import SecretStr

from trading_house.audit.models import AuditEvent
from trading_house.audit.repository import PostgresAuditLedger
from trading_house.database.connection import open_runtime_connection

if TYPE_CHECKING:
    from ..conftest import DatabaseHarness

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("isolated_audit_ledger"),
]


def _event(index: int) -> AuditEvent:
    return AuditEvent(
        schema_version=1,
        event_id=uuid5(NAMESPACE_URL, f"audit-tamper-event-{index}"),
        event_type="integration.tamper",
        occurred_at=datetime(2026, 8, 3, 14, index, tzinfo=UTC),
        actor="tamper-suite",
        actor_type="test",
        correlation_id=None,
        causation_id=None,
        payload={"accepted": True, "index": index},
        source_component="audit-tests",
        subject_id=f"tamper-{index}",
    )


def _runtime_factory(database: DatabaseHarness) -> Callable[[], psycopg.Connection[Any]]:
    return lambda: open_runtime_connection(SecretStr(database.runtime_dsn))


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


def _assert_mutation_trigger_enabled(database: DatabaseHarness) -> None:
    with (
        psycopg.connect(database.test_superuser_dsn) as connection,
        connection.cursor() as cursor,
    ):
        cursor.execute(
            "SELECT tgenabled FROM pg_catalog.pg_trigger "
            "WHERE tgrelid = 'audit.ledger'::regclass "
            "AND tgname = 'reject_ledger_row_mutation'"
        )
        assert cursor.fetchone() == ("O",)


@pytest.mark.parametrize(
    ("tamper_sql", "parameters", "invalid_sequence", "checked_entries", "reason"),
    [
        (
            "UPDATE audit.ledger SET event_json = "
            "jsonb_set(event_json, '{payload,accepted}', 'false'::jsonb) "
            "WHERE sequence_number = 2",
            (),
            2,
            1,
            "event_json_mismatch",
        ),
        (
            "UPDATE audit.ledger SET canonical_event = decode('ff', 'hex') "
            "WHERE sequence_number = 2",
            (),
            2,
            1,
            "canonical_utf8_invalid",
        ),
        (
            "UPDATE audit.ledger SET previous_hash = %s WHERE sequence_number = 2",
            (b"p" * 32,),
            2,
            1,
            "previous_hash_mismatch",
        ),
        (
            "UPDATE audit.ledger SET entry_hash = %s WHERE sequence_number = 2",
            (b"e" * 32,),
            2,
            1,
            "entry_hash_mismatch",
        ),
        (
            "DELETE FROM audit.ledger WHERE sequence_number = 2",
            (),
            3,
            1,
            "sequence_gap",
        ),
    ],
    ids=["stored-payload", "canonical-bytes", "previous-link", "entry-hash", "deleted-row"],
)
def test_administrative_corruption_is_detected_after_trigger_restoration(
    database: DatabaseHarness,
    tamper_sql: str,
    parameters: tuple[object, ...],
    invalid_sequence: int,
    checked_entries: int,
    reason: str,
) -> None:
    ledger = PostgresAuditLedger(_runtime_factory(database))
    for index in range(1, 4):
        ledger.append(_event(index))

    with _administrative_tamper(database) as cursor:
        cursor.execute(tamper_sql, parameters)

    _assert_mutation_trigger_enabled(database)
    report = ledger.verify()
    assert not report.valid
    assert report.first_invalid_sequence == invalid_sequence
    assert report.checked_entries == checked_entries
    assert report.reason == reason


def test_untampered_postgres_ledger_verifies_as_valid(database: DatabaseHarness) -> None:
    ledger = PostgresAuditLedger(_runtime_factory(database))
    for index in range(1, 4):
        ledger.append(_event(index))

    report = ledger.verify()

    assert report.valid
    assert report.checked_entries == 3
