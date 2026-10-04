"""Phase 11: the halt ledger against real PostgreSQL -- one halt per switch,
cleared once, append-only by grant and by trigger."""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import psycopg
import pytest
from alembic import command
from pydantic import SecretStr

from trading_house.core.control import SYSTEM_ACTOR, HaltKind, HaltRequest, HaltScope
from trading_house.core.errors import HaltNotActiveError, TradingHaltedError
from trading_house.database.connection import open_runtime_connection
from trading_house.execution.control import PostgresControlStore, require_not_halted

if TYPE_CHECKING:
    from ...conftest import DatabaseHarness

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
SAFE = HaltRequest(
    kind=HaltKind.SAFE_MODE,
    scope=HaltScope.FIRM,
    target=None,
    reason="guard_escalation",
    actor=SYSTEM_ACTOR,
)
KILL_SCALP = HaltRequest(
    kind=HaltKind.KILL, scope=HaltScope.BOOK, target="fx_scalp", reason="desk", actor="ana"
)


@pytest.fixture
def _isolated_control_ledger(database: DatabaseHarness) -> Iterator[None]:
    """``execution.control_events`` is append-only against every role, so a
    fresh table is a downgrade past 0009 and back."""

    try:
        yield
    finally:
        command.downgrade(database.alembic_config, "0008_bars_closed_before_run")
        command.upgrade(database.alembic_config, "head")


pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("_isolated_control_ledger")]


@pytest.fixture
def store(database: DatabaseHarness) -> PostgresControlStore:
    return PostgresControlStore(lambda: open_runtime_connection(SecretStr(database.runtime_dsn)))


def test_a_halt_is_in_force_from_entry_until_a_person_clears_it(
    store: PostgresControlStore,
) -> None:
    halt, entered = store.enter(KILL_SCALP, NOW)

    assert entered
    assert store.active() == (halt,)
    assert halt.entered_at == NOW
    cleared = store.clear(halt.halt_id, actor="ben", reason="reviewed", at=NOW)
    assert cleared == halt
    assert store.active() == ()


def test_the_same_switch_twice_is_one_halt(store: PostgresControlStore) -> None:
    first, _ = store.enter(SAFE, NOW)
    again, entered = store.enter(SAFE.model_copy(update={"reason": "other"}), NOW)

    assert not entered
    assert again == first
    assert len(store.active()) == 1


def test_two_processes_asking_at_once_still_make_one_halt(
    store: PostgresControlStore,
) -> None:
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: store.enter(SAFE, NOW), range(8)))

    assert sum(entered for _, entered in results) == 1
    assert len({halt.halt_id for halt, _ in results}) == 1


def test_a_halt_cannot_be_cleared_twice_or_before_it_exists(
    store: PostgresControlStore,
) -> None:
    halt, _ = store.enter(KILL_SCALP, NOW)
    store.clear(halt.halt_id, actor="ben", reason="r", at=NOW)

    with pytest.raises(HaltNotActiveError):
        store.clear(halt.halt_id, actor="ben", reason="again", at=NOW)
    with pytest.raises(HaltNotActiveError):
        store.clear("never-entered", actor="ben", reason="r", at=NOW)


def test_after_a_clear_the_same_switch_can_be_thrown_again(
    store: PostgresControlStore,
) -> None:
    first, _ = store.enter(KILL_SCALP, NOW)
    store.clear(first.halt_id, actor="ben", reason="r", at=NOW)

    second, entered = store.enter(KILL_SCALP, NOW + timedelta(minutes=1))

    assert entered
    assert second.halt_id != first.halt_id


def test_last_cleared_is_per_switch_and_none_when_never(store: PostgresControlStore) -> None:
    halt, _ = store.enter(SAFE, NOW)
    later = NOW + timedelta(hours=1)
    store.clear(halt.halt_id, actor="ben", reason="r", at=later)

    assert store.last_cleared(HaltKind.SAFE_MODE, HaltScope.FIRM, None) == later
    assert store.last_cleared(HaltKind.KILL, HaltScope.BOOK, "fx_scalp") is None


def test_the_gate_names_the_halts_that_cover_an_order(store: PostgresControlStore) -> None:
    halt, _ = store.enter(KILL_SCALP, NOW)

    require_not_halted(store, book="fx_swing", instrument_id="fx.eurusd", strategy_id="s")
    with pytest.raises(TradingHaltedError) as refusal:
        require_not_halted(store, book="fx_scalp", instrument_id="fx.eurusd", strategy_id="s")

    assert refusal.value.halts == ((halt.halt_id, "kill"),)


@pytest.mark.parametrize(
    "statement",
    ["UPDATE execution.control_events SET reason = 'x'", "DELETE FROM execution.control_events"],
)
def test_the_runtime_role_cannot_rewrite_a_halt(
    database: DatabaseHarness, store: PostgresControlStore, statement: str
) -> None:
    store.enter(KILL_SCALP, NOW)

    with (
        open_runtime_connection(SecretStr(database.runtime_dsn)) as connection,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        connection.execute(statement)


@pytest.mark.parametrize(
    "statement",
    ["UPDATE execution.control_events SET reason = 'x'", "DELETE FROM execution.control_events"],
)
def test_the_trigger_refuses_even_the_owner(
    database: DatabaseHarness, store: PostgresControlStore, statement: str
) -> None:
    """The owner holds UPDATE and DELETE outright, so only the trigger can be
    what refuses here: a kill switch nobody can quietly lift."""

    store.enter(KILL_SCALP, NOW)

    with psycopg.connect(database.migration_dsn) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SET ROLE trading_house_owner")
            with pytest.raises(psycopg.errors.RaiseException):
                cursor.execute(statement)
        connection.rollback()


def test_a_firm_halt_with_a_target_is_refused_by_the_table(database: DatabaseHarness) -> None:
    with (
        psycopg.connect(database.runtime_dsn) as connection,
        pytest.raises(psycopg.errors.CheckViolation),
    ):
        connection.execute(
            "INSERT INTO execution.control_events "
            "(halt_id, action, kind, scope, target, reason, actor, event_time) "
            "VALUES ('h', 'ENTERED', 'kill', 'firm', 'fx_scalp', 'r', 'a', now())"
        )
