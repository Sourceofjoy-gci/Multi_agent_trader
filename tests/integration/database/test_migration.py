from __future__ import annotations

import hashlib
import json
import struct
import traceback
from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb
from pydantic import SecretStr

from trading_house.audit.canonical import DOMAIN_SEPARATOR, GENESIS_HASH
from trading_house.core.errors import DatabaseUnavailableError, MigrationMismatchError
from trading_house.database.connection import open_runtime_connection
from trading_house.database.migrations import assert_at_head

if TYPE_CHECKING:
    from ...conftest import DatabaseHarness

pytestmark = pytest.mark.integration


def _event() -> tuple[bytes, dict[str, object]]:
    event_json: dict[str, object] = {
        "event_id": str(uuid4()),
        "event_type": "integration.test",
        "payload": {"accepted": True},
    }
    canonical_event = json.dumps(event_json, sort_keys=True, separators=(",", ":")).encode()
    return canonical_event, event_json


def test_migration_creates_audit_objects_and_exact_revision(
    database: DatabaseHarness,
) -> None:
    with psycopg.connect(database.runtime_dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT to_regclass('audit.ledger'), "
                "to_regprocedure('audit.append_event(bytea,jsonb)')"
            )
            assert cursor.fetchone() == ("audit.ledger", "audit.append_event(bytea,jsonb)")

        assert_at_head(connection, database.alembic_config)


def test_database_default_timezone_is_utc_without_runtime_session_setup(
    database: DatabaseHarness,
) -> None:
    with psycopg.connect(database.runtime_dsn) as connection, connection.cursor() as cursor:
        cursor.execute("SHOW TIME ZONE")
        assert cursor.fetchone() == ("UTC",)


def test_runtime_connection_forces_and_verifies_utc(database: DatabaseHarness) -> None:
    with (
        open_runtime_connection(SecretStr(database.runtime_dsn)) as connection,
        connection.cursor() as cursor,
    ):
        cursor.execute("SHOW TIME ZONE")
        assert cursor.fetchone() == ("UTC",)


@pytest.mark.parametrize(
    "dsn_template",
    [
        "postgresql://runtime:{marker}@[malformed/trading_house",
        "host=127.0.0.1 port=not-a-port user=runtime password={marker}",
    ],
)
def test_runtime_connection_redacts_the_entire_exception_graph(
    dsn_template: str,
) -> None:
    marker = "round-one-sensitive-value"
    malformed_dsn = dsn_template.format(marker=marker)

    with pytest.raises(DatabaseUnavailableError) as raised:
        open_runtime_connection(SecretStr(malformed_dsn))

    assert str(raised.value) == "database connection failed"
    cause = raised.value.__cause__
    assert cause is not None
    assert not isinstance(cause, psycopg.Error)
    assert raised.value.__context__ is None
    assert cause.__cause__ is None
    assert cause.__context__ is None

    rendered_values = (
        str(raised.value),
        repr(raised.value),
        str(cause),
        repr(cause),
        "".join(traceback.format_exception(raised.value)),
    )
    for rendered in rendered_values:
        assert marker not in rendered
        assert malformed_dsn not in rendered


@pytest.mark.parametrize("revision_state", ["missing", "unexpected", "multiple"])
def test_revision_check_fails_closed(database: DatabaseHarness, revision_state: str) -> None:
    with psycopg.connect(database.admin_dsn) as connection:
        with connection.cursor() as cursor:
            if revision_state == "missing":
                cursor.execute("DROP TABLE public.alembic_version")
            elif revision_state == "unexpected":
                cursor.execute(
                    "UPDATE public.alembic_version SET version_num = 'unexpected_revision'"
                )
            else:
                cursor.execute(
                    "INSERT INTO public.alembic_version(version_num) VALUES ('unexpected_revision')"
                )

        with pytest.raises(MigrationMismatchError) as raised:
            assert_at_head(connection, database.alembic_config)

        assert str(raised.value) == "migration revision mismatch"
        connection.rollback()


def test_append_returns_exact_genesis_json_hashes_and_utc_receipt(
    database: DatabaseHarness,
) -> None:
    canonical_event, event_json = _event()

    with open_runtime_connection(SecretStr(database.runtime_dsn)) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT sequence_number, event_id, canonical_event, event_json, "
                "previous_hash, entry_hash, received_at "
                "FROM audit.append_event(%s, %s)",
                (canonical_event, Jsonb(event_json)),
            )
            row = cursor.fetchone()
        connection.commit()

    assert row is not None
    (
        sequence_number,
        event_id,
        stored_bytes,
        stored_json,
        previous_hash,
        entry_hash,
        received_at,
    ) = row
    assert sequence_number == 1
    assert event_id == event_json["event_id"] or str(event_id) == event_json["event_id"]
    assert stored_bytes == canonical_event
    assert stored_json == event_json
    assert previous_hash == GENESIS_HASH == bytes(32)
    assert len(previous_hash) == 32
    assert len(entry_hash) == 32
    assert (
        entry_hash
        == hashlib.sha256(
            DOMAIN_SEPARATOR + struct.pack(">q", 1) + previous_hash + canonical_event
        ).digest()
    )
    assert received_at.utcoffset() == timedelta(0)


def test_append_rejects_byte_json_mismatch_without_partial_row(
    database: DatabaseHarness,
) -> None:
    canonical_event, event_json = _event()
    mismatched_json = {**event_json, "payload": {"accepted": False}}

    with open_runtime_connection(SecretStr(database.runtime_dsn)) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM audit.ledger")
            count_before = cursor.fetchone()
            with pytest.raises(psycopg.DataError):
                cursor.execute(
                    "SELECT * FROM audit.append_event(%s, %s)",
                    (canonical_event, Jsonb(mismatched_json)),
                )
        connection.rollback()

    with (
        open_runtime_connection(SecretStr(database.runtime_dsn)) as connection,
        connection.cursor() as cursor,
    ):
        cursor.execute("SELECT count(*) FROM audit.ledger")
        count_after = cursor.fetchone()

    assert count_after == count_before


def test_migration_can_downgrade_cleanly_and_reapply(database: DatabaseHarness) -> None:
    from alembic import command

    command.downgrade(database.alembic_config, "base")
    try:
        with (
            psycopg.connect(database.admin_dsn) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                "SELECT to_regclass('audit.ledger'), "
                "to_regnamespace('audit'), to_regnamespace('audit_crypto')"
            )
            assert cursor.fetchone() == (None, None, None)

        with (
            psycopg.connect(database.migration_dsn) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute("SET ROLE trading_house_owner")
            cursor.execute(
                "CREATE FUNCTION public.downgrade_default_acl_probe() "
                "RETURNS INTEGER LANGUAGE SQL AS 'SELECT 1'"
            )

        with psycopg.connect(database.runtime_dsn) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT public.downgrade_default_acl_probe()")
            assert cursor.fetchone() == (1,)
    finally:
        with (
            psycopg.connect(database.migration_dsn) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute("SET ROLE trading_house_owner")
            cursor.execute("DROP FUNCTION IF EXISTS public.downgrade_default_acl_probe()")
        command.upgrade(database.alembic_config, "head")
