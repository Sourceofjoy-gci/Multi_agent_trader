"""The append-only trial ledger, against a real second PostgreSQL database.

Every assertion here is a database behaviour, not a Python one. The chain is
verified by re-deriving its hashes from the bytes PostgreSQL stored, the
append-only guarantee is asserted by watching PostgreSQL refuse a statement, and
the concurrency proof is eight writers on eight connections -- none of which a
mock could produce, and all of which a unit test would only be able to assume.

``research_migration_dsn`` is the *owner* role rather than the runtime role, so
the mutation tests prove the trigger refuses even the role that owns the table.
A privilege test that only ran as the runtime role would pass against a table
with no trigger at all.
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal
from itertools import pairwise
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import psycopg
import pytest
from psycopg.types.json import Jsonb
from pydantic import SecretStr

import trading_house.research.ledger_store as ledger_store
from trading_house.core.errors import TrialLedgerAppendError
from trading_house.database.connection import open_runtime_connection
from trading_house.marketdata.models import Timeframe
from trading_house.research.backtest.costs import CostModel
from trading_house.research.canonical import canonical_bytes, canonical_sha256
from trading_house.research.ledger_store import GENESIS_HASH, PostgresTrialLedger
from trading_house.research.trial_ledger import (
    CostSpec,
    DataSpec,
    EvidenceSealedPayload,
    ExecutionSpec,
    ExecutionStartedPayload,
    FailedPayload,
    HoldoutSpec,
    HoldoutState,
    LedgerEvent,
    LedgerEventPayload,
    LedgerEventType,
    LegacyImportedPayload,
    PreregisteredPayload,
    RegimeSpec,
    RegistrationState,
    ResultRecordedPayload,
    ScopeKind,
    TrialCounters,
    TrialProtocol,
    TrialSpec,
    ValidationSpec,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("isolated_research_ledger"),
]

WRITERS = 8
GENESIS_HEX = GENESIS_HASH.hex()


def _data() -> DataSpec:
    return DataSpec(
        instrument_id="fx.eurusd",
        timeframe=Timeframe.M15,
        start=datetime(2024, 1, 1, tzinfo=UTC),
        end=datetime(2024, 6, 1, tzinfo=UTC),
        dataset_sha256="a" * 64,
        point_in_time_policy="availability_time",
    )


def _candidate(index: int) -> TrialSpec:
    return TrialSpec(
        trial_id=f"trial-{index}",
        spec_id=f"spec-{index}",
        rationale="declared before execution",
        parameter_space=(("window", f"value-{index}"),),
    )


def _protocol() -> TrialProtocol:
    return TrialProtocol(
        protocol_id="protocol-1",
        protocol_version="1",
        agent_run_id="run-1",
        strategy_id="session_momentum",
        strategy_version="1",
        strategy_sha256="b" * 64,
        data=_data(),
        execution=ExecutionSpec(
            seed="fixed",
            warmup_bars=20,
            fill_policy="pessimistic-bar",
            sizing_policy="risk-engine",
        ),
        costs=CostSpec(
            baseline=CostModel(
                commission_per_lot_per_side=Decimal("0"),
                slippage_points_per_side=Decimal("0.4"),
                swap_long_points_per_day=Decimal("-7.7"),
                swap_short_points_per_day=Decimal("2"),
                triple_swap_weekday=2,
                stress_multiplier=Decimal("1"),
            ),
            stress_multipliers=(Decimal("1.5"), Decimal("2")),
        ),
        validation=ValidationSpec(
            primary_metric="net_expectancy",
            wfa_train_months=24,
            wfa_validation_months=6,
            wfa_test_months=6,
            purge_hours=16,
            embargo_hours=16,
            cpcv_folds=6,
            bootstrap_replicates=10000,
            bootstrap_c=Decimal("6.7"),
            trial_count_rule="conservative-selection-lotteries",
        ),
        regimes=RegimeSpec(labels=("london", "new_york"), provenance_sha256="c" * 64),
        holdout=HoldoutSpec(state=HoldoutState.NOT_DEFINED),
        candidates=(_candidate(1), _candidate(2)),
    )


def _ledger(dsn: str) -> PostgresTrialLedger:
    return PostgresTrialLedger(lambda: open_runtime_connection(SecretStr(dsn)))


def _attempt_event(
    trial_id: str,
    *,
    event_type: LedgerEventType,
    payload: LedgerEventPayload,
) -> LedgerEvent:
    return LedgerEvent(
        event_id=uuid5(NAMESPACE_URL, f"test:{event_type.value}:{trial_id}"),
        scope_kind=ScopeKind.ATTEMPT,
        scope_id=f"attempt-{trial_id}",
        event_type=event_type,
        trial_id=trial_id,
        attempt_id=f"attempt-{trial_id}",
        spec_sha256="d" * 64,
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
        payload=payload,
    )


def _execution_started_event(trial_id: str) -> LedgerEvent:
    return _attempt_event(
        trial_id,
        event_type=LedgerEventType.EXECUTION_STARTED,
        payload=ExecutionStartedPayload(
            event_type=LedgerEventType.EXECUTION_STARTED,
            attempt_id=f"attempt-{trial_id}",
            execution_started_at=datetime(2026, 1, 1, tzinfo=UTC),
        ),
    )


def _result_event(trial_id: str) -> LedgerEvent:
    return _attempt_event(
        trial_id,
        event_type=LedgerEventType.RESULT_RECORDED,
        payload=ResultRecordedPayload(
            event_type=LedgerEventType.RESULT_RECORDED,
            attempt_id=f"attempt-{trial_id}",
            source_result_sha256="a" * 64,
        ),
    )


def _failed_event(trial_id: str) -> LedgerEvent:
    return _attempt_event(
        trial_id,
        event_type=LedgerEventType.FAILED,
        payload=FailedPayload(
            event_type=LedgerEventType.FAILED,
            attempt_id=f"attempt-{trial_id}",
            reason="the backtester raised",
        ),
    )


def _sealed_event(trial_id: str) -> LedgerEvent:
    return _attempt_event(
        trial_id,
        event_type=LedgerEventType.EVIDENCE_SEALED,
        payload=EvidenceSealedPayload(
            event_type=LedgerEventType.EVIDENCE_SEALED,
            attempt_id=f"attempt-{trial_id}",
            evidence_sha256="e" * 64,
        ),
    )


def _preregistered_event(protocol: TrialProtocol) -> LedgerEvent:
    return LedgerEvent(
        event_id=uuid5(NAMESPACE_URL, f"test:protocol:{protocol.protocol_id}"),
        scope_kind=ScopeKind.PROTOCOL,
        scope_id=protocol.protocol_id,
        event_type=LedgerEventType.PREREGISTERED,
        spec_sha256=canonical_sha256(protocol),
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
        payload=PreregisteredPayload(
            event_type=LedgerEventType.PREREGISTERED,
            protocol=protocol,
            registration_state=RegistrationState.PROSPECTIVE,
        ),
    )


def _legacy_event(trial_id: str) -> LedgerEvent:
    reason = "imported from a phase 7 artifact"
    return LedgerEvent(
        event_id=uuid5(NAMESPACE_URL, f"test:legacy:{trial_id}"),
        scope_kind=ScopeKind.TRIAL,
        scope_id=trial_id,
        event_type=LedgerEventType.LEGACY_IMPORTED,
        trial_id=trial_id,
        attempt_id=f"attempt-{trial_id}",
        spec_sha256="c" * 64,
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
        payload=LegacyImportedPayload(
            event_type=LedgerEventType.LEGACY_IMPORTED,
            trial=_candidate(1).model_copy(update={"trial_id": trial_id}),
            evidence_sha256="b" * 64,
            source_result_sha256="c" * 64,
            legacy_reason=reason,
        ),
        legacy=True,
        legacy_reason=reason,
    )


@pytest.fixture
def trial_protocol() -> TrialProtocol:
    return _protocol()


def test_genesis_and_chained_events_verify(
    research_ledger_dsn: str,
    research_evidence_root: Path,
    trial_protocol: TrialProtocol,
) -> None:
    """The chain verifies against the bytes PostgreSQL stored, not against itself.

    ``research_evidence_root`` is here to prove the ledger store touches no
    filesystem: the evidence store owns that root, and an append that created a
    directory under it would be the first step toward two owners of one address.
    """

    ledger = _ledger(research_ledger_dsn)

    first = ledger.append(_preregistered_event(trial_protocol))
    second = ledger.append(_execution_started_event(trial_protocol.candidates[0].trial_id))
    report = ledger.verify()

    assert report.valid
    assert report.reason is None
    assert report.checked_events == 2
    assert first.sequence == 1
    assert first.previous_hash == GENESIS_HEX
    assert first.spec_sha256 == canonical_sha256(trial_protocol)
    assert first.legacy is False
    assert first.recorded_at.tzinfo is not None
    assert second.sequence == 2
    assert second.previous_hash == first.event_hash
    assert len(first.event_hash) == 64
    assert not research_evidence_root.exists()


@pytest.mark.parametrize(
    "event_factory",
    [_result_event, _failed_event, _sealed_event],
    ids=["result", "failed", "sealed"],
)
def test_result_before_registration_is_refused(
    research_ledger_dsn: str,
    trial_protocol: TrialProtocol,
    event_factory: Callable[[str], LedgerEvent],
) -> None:
    """A trial that was never preregistered cannot have produced an outcome.

    A result recorded against a protocol nobody ever saw is the shape of a
    p-hack, and it is refused at the boundary rather than left for a later
    reader to notice.
    """

    ledger = _ledger(research_ledger_dsn)

    with pytest.raises(TrialLedgerAppendError):
        ledger.append(event_factory(trial_protocol.candidates[0].trial_id))

    assert ledger.events() == ()


def test_an_outcome_that_names_no_trial_is_refused(research_ledger_dsn: str) -> None:
    """A result with no trial id attributes itself to nothing and counts for nothing.

    It would be invisible to ``counters`` and unattributable to any protocol,
    while still being a permanent row in an append-only table.
    """

    ledger = _ledger(research_ledger_dsn)
    event = _result_event("trial-1").model_copy(update={"trial_id": None})

    with pytest.raises(TrialLedgerAppendError):
        ledger.append(event)

    assert ledger.events() == ()


def test_a_registration_does_not_cover_a_candidate_it_never_declared(
    research_ledger_dsn: str,
    trial_protocol: TrialProtocol,
) -> None:
    """Registration is per trial, not per ledger.

    Satisfying the check with a *sibling* candidate's declaration is the shape
    this refuses: the candidates are a family, and a member that was never
    declared is not covered by the ones that were.
    """

    ledger = _ledger(research_ledger_dsn)
    ledger.append(_preregistered_event(trial_protocol))

    with pytest.raises(TrialLedgerAppendError):
        ledger.append(_result_event("trial-never-declared"))


def test_same_event_id_with_different_payload_is_refused(
    research_ledger_dsn: str,
    trial_protocol: TrialProtocol,
) -> None:
    ledger = _ledger(research_ledger_dsn)
    event = _preregistered_event(trial_protocol)
    first = ledger.append(event)

    with pytest.raises(TrialLedgerAppendError):
        ledger.append(event.model_copy(update={"scope_id": "different"}))

    assert ledger.events() == (first,)


def test_re_appending_the_identical_event_is_idempotent(
    research_ledger_dsn: str,
    trial_protocol: TrialProtocol,
) -> None:
    """A retry after a lost acknowledgement must not fork the chain.

    The event id is derived from the content, so a retried event is recognised
    by its id *before* the expected previous hash is compared -- otherwise a
    retry that raced another append would be refused for the wrong reason, and
    the caller could not tell a lost race from a lost write.
    """

    ledger = _ledger(research_ledger_dsn)
    first = ledger.append(_preregistered_event(trial_protocol))
    second = ledger.append(_execution_started_event(trial_protocol.candidates[0].trial_id))

    retried = ledger.append(_preregistered_event(trial_protocol))

    assert retried == first
    assert ledger.events() == (first, second)
    assert ledger.verify().valid


def test_a_lost_race_is_retried_and_lands_the_same_event_once(
    research_ledger_dsn: str,
    trial_protocol: TrialProtocol,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 40001 path is the design's race handling, so it is staged, not awaited.

    A real eight-writer run almost never loses this race: opening a connection
    costs milliseconds and the window between reading the head and appending is
    microseconds, so the writers arrive already staggered. The race is therefore
    injected -- the store is handed a head that has moved, which is exactly the
    state a lost race puts it in, and the database raises a real 40001.
    """

    ledger = _ledger(research_ledger_dsn)
    first = ledger.append(_preregistered_event(trial_protocol))
    read_the_real_head = ledger_store._tail_hash
    stale: list[bytes] = [GENESIS_HASH]

    def stale_once(cursor: psycopg.Cursor[tuple[object, ...]]) -> bytes:
        head, stale[0] = stale[0], read_the_real_head(cursor)
        return head

    monkeypatch.setattr(ledger_store, "_tail_hash", stale_once)

    record = ledger.append(_execution_started_event(trial_protocol.candidates[0].trial_id))

    assert record.sequence == 2
    assert record.previous_hash == first.event_hash
    assert ledger.events() == (first, record)
    assert ledger.verify().valid


def test_a_head_that_never_stops_moving_gives_up_rather_than_looping(
    research_ledger_dsn: str,
    trial_protocol: TrialProtocol,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The retry budget is a budget.

    A store that re-read the head forever would turn a broken writer into a
    hung command, and a trial command that hangs is indistinguishable from one
    that is working.
    """

    ledger = _ledger(research_ledger_dsn)
    first = ledger.append(_preregistered_event(trial_protocol))
    monkeypatch.setattr(ledger_store, "_tail_hash", lambda _cursor: b"x" * 32)

    with pytest.raises(TrialLedgerAppendError):
        ledger.append(_execution_started_event(trial_protocol.candidates[0].trial_id))

    assert ledger.events() == (first,)


def test_rolled_back_sequence_gap_does_not_break_chain(
    research_ledger_dsn: str,
    research_migration_dsn: str,
    trial_protocol: TrialProtocol,
) -> None:
    """A burned sequence number is a gap, not a break.

    The chain is linked by previous_hash and never by ``sequence - 1``, so an
    insert that consumed a sequence value and then rolled back is invisible to
    integrity.
    """

    ledger = _ledger(research_ledger_dsn)
    ledger.append(_preregistered_event(trial_protocol))

    with psycopg.connect(research_migration_dsn) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT nextval('research.trial_ledger_events_sequence_seq')")
        connection.rollback()

    assert ledger.verify().valid


def test_concurrent_appends_form_one_continuous_chain(research_ledger_dsn: str) -> None:
    """Eight writers, eight connections, one chain.

    Each writer reads the head before it appends, so most of them lose the race
    to the advisory lock and are told 40001. Retrying the *same* deterministic
    event is what keeps the chain whole; minting a new event id per attempt is
    what would fork it.
    """

    ledger = _ledger(research_ledger_dsn)

    with ThreadPoolExecutor(max_workers=WRITERS) as pool:
        appended = list(
            pool.map(
                lambda index: ledger.append(_execution_started_event(f"t{index}")),
                range(WRITERS),
            )
        )

    ordered = ledger.events()

    assert [record.sequence for record in ordered] == list(range(1, WRITERS + 1))
    assert ordered == tuple(sorted(appended, key=lambda record: record.sequence))
    assert len({record.event_id for record in ordered}) == WRITERS
    assert sum(record.previous_hash == GENESIS_HEX for record in ordered) == 1
    assert all(right.previous_hash == left.event_hash for left, right in pairwise(ordered))
    assert ledger.verify().valid


def test_register_seals_the_whole_protocol_once_and_returns_its_candidates(
    research_ledger_dsn: str,
    trial_protocol: TrialProtocol,
) -> None:
    ledger = _ledger(research_ledger_dsn)

    first = ledger.register(trial_protocol)
    second = ledger.register(trial_protocol)

    records = ledger.events()

    assert first == trial_protocol.candidates
    assert second == trial_protocol.candidates
    assert len(records) == 1
    assert records[0].event_id == uuid5(
        NAMESPACE_URL, f"trading-house:trial-protocol:{canonical_sha256(trial_protocol)}"
    )
    assert records[0].scope_kind == ScopeKind.PROTOCOL
    assert records[0].event_type == LedgerEventType.PREREGISTERED
    assert ledger.verify().valid


def test_events_for_replay_and_counters_read_one_chain(
    research_ledger_dsn: str,
    trial_protocol: TrialProtocol,
) -> None:
    ledger = _ledger(research_ledger_dsn)
    registered = trial_protocol.candidates[0].trial_id
    other = trial_protocol.candidates[1].trial_id
    ledger.register(trial_protocol)
    ledger.append(_execution_started_event(registered))
    ledger.append(_result_event(registered))
    ledger.append(_execution_started_event(other))

    records = ledger.events()
    events = ledger.replay()

    assert [record.sequence for record in ledger.events_for(registered)] == [2, 3]
    assert [record.event_type for record in ledger.events_for(registered)] == [
        LedgerEventType.EXECUTION_STARTED,
        LedgerEventType.RESULT_RECORDED,
    ]
    assert [event.event_id for event in events] == [record.event_id for record in records]
    assert events[0].payload.event_type is LedgerEventType.PREREGISTERED
    assert ledger.counters() == TrialCounters(
        audit_attempts=2,
        selection_lotteries=2,
        effective_specifications=1,
    )


def test_legacy_lineage_admits_a_result_without_a_preregistration(
    research_ledger_dsn: str,
) -> None:
    """A Phase 7 result has no protocol because it predates the ledger.

    Refusing it would leave the three completed trials out of the denominator
    they belong to, so an explicit legacy import is the one lineage that carries
    a result on its own -- and it says so, in a reason field, forever.
    """

    ledger = _ledger(research_ledger_dsn)
    trial_id = "trial-legacy"
    ledger.append(_legacy_event(trial_id))

    record = ledger.append(_result_event(trial_id))

    assert record.trial_id == trial_id
    assert ledger.verify().valid


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE research.trial_ledger_events SET event_hash = event_hash",
        "DELETE FROM research.trial_ledger_events",
        "TRUNCATE research.trial_ledger_events",
    ],
)
def test_the_owner_cannot_mutate_or_truncate_the_ledger(
    research_ledger_dsn: str,
    research_migration_dsn: str,
    trial_protocol: TrialProtocol,
    statement: str,
) -> None:
    """The trigger, not the grant, is the guarantee.

    ``research_migration_dsn`` is the migrator, and it can ``SET ROLE`` to the
    owner that owns the table. A privilege-only design would let this through;
    the trigger is what refuses it.
    """

    _ledger(research_ledger_dsn).append(_preregistered_event(trial_protocol))

    with psycopg.connect(research_migration_dsn) as connection, connection.cursor() as cursor:
        cursor.execute("SET ROLE trading_house_owner")
        with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
            cursor.execute(statement)
        connection.rollback()


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO research.trial_ledger_events DEFAULT VALUES",
        "UPDATE research.trial_ledger_events SET event_hash = event_hash",
        "DELETE FROM research.trial_ledger_events",
        "TRUNCATE research.trial_ledger_events",
    ],
)
def test_the_runtime_role_holds_no_direct_mutation_privilege(
    research_ledger_dsn: str,
    statement: str,
) -> None:
    with open_runtime_connection(SecretStr(research_ledger_dsn)) as connection:
        with connection.cursor() as cursor, pytest.raises(psycopg.errors.InsufficientPrivilege):
            cursor.execute(statement)
        connection.rollback()


def test_the_runtime_may_read_the_chain_and_call_the_function(
    research_ledger_dsn: str,
) -> None:
    """The exact privilege set: read the chain, call the function, nothing else.

    ``research.trial_ledger_heads`` is deliberately absent -- the head the store
    submits is re-derived from the chain itself, so no role is trusted to keep
    a mutable summary row in step with the events.
    """

    with (
        open_runtime_connection(SecretStr(research_ledger_dsn)) as connection,
        connection.cursor() as cursor,
    ):
        cursor.execute(
            "SELECT "
            "has_table_privilege(current_user, 'research.trial_ledger_events', 'SELECT'), "
            "has_table_privilege(current_user, 'research.trial_ledger_events', 'INSERT'), "
            "has_table_privilege(current_user, 'research.trial_ledger_events', 'UPDATE'), "
            "has_table_privilege(current_user, 'research.trial_ledger_events', 'DELETE'), "
            "has_table_privilege(current_user, 'research.trial_ledger_events', 'TRUNCATE'), "
            "has_table_privilege(current_user, 'research.trial_ledger_heads', 'SELECT'), "
            "has_function_privilege(current_user, "
            "'research.append_trial_ledger_event(bytea,jsonb,bytea,bytea)', 'EXECUTE')"
        )
        assert cursor.fetchone() == (True, False, False, False, False, False, True)


def test_public_privileges_are_revoked_and_the_append_function_is_pinned(
    research_ledger_dsn: str,
) -> None:
    """``SECURITY DEFINER`` with a pinned ``search_path`` is the only shape here
    that can do this job: the function runs as the owner, so it needs to be
    unable to be redirected by a caller's ``search_path``."""

    with (
        open_runtime_connection(SecretStr(research_ledger_dsn)) as connection,
        connection.cursor() as cursor,
    ):
        cursor.execute(
            "SELECT "
            "has_table_privilege(0::oid, 'research.trial_ledger_events', 'SELECT'), "
            "has_table_privilege(0::oid, 'research.trial_ledger_events', 'INSERT'), "
            "has_function_privilege(0::oid, "
            "'research.append_trial_ledger_event(bytea,jsonb,bytea,bytea)', 'EXECUTE')"
        )
        assert cursor.fetchone() == (False, False, False)

        cursor.execute(
            "SELECT r.rolname, p.prosecdef, p.proconfig "
            "FROM pg_catalog.pg_proc AS p "
            "JOIN pg_catalog.pg_roles AS r ON r.oid = p.proowner "
            "WHERE p.oid = "
            "'research.append_trial_ledger_event(bytea,jsonb,bytea,bytea)'::regprocedure"
        )
        assert cursor.fetchone() == ("trading_house_owner", True, ["search_path=pg_catalog"])


def test_the_append_function_refuses_a_stale_expected_head(research_ledger_dsn: str) -> None:
    """40001 is the only error the store retries, so it has to mean one thing.

    Handing the function a head that has moved on is a lost race, and it is
    reported as one. Appending anyway would let a caller believe its event
    chained onto a head that no longer exists.

    The event has to be one that is *not* already in the chain: an id the
    function recognises is a successful retry, checked before the head, and
    would answer with a row instead of the error.
    """

    _ledger(research_ledger_dsn).append(_execution_started_event("trial-1"))
    event = _execution_started_event("trial-2")

    with open_runtime_connection(SecretStr(research_ledger_dsn)) as connection:
        with connection.cursor() as cursor, pytest.raises(psycopg.errors.SerializationFailure):
            cursor.execute(
                "SELECT * FROM research.append_trial_ledger_event(%s, %s, %s, %s)",
                (
                    canonical_bytes(event),
                    Jsonb(event.model_dump(mode="json")),
                    b"wrong-head",
                    b"p" * 32,
                ),
            )
        connection.rollback()


@pytest.mark.parametrize(
    ("statement", "reason"),
    [
        (
            "SET event_hash = pg_catalog.decode(pg_catalog.repeat('00', 32), 'hex')",
            "event_hash_mismatch",
        ),
        (
            "SET previous_hash = pg_catalog.decode(pg_catalog.repeat('11', 32), 'hex')",
            "previous_hash_mismatch",
        ),
        (
            "SET spec_sha256 = pg_catalog.decode(pg_catalog.repeat('22', 32), 'hex')",
            "spec_digest_mismatch",
        ),
        (
            "SET payload_sha256 = pg_catalog.decode(pg_catalog.repeat('33', 32), 'hex')",
            "payload_digest_mismatch",
        ),
        (
            "SET canonical_event = canonical_event || pg_catalog.convert_to(' ', 'UTF8')",
            "canonical_event_mismatch",
        ),
        (
            "SET event_json = event_json || '{\"tampered\": true}'::pg_catalog.jsonb",
            "event_json_mismatch",
        ),
        (
            "SET canonical_event = pg_catalog.convert_to('{\"not\": \"an event\"}', 'UTF8')",
            "event_schema_invalid",
        ),
    ],
    ids=[
        "hash",
        "link",
        "spec",
        "payload",
        "non-canonical-bytes",
        "jsonb",
        "unparseable",
    ],
)
def test_every_kind_of_tampering_is_reported_rather_than_raised(
    research_ledger_dsn: str,
    research_migration_dsn: str,
    trial_protocol: TrialProtocol,
    statement: str,
    reason: str,
) -> None:
    """``verify()`` answers with a report, because a caller has to be able to ask.

    Raising would make "is the ledger intact?" a question with a stack trace
    instead of an answer, and the operator asking it is the one who most needs a
    plain reason. Every reason is exercised, because a verifier that reported one
    canned string for every corruption would pass a single-case test while
    telling an operator nothing.

    The tamper is done by the *owner* with the trigger disabled, which is the
    only role that can do it -- and is exactly why ``verify()`` cannot assume the
    database protected itself. Row 2 is the one altered, so the first row still
    verifies and the report says how far the chain was good.
    ``isolated_research_ledger`` drops and rebuilds the database afterwards, so
    the trigger does not have to be re-enabled.
    """

    ledger = _ledger(research_ledger_dsn)
    ledger.append(_preregistered_event(trial_protocol))
    ledger.append(_execution_started_event(trial_protocol.candidates[0].trial_id))

    with psycopg.connect(research_migration_dsn) as connection, connection.cursor() as cursor:
        cursor.execute("SET ROLE trading_house_owner")
        cursor.execute(
            "ALTER TABLE research.trial_ledger_events "
            "DISABLE TRIGGER reject_trial_ledger_row_mutation"
        )
        cursor.execute(f"UPDATE research.trial_ledger_events {statement} WHERE sequence = 2")
        connection.commit()

    report = ledger.verify()

    assert not report.valid
    assert report.reason == reason
    assert report.checked_events == 1
