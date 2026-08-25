from __future__ import annotations

from typing import TYPE_CHECKING

import psycopg
import pytest

if TYPE_CHECKING:
    from ...conftest import DatabaseHarness

pytestmark = pytest.mark.integration


def test_memory_and_research_schemas_exist(database: DatabaseHarness) -> None:
    with psycopg.connect(database.runtime_dsn) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT to_regclass('memory.observed_facts'), "
            "to_regclass('memory.agent_beliefs'), to_regclass('research.trials')"
        )
        assert cursor.fetchone() == (
            "memory.observed_facts",
            "memory.agent_beliefs",
            "research.trials",
        )


def test_runtime_cannot_insert_a_fact_directly(database: DatabaseHarness) -> None:
    """I-13 at the database boundary, not just in the model."""

    with (
        psycopg.connect(database.runtime_dsn) as connection,
        connection.cursor() as cursor,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        cursor.execute("INSERT INTO memory.observed_facts (fact_id) VALUES ('f-1')")


# ERRCODE 55000 ("object not in prerequisite state") is this repo's shared append-only
# rejection code -- audit.reject_ledger_row_mutation() in 0001 raises it too, and psycopg
# maps it to ObjectNotInPrerequisiteState, not RaiseException (that class is only for the
# default, code-less PL/pgSQL RAISE EXCEPTION). Do not "fix" this back to RaiseException.


def test_facts_are_append_only(database: DatabaseHarness) -> None:
    """A row must exist for the row-level trigger to fire; roll back so nothing persists."""

    with psycopg.connect(database.migration_dsn) as connection, connection.cursor() as cursor:
        cursor.execute("SET ROLE trading_house_owner")
        cursor.execute(
            "INSERT INTO memory.observed_facts ("
            "fact_id, written_by, instrument_id, metric, value, observed_at, "
            "availability_time, previous_hash, entry_hash"
            ") VALUES ("
            "'f-append-only-check', 'deterministic', 'fx.eurusd', 'slippage_bps', 1.5, "
            "pg_catalog.clock_timestamp(), pg_catalog.clock_timestamp(), "
            "pg_catalog.decode(pg_catalog.repeat('00', 32), 'hex'), "
            "pg_catalog.decode(pg_catalog.repeat('00', 32), 'hex')"
            ")"
        )
        with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
            cursor.execute("UPDATE memory.observed_facts SET metric = 'x'")
        connection.rollback()


def test_trials_are_append_only(database: DatabaseHarness) -> None:
    """A row must exist for the row-level trigger to fire; roll back so nothing persists."""

    with psycopg.connect(database.migration_dsn) as connection, connection.cursor() as cursor:
        cursor.execute("SET ROLE trading_house_owner")
        cursor.execute(
            "INSERT INTO research.trials ("
            "trial_id, spec_id, agent_run_id, status, registered_at_sequence, "
            "previous_hash, entry_hash"
            ") VALUES ("
            "'t-append-only-check', 'spec-1', 'run-1', 'abandoned', 1, "
            "pg_catalog.decode(pg_catalog.repeat('00', 32), 'hex'), "
            "pg_catalog.decode(pg_catalog.repeat('00', 32), 'hex')"
            ")"
        )
        with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
            cursor.execute("DELETE FROM research.trials")
        connection.rollback()
