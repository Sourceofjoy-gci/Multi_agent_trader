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

from trading_house.core.errors import ConcurrentSubmissionError, IntentAlreadySubmittedError
from trading_house.core.values import IntentState
from trading_house.database.connection import open_runtime_connection
from trading_house.execution.ledger import (
    ConnectionFactory,
    PostgresIntentLedger,
    submission_lock,
)

if TYPE_CHECKING:
    from ...conftest import DatabaseHarness

NOW = datetime(2026, 1, 1, tzinfo=UTC)
EPOCH = datetime(2000, 1, 1, tzinfo=UTC)


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


def test_the_runtime_role_holds_no_update_privilege_at_all(
    database: DatabaseHarness,
) -> None:
    """This proves only the grant, not the trigger: ``trading_house_runtime`` holds no
    UPDATE privilege on ``execution.intent_events`` (migration 0004 grants it only
    SELECT, INSERT), so Postgres refuses the statement on privilege alone, before it
    ever consults a trigger. Catching the bare ``psycopg.errors.Error`` base class here
    would also pass if the trigger were broken or missing -- it swallows a syntax error
    or a missing table just as happily as a privilege violation -- so this asserts the
    specific sqlstate ``42501`` ("insufficient privilege") instead. The trigger itself
    is proved separately, from a role that *has* the privilege: see
    ``test_the_trigger_rejects_an_update_from_a_role_with_the_privilege`` below.
    """

    ledger = PostgresIntentLedger(lambda: open_runtime_connection(SecretStr(database.runtime_dsn)))
    ledger.append("i-1", IntentState.SUBMITTING, NOW, {})

    with (
        open_runtime_connection(SecretStr(database.runtime_dsn)) as connection,
        pytest.raises(psycopg.errors.InsufficientPrivilege) as exc_info,
    ):
        connection.execute(
            "UPDATE execution.intent_events SET state = 'CONFIRMED' WHERE intent_id = 'i-1'"
        )
    assert exc_info.value.sqlstate == "42501"


def test_the_runtime_role_holds_no_delete_privilege_at_all(
    database: DatabaseHarness,
) -> None:
    """Same reasoning as the UPDATE case above: this proves the grant only. The runtime
    role has no DELETE privilege on the table, so the statement is refused as sqlstate
    ``42501`` before any trigger runs. The trigger is proved separately below."""

    ledger = PostgresIntentLedger(lambda: open_runtime_connection(SecretStr(database.runtime_dsn)))
    ledger.append("i-1", IntentState.SUBMITTING, NOW, {})

    with (
        open_runtime_connection(SecretStr(database.runtime_dsn)) as connection,
        pytest.raises(psycopg.errors.InsufficientPrivilege) as exc_info,
    ):
        connection.execute("DELETE FROM execution.intent_events")
    assert exc_info.value.sqlstate == "42501"


def test_the_trigger_rejects_an_update_from_a_role_with_the_privilege(
    database: DatabaseHarness, ledger: PostgresIntentLedger
) -> None:
    """This proves only the trigger, not the grant: ``trading_house_owner`` holds UPDATE
    outright (it created the table), so if the statement is still refused, the grant
    cannot be why -- only ``intent_events_no_mutation`` firing explains it. Together with
    the runtime-role tests above, the two layers are now each proved by a test the other
    cannot pass: a test that cannot tell a privilege check from a trigger firing proves
    neither."""

    ledger.append("i-1", IntentState.SUBMITTING, NOW, {})

    with psycopg.connect(database.migration_dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SET ROLE trading_house_owner")
            with pytest.raises(psycopg.errors.RaiseException) as exc_info:
                cursor.execute(
                    "UPDATE execution.intent_events SET state = 'CONFIRMED' WHERE intent_id = 'i-1'"
                )
            assert exc_info.value.sqlstate == "P0001"
        connection.rollback()


def test_the_trigger_rejects_a_delete_from_a_role_with_the_privilege(
    database: DatabaseHarness, ledger: PostgresIntentLedger
) -> None:
    """Same reasoning as the UPDATE case above, for DELETE: the owner role has the
    privilege the grant would otherwise deny, so a rejection here can only be the
    trigger."""

    ledger.append("i-1", IntentState.SUBMITTING, NOW, {})

    with psycopg.connect(database.migration_dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SET ROLE trading_house_owner")
            with pytest.raises(psycopg.errors.RaiseException) as exc_info:
                cursor.execute("DELETE FROM execution.intent_events WHERE intent_id = 'i-1'")
            assert exc_info.value.sqlstate == "P0001"
        connection.rollback()


# --- one SUBMITTING per intent, enforced by the database (I-6) ---------------


def _factory(database: DatabaseHarness) -> ConnectionFactory:
    return lambda: open_runtime_connection(SecretStr(database.runtime_dsn))


def test_the_database_refuses_a_second_submitting_row_for_one_intent(
    database: DatabaseHarness,
) -> None:
    """The TOCTOU the application cannot close. ``OrderManager.submit`` reads
    the ledger and then writes to it; two invocations a millisecond apart both
    read "no events" and both insert SUBMITTING, and one approved decision
    becomes two positions. The window is between two statements, so only the
    database can refuse it -- this asserts the raw INSERT is rejected, not
    that our own code chose not to issue it."""

    insert = (
        "INSERT INTO execution.intent_events (intent_id, state, event_time, payload) "
        "VALUES ('i-race', 'SUBMITTING', %s, '{}'::jsonb)"
    )
    with open_runtime_connection(SecretStr(database.runtime_dsn)) as connection:
        connection.execute(insert, (NOW,))
        connection.commit()
        with pytest.raises(psycopg.errors.UniqueViolation) as exc_info:
            connection.execute(insert, (NOW,))
        assert exc_info.value.sqlstate == "23505"
        connection.rollback()


def test_the_ledger_reports_a_refused_duplicate_as_an_already_submitted_intent(
    ledger: PostgresIntentLedger,
) -> None:
    """The constraint above, surfaced as the typed error the CLI already maps
    to an exit code rather than as a raw driver failure."""

    ledger.append("i-race", IntentState.SUBMITTING, NOW, {})

    with pytest.raises(IntentAlreadySubmittedError):
        ledger.append("i-race", IntentState.SUBMITTING, NOW, {})


def test_an_intent_may_still_pass_through_every_other_state(
    ledger: PostgresIntentLedger,
) -> None:
    """Guard the guard: the index is partial for a reason. An intent
    legitimately accumulates rows after SUBMITTING, and a plain unique index
    on intent_id would turn the ledger into a single-row table."""

    ledger.append("i-1", IntentState.SUBMITTING, NOW, {})
    ledger.append("i-1", IntentState.UNKNOWN, NOW, {})
    ledger.append("i-1", IntentState.RECONCILING, NOW, {})
    ledger.append("i-1", IntentState.CONFIRMED, NOW, {})

    assert len(ledger.events_for("i-1")) == 4


# --- the submission lock (I-6) ----------------------------------------------


def test_a_second_submission_cannot_hold_the_lock(database: DatabaseHarness) -> None:
    """The other half of the race: two *different* intents, both passing a
    clean gate, both sending. The unique index says nothing about that case;
    the advisory lock is what makes the two invocations serialise."""

    factory = _factory(database)

    with (
        submission_lock(factory),
        pytest.raises(ConcurrentSubmissionError),
        submission_lock(factory),
    ):
        pytest.fail("a second invocation must not hold the submission lock")


def test_the_lock_is_released_when_the_sequence_ends(database: DatabaseHarness) -> None:
    """Guard the guard: a lock that were never released would refuse every
    subsequent order for the life of the database."""

    factory = _factory(database)

    with submission_lock(factory):
        pass
    with submission_lock(factory):
        pass  # must not raise


# --- Phase 10: what the portfolio's order rate and reject streak read ------------------


def test_submissions_since_names_each_submissions_book_after_the_start(
    ledger: PostgresIntentLedger,
) -> None:
    early = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
    late = datetime(2026, 10, 4, 12, 0, 30, tzinfo=UTC)
    ledger.append("a", IntentState.SUBMITTING, early, {"book": "fx_scalp"})
    ledger.append("b", IntentState.SUBMITTING, late, {"book": "fx_swing"})
    ledger.append("b", IntentState.CONFIRMED, late, {})

    assert ledger.submissions_since(early) == (("fx_swing", late),)
    assert [book for book, _ in ledger.submissions_since(datetime(2026, 1, 1, tzinfo=UTC))] == [
        "fx_scalp",
        "fx_swing",
    ]


def test_the_reject_streak_counts_rejections_since_the_last_confirmation(
    ledger: PostgresIntentLedger,
) -> None:
    at = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)

    def settle(intent_id: str, state: IntentState) -> None:
        ledger.append(intent_id, IntentState.SUBMITTING, at, {"book": "fx_scalp"})
        ledger.append(intent_id, state, at, {})

    assert ledger.consecutive_rejects(EPOCH) == 0
    settle("r1", IntentState.REJECTED)
    settle("r2", IntentState.REJECTED)
    assert ledger.consecutive_rejects(EPOCH) == 2
    settle("c1", IntentState.CONFIRMED)
    assert ledger.consecutive_rejects(EPOCH) == 0
    settle("r3", IntentState.REJECTED)
    # FAILED is the reconciler's verdict on a lost order, not a venue refusal:
    # it neither extends the streak nor breaks it.
    settle("f1", IntentState.FAILED)
    settle("r4", IntentState.REJECTED)
    assert ledger.consecutive_rejects(EPOCH) == 2
    # Phase 11: a person clearing safe mode restarts the streak from then.
    assert ledger.consecutive_rejects(at) == 0
