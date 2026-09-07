"""The append-only intent ledger: every transition kept, latest one wins,
and the database itself -- not application discipline -- refuses mutation."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import psycopg
import pytest
from alembic import command
from pydantic import SecretStr

from trading_house.core.values import IntentState
from trading_house.database.connection import open_runtime_connection
from trading_house.execution.ledger import PostgresIntentLedger

if TYPE_CHECKING:
    from ...conftest import DatabaseHarness

NOW = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def _isolated_intent_ledger(database: DatabaseHarness) -> Iterator[None]:
    """Give each test a fresh, empty intent ledger.

    ``execution.intent_events`` is append-only by trigger (migration 0004)
    against every role, including a superuser, so there is no TRUNCATE or
    DELETE available to reset it between tests -- the same constraint
    ``isolated_audit_ledger`` in ``tests/conftest.py`` works around for the
    audit ledger. Downgrading to just before 0004 and back up recreates an
    empty table without touching any other schema.
    """

    try:
        yield
    finally:
        command.downgrade(database.alembic_config, "0003_market_bars")
        command.upgrade(database.alembic_config, "head")


pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("_isolated_intent_ledger")]


@pytest.fixture
def ledger(database: DatabaseHarness) -> PostgresIntentLedger:
    return PostgresIntentLedger(lambda: open_runtime_connection(SecretStr(database.runtime_dsn)))


def test_current_state_is_the_latest_event(ledger: PostgresIntentLedger) -> None:
    ledger.append("i-1", IntentState.SUBMITTING, NOW, {"step": 1})
    ledger.append("i-1", IntentState.UNKNOWN, NOW, {"step": 2})
    ledger.append("i-1", IntentState.CONFIRMED, NOW, {"step": 3})

    assert ledger.current_state("i-1") is IntentState.CONFIRMED


def test_every_transition_is_kept(ledger: PostgresIntentLedger) -> None:
    """The point of an append-only ledger is that an incident review can see
    how long an intent sat in UNKNOWN, not merely that it did."""

    ledger.append("i-1", IntentState.SUBMITTING, NOW, {})
    ledger.append("i-1", IntentState.UNKNOWN, NOW, {})
    ledger.append("i-1", IntentState.CONFIRMED, NOW, {})

    assert [e.state for e in ledger.events_for("i-1")] == [
        IntentState.SUBMITTING,
        IntentState.UNKNOWN,
        IntentState.CONFIRMED,
    ]


def test_non_terminal_finds_exactly_the_intents_the_gate_must_resolve(
    ledger: PostgresIntentLedger,
) -> None:
    ledger.append("open-1", IntentState.SUBMITTING, NOW, {})
    ledger.append("open-2", IntentState.UNKNOWN, NOW, {})
    ledger.append("open-3", IntentState.RECONCILING, NOW, {})
    ledger.append("done-1", IntentState.SUBMITTING, NOW, {})
    ledger.append("done-1", IntentState.CONFIRMED, NOW, {})
    ledger.append("done-2", IntentState.REJECTED, NOW, {})
    ledger.append("done-3", IntentState.FAILED, NOW, {})

    assert set(ledger.non_terminal()) == {"open-1", "open-2", "open-3"}


def test_an_intent_that_reached_a_terminal_state_is_not_reopened(
    ledger: PostgresIntentLedger,
) -> None:
    """A CONFIRMED intent followed by nothing must stay out of the gate's list.
    If `non_terminal` looked at any event rather than the latest, every intent
    that ever passed through SUBMITTING would block all trading forever."""

    ledger.append("i-1", IntentState.SUBMITTING, NOW, {})
    ledger.append("i-1", IntentState.CONFIRMED, NOW, {})

    assert ledger.non_terminal() == ()


def test_the_database_refuses_an_update_from_the_runtime_role(
    database: DatabaseHarness,
) -> None:
    """Append-only by grant and trigger, not by application discipline.
    Discipline is not evidence; this is."""

    ledger = PostgresIntentLedger(lambda: open_runtime_connection(SecretStr(database.runtime_dsn)))
    ledger.append("i-1", IntentState.SUBMITTING, NOW, {})

    with (
        open_runtime_connection(SecretStr(database.runtime_dsn)) as connection,
        pytest.raises(psycopg.errors.Error),
    ):
        connection.execute(
            "UPDATE execution.intent_events SET state = 'CONFIRMED' WHERE intent_id = 'i-1'"
        )


def test_the_database_refuses_a_delete_from_the_runtime_role(
    database: DatabaseHarness,
) -> None:
    ledger = PostgresIntentLedger(lambda: open_runtime_connection(SecretStr(database.runtime_dsn)))
    ledger.append("i-1", IntentState.SUBMITTING, NOW, {})

    with (
        open_runtime_connection(SecretStr(database.runtime_dsn)) as connection,
        pytest.raises(psycopg.errors.Error),
    ):
        connection.execute("DELETE FROM execution.intent_events")
