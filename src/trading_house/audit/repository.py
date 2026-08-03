"""Transactional PostgreSQL persistence for the append-only audit ledger."""

from __future__ import annotations

from typing import Any, Protocol

import psycopg
from psycopg.types.json import Jsonb

from trading_house.audit.canonical import canonicalize_event
from trading_house.audit.models import AuditEvent, AuditRecord
from trading_house.core.errors import AuditAppendError


class ConnectionFactory(Protocol):
    """Open one distinct runtime connection for a repository operation."""

    def __call__(self) -> psycopg.Connection[tuple[Any, ...]]: ...


class _AuditRepositoryFailure(Exception):
    """Credential- and event-free diagnostic cause for repository failures."""


def _record_from_row(row: tuple[Any, ...] | None) -> AuditRecord:
    if row is None or len(row) != 7:
        raise ValueError("audit database returned an invalid row")
    return AuditRecord(
        sequence_number=row[0],
        event_id=row[1],
        canonical_event=row[2],
        event_json=row[3],
        previous_hash=row[4],
        entry_hash=row[5],
        received_at=row[6],
    )


class PostgresAuditLedger:
    """Append and read immutable audit records through PostgreSQL transactions."""

    def __init__(self, connection_factory: ConnectionFactory) -> None:
        self._connection_factory = connection_factory

    def append(self, event: AuditEvent) -> AuditRecord:
        """Atomically append one canonical event and return its persisted row."""

        try:
            canonical_event = canonicalize_event(event)
            event_json = event.model_dump(mode="json")
            with (
                self._connection_factory() as connection,
                connection.cursor() as cursor,
            ):
                cursor.execute(
                    "SELECT * FROM audit.append_event(%s, %s)",
                    (canonical_event, Jsonb(event_json)),
                )
                record = _record_from_row(cursor.fetchone())
            return record
        except Exception:
            diagnostic = _AuditRepositoryFailure("audit repository operation failed")
        raise AuditAppendError() from diagnostic

    def records(self) -> tuple[AuditRecord, ...]:
        """Return the ordered ledger from a short, read-only transaction."""

        try:
            with (
                self._connection_factory() as connection,
                connection.cursor() as cursor,
            ):
                cursor.execute("SET TRANSACTION READ ONLY")
                cursor.execute(
                    "SELECT sequence_number, event_id, canonical_event, event_json, "
                    "previous_hash, entry_hash, received_at "
                    "FROM audit.ledger ORDER BY sequence_number"
                )
                records = tuple(_record_from_row(row) for row in cursor.fetchall())
            return records
        except Exception:
            diagnostic = _AuditRepositoryFailure("audit repository operation failed")
        raise AuditAppendError() from diagnostic
