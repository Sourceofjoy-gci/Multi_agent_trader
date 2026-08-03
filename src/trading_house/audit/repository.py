"""Transactional PostgreSQL persistence for the append-only audit ledger."""

from __future__ import annotations

from typing import Any, Never, Protocol

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


class _OperationFailure:
    """Non-sensitive sentinel returned after discarding an operation failure."""


_OPERATION_FAILED = _OperationFailure()


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


def _close_connection(connection: psycopg.Connection[tuple[Any, ...]]) -> bool:
    try:
        connection.close()
    except Exception:
        return False
    return True


def _append_operation(
    connection_factory: ConnectionFactory,
    event: AuditEvent,
) -> AuditRecord | _OperationFailure:
    connection: psycopg.Connection[tuple[Any, ...]] | None = None
    outcome: AuditRecord | _OperationFailure = _OPERATION_FAILED
    try:
        canonical_event = canonicalize_event(event)
        event_json = event.model_dump(mode="json")
        connection = connection_factory()
        with connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM audit.append_event(%s, %s)",
                (canonical_event, Jsonb(event_json)),
            )
            outcome = _record_from_row(cursor.fetchone())
    except Exception:
        outcome = _OPERATION_FAILED
    finally:
        if connection is not None and not _close_connection(connection):
            outcome = _OPERATION_FAILED
    return outcome


def _records_operation(
    connection_factory: ConnectionFactory,
) -> tuple[AuditRecord, ...] | _OperationFailure:
    connection: psycopg.Connection[tuple[Any, ...]] | None = None
    outcome: tuple[AuditRecord, ...] | _OperationFailure = _OPERATION_FAILED
    try:
        connection = connection_factory()
        with connection, connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute(
                "SELECT sequence_number, event_id, canonical_event, event_json, "
                "previous_hash, entry_hash, received_at "
                "FROM audit.ledger ORDER BY sequence_number"
            )
            outcome = tuple(_record_from_row(row) for row in cursor.fetchall())
    except Exception:
        outcome = _OPERATION_FAILED
    finally:
        if connection is not None and not _close_connection(connection):
            outcome = _OPERATION_FAILED
    return outcome


def _raise_audit_append_error() -> Never:
    raise AuditAppendError() from _AuditRepositoryFailure("audit repository operation failed")


class PostgresAuditLedger:
    """Append and read immutable audit records through PostgreSQL transactions."""

    def __init__(self, connection_factory: ConnectionFactory) -> None:
        self._connection_factory = connection_factory

    def append(self, event: AuditEvent) -> AuditRecord:
        """Atomically append one canonical event and return its persisted row."""

        outcome = _append_operation(self._connection_factory, event)
        del self, event
        if isinstance(outcome, _OperationFailure):
            _raise_audit_append_error()
        return outcome

    def records(self) -> tuple[AuditRecord, ...]:
        """Return the ordered ledger from a short, read-only transaction."""

        outcome = _records_operation(self._connection_factory)
        del self
        if isinstance(outcome, _OperationFailure):
            _raise_audit_append_error()
        return outcome
