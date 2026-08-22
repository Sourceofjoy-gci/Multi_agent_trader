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
from trading_house.core.errors import AuditAppendError, SchemaValidationError
from trading_house.database.connection import open_runtime_connection

if TYPE_CHECKING:
    from ...conftest import DatabaseHarness

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
    traceback_with_locals = traceback.TracebackException.from_exception(error, capture_locals=True)
    frame_locals: list[str] = []
    repository_locals: list[dict[str, object]] = []
    for item in graph:
        current_traceback = item.__traceback__
        while current_traceback is not None:
            locals_snapshot = current_traceback.tb_frame.f_locals
            frame_locals.append(repr(locals_snapshot))
            if current_traceback.tb_frame.f_code.co_filename.endswith("repository.py"):
                repository_locals.append(locals_snapshot)
            current_traceback = current_traceback.tb_next

    rendered = "\n".join(
        [
            *(str(item) for item in graph),
            *(repr(item) for item in graph),
            "".join(traceback.format_exception(error)),
            "".join(traceback_with_locals.format()),
            *frame_locals,
        ]
    )
    for marker in markers:
        assert marker not in rendered
    forbidden_repository_locals = {
        "self",
        "event",
        "canonical_event",
        "event_json",
        "connection",
        "cursor",
        "record",
        "records",
        "row",
        "params",
    }
    assert all(
        forbidden_repository_locals.isdisjoint(locals_snapshot)
        for locals_snapshot in repository_locals
    )


def _capture_append_failure(ledger: PostgresAuditLedger, event: AuditEvent) -> AuditAppendError:
    try:
        ledger.append(event)
    except AuditAppendError as error:
        del ledger, event
        return error
    raise AssertionError("append unexpectedly succeeded")


def _capture_records_failure(ledger: PostgresAuditLedger) -> AuditAppendError:
    try:
        ledger.records()
    except AuditAppendError as error:
        del ledger
        return error
    raise AssertionError("records unexpectedly succeeded")


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
        self.close_calls = 0

    def __enter__(self) -> _RecordingConnection:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.context_exits += 1
        self.closed = True

    def cursor(self) -> _RecordingCursor:
        return _RecordingCursor(self, self._row)

    def commit(self) -> None:
        raise AssertionError("repository must commit only through the connection context")

    def rollback(self) -> None:
        raise AssertionError("repository must roll back only through the connection context")

    def close(self) -> None:
        self.close_calls += 1
        self.closed = True


class _LifecycleCursor:
    def __init__(
        self,
        connection: _LifecycleConnection,
        *,
        row: tuple[object, ...],
        rows: list[tuple[object, ...]],
        fail_execute_at: int | None = None,
    ) -> None:
        self._connection = connection
        self._row = row
        self._rows = rows
        self._fail_execute_at = fail_execute_at

    def __enter__(self) -> _LifecycleCursor:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def execute(self, query: str, params: object = None) -> None:
        self._connection.executions.append((query, params))
        if self._fail_execute_at == len(self._connection.executions):
            raise psycopg.DataError(self._connection.driver_marker)

    def fetchone(self) -> tuple[object, ...]:
        return self._row

    def fetchall(self) -> list[tuple[object, ...]]:
        return self._rows


class _LifecycleConnection:
    def __init__(
        self,
        *,
        row: tuple[object, ...],
        rows: list[tuple[object, ...]] | None = None,
        fail_enter: bool = False,
        fail_exit: bool = False,
        fail_close: bool = False,
        fail_execute_at: int | None = None,
        marker: str = "sensitive-lifecycle-driver-marker",
    ) -> None:
        self._row = row
        self._rows = rows or []
        self._fail_enter = fail_enter
        self._fail_exit = fail_exit
        self._fail_close = fail_close
        self._fail_execute_at = fail_execute_at
        self.driver_marker = marker
        self.executions: list[tuple[str, object]] = []
        self.exit_exception_types: list[type[BaseException] | None] = []
        self.close_calls = 0

    def __enter__(self) -> _LifecycleConnection:
        if self._fail_enter:
            raise psycopg.OperationalError(self.driver_marker)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        exc_traceback: object,
    ) -> None:
        del exc_value, exc_traceback
        self.exit_exception_types.append(exc_type)
        if self._fail_exit:
            raise psycopg.OperationalError(self.driver_marker)

    def cursor(self) -> _LifecycleCursor:
        return _LifecycleCursor(
            self,
            row=self._row,
            rows=self._rows,
            fail_execute_at=self._fail_execute_at,
        )

    def commit(self) -> None:
        raise AssertionError("repository must commit only through the connection context")

    def rollback(self) -> None:
        raise AssertionError("repository must roll back only through the connection context")

    def close(self) -> None:
        self.close_calls += 1
        if self._fail_close:
            raise psycopg.OperationalError(self.driver_marker)


def _row(event: AuditEvent, marker: bytes = b"canonical-row") -> tuple[object, ...]:
    return (
        1,
        event.event_id,
        marker,
        event.model_dump(mode="json"),
        b"p" * 32,
        b"e" * 32,
        datetime(2026, 8, 3, 12, 30, tzinfo=UTC),
    )


@pytest.mark.parametrize("operation", ["append", "records"])
@pytest.mark.parametrize("failure", ["enter", "exit", "close"])
def test_connection_lifecycle_failures_always_attempt_a_fallback_close(
    operation: str,
    failure: str,
) -> None:
    event = _event()
    marker = f"sensitive-{operation}-{failure}-marker"
    connection = _LifecycleConnection(
        row=_row(event),
        fail_enter=failure == "enter",
        fail_exit=failure == "exit",
        fail_close=failure == "close",
        marker=marker,
    )
    ledger = PostgresAuditLedger(lambda: connection)

    error = (
        _capture_append_failure(ledger, event)
        if operation == "append"
        else _capture_records_failure(ledger)
    )

    _assert_redacted(error, marker, repr(event), repr(connection))
    assert connection.close_calls == 1


@pytest.mark.parametrize("operation", ["append", "records"])
def test_rollback_exit_and_close_failures_do_not_mask_or_leak_each_other(
    operation: str,
) -> None:
    event = _event()
    marker = f"sensitive-{operation}-multiple-failures"
    connection = _LifecycleConnection(
        row=_row(event),
        fail_exit=True,
        fail_close=True,
        fail_execute_at=1 if operation == "append" else 2,
        marker=marker,
    )
    ledger = PostgresAuditLedger(lambda: connection)

    error = (
        _capture_append_failure(ledger, event)
        if operation == "append"
        else _capture_records_failure(ledger)
    )

    _assert_redacted(error, marker, repr(event), repr(connection))
    assert connection.close_calls == 1
    assert connection.exit_exception_types == [psycopg.DataError]


def test_canonicalizer_schema_failure_is_redacted_without_opening_a_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = _event()
    marker = "sensitive-canonicalizer-schema-marker"
    factory_called = False

    def fail_canonicalization(_: AuditEvent) -> bytes:
        try:
            raise ValueError(marker)
        except ValueError as cause:
            raise SchemaValidationError() from cause

    def factory() -> psycopg.Connection[Any]:
        nonlocal factory_called
        factory_called = True
        raise AssertionError("canonicalizer failures must not open a connection")

    monkeypatch.setattr(repository_module, "canonicalize_event", fail_canonicalization)

    error = _capture_append_failure(PostgresAuditLedger(factory), event)

    _assert_redacted(error, marker, repr(event), repr(factory))
    assert not factory_called


def test_malformed_returned_row_rolls_back_closes_and_redacts_row_values() -> None:
    event = _event()
    marker = "sensitive-malformed-returned-row"
    connection = _LifecycleConnection(row=(marker,))

    error = _capture_append_failure(PostgresAuditLedger(lambda: connection), event)

    _assert_redacted(error, marker, repr(event), repr(connection))
    assert connection.exit_exception_types == [ValueError]
    assert connection.close_calls == 1


def test_records_failure_closes_and_redacts_factory_rows_and_sql_parameters() -> None:
    event = _event()
    row_marker = "sensitive-records-row-marker"
    driver_marker = "sensitive-records-driver-marker"
    connection = _LifecycleConnection(
        row=_row(event),
        rows=[(row_marker,)],
        fail_execute_at=2,
        marker=driver_marker,
    )

    error = _capture_records_failure(PostgresAuditLedger(lambda: connection))

    _assert_redacted(error, row_marker, driver_marker, repr(connection))
    assert connection.exit_exception_types == [psycopg.DataError]
    assert connection.close_calls == 1


def test_records_uses_exact_read_only_ordered_database_contract() -> None:
    event = _event()
    connection = _LifecycleConnection(row=_row(event), rows=[_row(event)])
    expected = AuditRecord(
        sequence_number=1,
        event_id=event.event_id,
        canonical_event=b"canonical-row",
        event_json=event.model_dump(mode="json"),
        previous_hash=b"p" * 32,
        entry_hash=b"e" * 32,
        received_at=datetime(2026, 8, 3, 12, 30, tzinfo=UTC),
    )

    records = PostgresAuditLedger(lambda: connection).records()

    assert records == (expected,)
    assert connection.executions == [
        ("SET TRANSACTION READ ONLY", None),
        (
            "SELECT sequence_number, event_id, canonical_event, event_json, "
            "previous_hash, entry_hash, received_at "
            "FROM audit.ledger ORDER BY sequence_number",
            None,
        ),
    ]
    assert connection.exit_exception_types == [None]
    assert connection.close_calls == 1


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
    assert connection.close_calls == 1
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

    error = _capture_append_failure(ledger, event)

    failed_connection = connections[-1]
    _assert_redacted(error, str(event.event_id), database.runtime_dsn)
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

    error = _capture_append_failure(ledger, event)

    failed_connection = connections[-1]
    _assert_redacted(error, str(event.event_id), database.runtime_dsn, "malformed bytes")
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
    error = _capture_append_failure(ledger, event)

    _assert_redacted(error, str(event.event_id), database.runtime_dsn, "append_event")
    assert ledger.records() == ()


def test_connection_failure_is_redacted_without_retaining_the_driver_error() -> None:
    marker = "sensitive-connection-diagnostic"

    def failing_factory() -> psycopg.Connection[Any]:
        raise psycopg.OperationalError(marker)

    error = _capture_append_failure(PostgresAuditLedger(failing_factory), _event())

    _assert_redacted(error, marker)


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
    assert sum(record.previous_hash == GENESIS_HASH for record in ordered) == 1
    assert all(right.previous_hash == left.entry_hash for left, right in pairwise(ordered))
    assert ledger.records() == ordered
