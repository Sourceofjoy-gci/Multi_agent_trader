"""The append-only position event store: every transition kept, the latest
row wins, the daemon's own row never resurfaces as a position, and the
database itself -- not application discipline -- refuses mutation."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

import psycopg
import pytest
from alembic import command
from pydantic import SecretStr

from tests.unit.execution.conftest import FakeProtectionVenue, RecordingEscalator
from trading_house.core.clock import FixedClock
from trading_house.core.venue import PositionRecord
from trading_house.database.connection import open_runtime_connection
from trading_house.execution.loop import SYSTEM_TICKET, PositionGuard
from trading_house.execution.positions import PostgresPositionStore

if TYPE_CHECKING:
    from ...conftest import DatabaseHarness

NOW = datetime(2026, 1, 1, tzinfo=UTC)
OWNED_RANGES = ((110000, 110100),)


@pytest.fixture
def _isolated_position_events(database: DatabaseHarness) -> Iterator[None]:
    """Give each test a fresh, empty position event store.

    ``execution.position_events`` is append-only by trigger (migration 0006)
    against every role, including a superuser, so there is no TRUNCATE or
    DELETE available to reset it between tests -- the same constraint
    ``_isolated_intent_ledger`` in ``test_ledger.py`` works around. Downgrading
    to just before 0006 and back up recreates an empty table without touching
    any other schema.
    """

    try:
        yield
    finally:
        command.downgrade(database.alembic_config, "0005_one_submitting_per_intent")
        command.upgrade(database.alembic_config, "head")


pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("_isolated_position_events")]


@pytest.fixture
def store(database: DatabaseHarness) -> PostgresPositionStore:
    return PostgresPositionStore(lambda: open_runtime_connection(SecretStr(database.runtime_dsn)))


def test_latest_is_the_most_recent_event(store: PostgresPositionStore) -> None:
    store.append(7, "OPEN_PROTECTED", NOW, {"stop_loss": "1.09700"})
    store.append(7, "OPEN_PROTECTED", NOW, {"stop_loss": "1.09800"})

    assert store.latest(7) == {
        "stop_loss": "1.09800",
        "position_ticket": 7,
        "lifecycle": "OPEN_PROTECTED",
    }


def test_open_positions_excludes_closed_ones(store: PostgresPositionStore) -> None:
    """The guard's working set. A closed position that kept appearing would be
    checked forever against a broker that no longer has it."""

    store.append(7, "OPEN_PROTECTED", NOW, {})
    store.append(8, "OPEN_PROTECTED", NOW, {})
    store.append(8, "CLOSED", NOW, {})

    rows = store.open_positions()
    assert [row["position_ticket"] for row in rows] == [7]
    # Exact-dict, not just the ticket: the supplement's named hazard was a
    # `DISTINCT ON` returning an extra column (e.g. a leaked `seq`), which an
    # index-only assertion would never see.
    assert rows[0] == {"position_ticket": 7, "lifecycle": "OPEN_PROTECTED"}


def test_open_positions_excludes_the_daemon_ticket(store: PostgresPositionStore) -> None:
    """Addition 3's second layer: the query itself must exclude SYSTEM_TICKET,
    not merely rely on the loop's own defensive skip -- otherwise a restart
    reads the shutdown/escalation row back as an open position."""

    store.append(7, "OPEN_PROTECTED", NOW, {})
    store.append(SYSTEM_TICKET, "GUARD_STOPPED", NOW, {})

    assert [row["position_ticket"] for row in store.open_positions()] == [7]


def test_append_stores_a_decimal_price_as_a_string_not_a_float(
    store: PostgresPositionStore,
) -> None:
    """Every price is a Decimal in memory and must round-trip as a JSON
    string, so no float ever reaches a stored price.

    The mutation that proves this test bites also corrected how: dropping
    `_payload_dumps` raises `TypeError: Object of type Decimal is not JSON
    serializable` at write time, because psycopg3's Jsonb has no fallback
    `default=`. So the failure mode guarded here is a refused write, not the
    silent float coercion this docstring used to claim -- a hard stop is the
    better of the two, and saying which one actually happens is the point.
    """

    # A raw Decimal, not a pre-stringified one: `append`'s `Mapping[str, str]`
    # annotation is a mypy-only constraint (mypy's `packages` config covers
    # only `src/trading_house`, never this test tree), so this is legal at
    # runtime, and it is the point -- only a raw Decimal can exercise
    # `_payload_dumps`'s `default=str` net (positions.py:90-94). A
    # pre-stringified value would stay green even with that net removed.
    store.append(7, "OPEN_PROTECTED", NOW, {"stop_loss": Decimal("1.09700")})

    stored = store.latest(7)
    assert stored is not None
    assert stored["stop_loss"] == "1.09700"
    assert isinstance(stored["stop_loss"], str)


def test_the_lifecycle_check_rejects_an_unknown_value(store: PostgresPositionStore) -> None:
    """Supplement Override 2: an unconstrained TEXT column in an append-only
    table lets one typo become a permanent row nothing can ever fix up.
    `position_lifecycle_known` (migration 0006) is what stops it."""

    with pytest.raises(psycopg.errors.CheckViolation) as exc_info:
        store.append(7, "OPEN_PROTECTD", NOW, {})
    assert exc_info.value.sqlstate == "23514"


# --- append-only, proved at both layers (grant and trigger) -----------------


def test_the_runtime_role_has_no_update_privilege(database: DatabaseHarness) -> None:
    """Proves the GRANT layer only. sqlstate 42501 is raised before Postgres
    ever consults a trigger, so this test alone says nothing about the
    trigger -- see the trigger tests below."""

    store = PostgresPositionStore(lambda: open_runtime_connection(SecretStr(database.runtime_dsn)))
    store.append(7, "OPEN_PROTECTED", NOW, {})

    with (
        open_runtime_connection(SecretStr(database.runtime_dsn)) as connection,
        pytest.raises(psycopg.errors.InsufficientPrivilege) as exc_info,
    ):
        connection.execute("UPDATE execution.position_events SET lifecycle = 'CLOSED'")
    assert exc_info.value.sqlstate == "42501"


def test_the_runtime_role_has_no_delete_privilege(database: DatabaseHarness) -> None:
    """Same reasoning as the UPDATE case: this proves the grant only."""

    store = PostgresPositionStore(lambda: open_runtime_connection(SecretStr(database.runtime_dsn)))
    store.append(7, "OPEN_PROTECTED", NOW, {})

    with (
        open_runtime_connection(SecretStr(database.runtime_dsn)) as connection,
        pytest.raises(psycopg.errors.InsufficientPrivilege) as exc_info,
    ):
        connection.execute("DELETE FROM execution.position_events")
    assert exc_info.value.sqlstate == "42501"


def test_the_trigger_refuses_a_role_that_does_have_privilege(
    database: DatabaseHarness, store: PostgresPositionStore
) -> None:
    """Proves the TRIGGER layer, which the grant tests above cannot reach:
    ``trading_house_owner`` holds UPDATE outright (it created the table), so a
    refusal here can only be ``position_events_no_mutation`` firing. This is
    the single most important test in this task -- Phase 4's ledger claimed
    append-only "by grant AND trigger" while only the grant was ever
    reachable, and a test exactly like this one is what proved that claim
    false."""

    store.append(7, "OPEN_PROTECTED", NOW, {})

    with psycopg.connect(database.migration_dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SET ROLE trading_house_owner")
            with pytest.raises(psycopg.errors.RaiseException) as exc_info:
                cursor.execute("UPDATE execution.position_events SET lifecycle = 'CLOSED'")
            assert exc_info.value.sqlstate == "P0001"
        connection.rollback()


def test_the_trigger_refuses_a_delete_from_a_role_with_the_privilege(
    database: DatabaseHarness, store: PostgresPositionStore
) -> None:
    """Same reasoning as the UPDATE case, for DELETE."""

    store.append(7, "OPEN_PROTECTED", NOW, {})

    with psycopg.connect(database.migration_dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SET ROLE trading_house_owner")
            with pytest.raises(psycopg.errors.RaiseException) as exc_info:
                cursor.execute("DELETE FROM execution.position_events WHERE position_ticket = 7")
            assert exc_info.value.sqlstate == "P0001"
        connection.rollback()


# --- conformance: the real guard against the real store (Addition 4) -------


def test_the_real_guard_drives_the_real_store_for_one_cycle(
    store: PostgresPositionStore,
) -> None:
    """The seam Addition 4 exists for: a type annotation alone would not catch
    a ``DISTINCT ON`` returning an extra column, or a row shape
    ``PositionGuard`` cannot actually consume. One cycle, a fake venue, the
    real Postgres store -- a row lands and reads back."""

    observed = PositionRecord(
        magic=110042,
        server_symbol="EURUSD",
        volume=Decimal("0.25"),
        position_ticket=7,
        stop_loss=Decimal("1.09700"),
        open_price=Decimal("1.10000"),
        is_buy=True,
        opened_at=NOW,
    )
    venue = FakeProtectionVenue(positions=(observed,))
    escalator = RecordingEscalator()
    guard = PositionGuard(
        store=store,
        venue=venue,
        escalator=escalator,
        clock=FixedClock(NOW),
        owned_magic_ranges=OWNED_RANGES,
        min_stop_distances={"EURUSD": Decimal("0.00001")},
        default_stop_distances={"EURUSD": Decimal("0.00300")},
    )

    report = guard.cycle()

    assert report.checked == 1
    row = store.latest(7)
    assert row is not None
    assert row["position_ticket"] == 7
    assert row["lifecycle"] == "OPEN_PROTECTED"
    assert row["stop_loss"] == "1.09700"
    assert row["open_price"] == "1.10000"
    assert row["initial_risk_distance"] == "0.00300"
    assert len(escalator.calls) == 1
