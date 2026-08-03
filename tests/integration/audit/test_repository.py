from __future__ import annotations

import traceback
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from itertools import pairwise
from threading import Lock
from typing import TYPE_CHECKING, Any
from uuid import NAMESPACE_URL, UUID, uuid5

import psycopg
import pytest
from psycopg.types.json import Jsonb
from pydantic import SecretStr

import trading_house.audit.repository as repository_module
from trading_house.audit.canonical import GENESIS_HASH, canonicalize_event
from trading_house.audit.models import AuditEvent, AuditRecord
from trading_house.audit.repository import PostgresAuditLedger
from trading_house.core.errors import AuditAppendError
from trading_house.database.connection import open_runtime_connection

if TYPE_CHECKING:
    from ..conftest import DatabaseHarness

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("isolated_audit_ledger"),
]


def _event(index: int = 0, *, event_id: UUID | None = None) -> AuditEvent:
    return AuditEvent(
        schema_version=1,
        event_id=event_id or uuid5(NAMESPACE_URL, f"audit-repository-event-{index}"),
        event_type="integration.audit",
        occurred_at=datetime(2026, 8, 3, 12, index % 60, tzinfo=UTC),
        actor="integration-suite",
        actor_type="test",
        correlation_id=None,
        causation_id=None,
        payload={"index": index, "accepted": True},
        source_component="audit-tests",
        subject_id=f"subject-{index}",
    )


def _runtime_factory(database: DatabaseHarness) -> Callable[[], psycopg.Connection[Any]]:
    return lambda: open_runtime_connection(SecretStr(database.runtime_dsn))


def _exception_graph(error: BaseException) -> tuple[BaseException, ...]:
    pending = [error]
    seen: set[int] = set()
    graph: list[BaseException] = []
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        graph.append(current)
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)
    return tuple(graph)


def _assert_redacted(error: AuditAppendError, *markers: str) -> None:
    assert error.args == ("audit append failed",)
    assert type(error) is AuditAppendError
    graph = _exception_graph(error)
    assert len(graph) == 2
    assert not any(isinstance(item, psycopg.Error) for item in graph)
    rendered = "\n".join(
        [
            *(str(item) for item in graph),
            *(repr(item) for item in graph),
            "".join(traceback.format_exception(error)),
        ]
    )
    for marker in markers:
        assert marker not in rendered


class _RecordingCursor:
    def __init__(self, connection: _RecordingConnection, row: tuple[object, ...]) -> None:
        self._connection = connection
        self._row = row

    def __enter__(self) -> _RecordingCursor:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def execute(self, query: str, params: object = None) -> None:
        self._connection.executions.append((query, params))

    def fetchone(self) -> tuple[object, ...]:
        return self._row


class _RecordingConnection:
    def __init__(self, row: tuple[object, ...]) -> None:
        self._row = row
        self.executions: list[tuple[str, object]] = []
        self.closed = False
        self.context_exits = 0

    def __enter__(self) -> _RecordingConnection:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.context_exits += 1
        self.closed = True

    def cursor(self) -> _RecordingCursor:
        return _RecordingCursor(self, self._row)

    def commit(self) -> None:
        raise AssertionError("repository must commit only through the connection context")


def test_append_canonicalizes_once_and_uses_the_exact_database_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = _event()
    canonical_bytes = b"one canonicalization result"
    received_at = datetime(2026, 8, 3, 12, 30, tzinfo=UTC)
    row = (
        7,
        event.event_id,
        canonical_bytes,
        event.model_dump(mode="json"),
        b"p" * 32,
        b"e" * 32,
        received_at,
    )
    connection = _RecordingConnection(row)
    calls = 0

    def canonicalize_once(supplied_event: AuditEvent) -> bytes:
        nonlocal calls
        assert supplied_event is event
        calls += 1
        return canonical_bytes

    monkeypatch.setattr(repository_module, "canonicalize_event", canonicalize_once)

    record = PostgresAuditLedger(lambda: connection).append(event)

    assert calls == 1
    assert connection.context_exits == 1
    assert connection.closed
    assert len(connection.executions) == 1
    query, params = connection.executions[0]
    assert query == "SELECT * FROM audit.append_event(%s, %s)"
    assert isinstance(params, tuple)
    assert params[0] is canonical_bytes
    assert type(params[1]) is Jsonb
    assert params[1].obj == event.model_dump(mode="json")
    assert record == AuditRecord(
        sequence_number=7,
        event_id=event.event_id,
        canonical_event=canonical_bytes,
        event_json=event.model_dump(mode="json"),
        previous_hash=b"p" * 32,
        entry_hash=b"e" * 32,
        received_at=received_at,
    )


def test_append_returns_the_exact_persisted_genesis_record(database: DatabaseHarness) -> None:
    event = _event()
    ledger = PostgresAuditLedger(_runtime_factory(database))

    record = ledger.append(event)

    assert record.sequence_number == 1
    assert record.event_id == event.event_id
    assert record.canonical_event == canonicalize_event(event)
    assert record.event_json == event.model_dump(mode="json")
    assert record.previous_hash == GENESIS_HASH
    assert len(record.entry_hash) == 32
    assert record.received_at.utcoffset() == UTC.utcoffset(record.received_at)
    assert ledger.records() == (record,)


def test_duplicate_uuid_rolls_back_closes_and_redacts_the_full_exception_graph(
    database: DatabaseHarness,
) -> None:
    connections: list[psycopg.Connection[Any]] = []

    def factory() -> psycopg.Connection[Any]:
        connection = open_runtime_connection(SecretStr(database.runtime_dsn))
        connections.append(connection)
        return connection

    event = _event()
    ledger = PostgresAuditLedger(factory)
    first = ledger.append(event)

    with pytest.raises(AuditAppendError) as raised:
        ledger.append(event)

    failed_connection = connections[-1]
    _assert_redacted(raised.value, str(event.event_id), database.runtime_dsn)
    assert failed_connection.closed
    assert ledger.records() == (first,)


def test_malformed_canonical_bytes_roll_back_and_close_without_partial_row(
    database: DatabaseHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connections: list[psycopg.Connection[Any]] = []

    def factory() -> psycopg.Connection[Any]:
        connection = open_runtime_connection(SecretStr(database.runtime_dsn))
        connections.append(connection)
        return connection

    event = _event()
    ledger = PostgresAuditLedger(factory)
    monkeypatch.setattr(repository_module, "canonicalize_event", lambda _: b"malformed bytes")

    with pytest.raises(AuditAppendError) as raised:
        ledger.append(event)

    failed_connection = connections[-1]
    _assert_redacted(raised.value, str(event.event_id), database.runtime_dsn, "malformed bytes")
    assert failed_connection.closed
    assert ledger.records() == ()


def test_permission_failure_is_redacted_and_does_not_append(
    database: DatabaseHarness,
) -> None:
    with psycopg.connect(database.migration_dsn) as connection, connection.cursor() as cursor:
        cursor.execute("SET ROLE trading_house_owner")
        cursor.execute(
            "REVOKE EXECUTE ON FUNCTION audit.append_event(BYTEA, JSONB) FROM trading_house_runtime"
        )

    event = _event()
    ledger = PostgresAuditLedger(_runtime_factory(database))
    with pytest.raises(AuditAppendError) as raised:
        ledger.append(event)

    _assert_redacted(raised.value, str(event.event_id), database.runtime_dsn, "append_event")
    assert ledger.records() == ()


def test_connection_failure_is_redacted_without_retaining_the_driver_error() -> None:
    marker = "sensitive-connection-diagnostic"

    def failing_factory() -> psycopg.Connection[Any]:
        raise psycopg.OperationalError(marker)

    with pytest.raises(AuditAppendError) as raised:
        PostgresAuditLedger(failing_factory).append(_event())

    _assert_redacted(raised.value, marker)


def test_records_returns_an_immutable_ordered_result_after_closing_connection(
    database: DatabaseHarness,
) -> None:
    connections: list[psycopg.Connection[Any]] = []

    def factory() -> psycopg.Connection[Any]:
        connection = open_runtime_connection(SecretStr(database.runtime_dsn))
        connections.append(connection)
        return connection

    ledger = PostgresAuditLedger(factory)
    expected = tuple(ledger.append(_event(index)) for index in range(3))

    records = ledger.records()
    read_connection = connections[-1]

    assert type(records) is tuple
    assert records == expected
    assert [record.sequence_number for record in records] == [1, 2, 3]
    assert read_connection.closed


def test_concurrent_appends_form_one_continuous_chain_on_distinct_connections(
    database: DatabaseHarness,
) -> None:
    connections: list[psycopg.Connection[Any]] = []
    connection_lock = Lock()

    def factory() -> psycopg.Connection[Any]:
        connection = open_runtime_connection(SecretStr(database.runtime_dsn))
        with connection_lock:
            connections.append(connection)
        return connection

    ledger = PostgresAuditLedger(factory)
    with ThreadPoolExecutor(max_workers=8) as pool:
        appended = list(pool.map(lambda index: ledger.append(_event(index)), range(20)))

    append_connections = tuple(connections)
    ordered = tuple(sorted(appended, key=lambda record: record.sequence_number))

    assert len(append_connections) == 20
    assert len({id(connection) for connection in append_connections}) == 20
    assert all(connection.closed for connection in append_connections)
    assert [record.sequence_number for record in ordered] == list(range(1, 21))
    assert ordered[0].previous_hash == GENESIS_HASH
    assert all(right.previous_hash == left.entry_hash for left, right in pairwise(ordered))
    assert ledger.records() == ordered
