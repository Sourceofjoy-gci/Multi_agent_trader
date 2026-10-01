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

import json
import traceback
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal
from itertools import pairwise
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import psycopg
import pytest
from psycopg.types.json import Jsonb
from pydantic import SecretStr

import trading_house.research.ledger_store as ledger_store
from trading_house.core.errors import TrialLedgerAppendError, TrialLedgerIntegrityError
from trading_house.database.connection import open_runtime_connection
from trading_house.marketdata.models import Timeframe
from trading_house.ops.ledger import execution_started_event
from trading_house.research.backtest.costs import CostModel
from trading_house.research.canonical import canonical_bytes, canonical_sha256
from trading_house.research.ledger_store import (
    GENESIS_HASH,
    PostgresTrialLedger,
    compute_event_hash,
)
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


def _protocol(candidates: tuple[TrialSpec, ...] | None = None) -> TrialProtocol:
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
        candidates=candidates if candidates is not None else (_candidate(1), _candidate(2)),
    )


def _ledger(dsn: str) -> PostgresTrialLedger:
    return PostgresTrialLedger(lambda: open_runtime_connection(SecretStr(dsn)))


def _attempt_event(
    trial_id: str,
    *,
    event_type: LedgerEventType,
    payload: LedgerEventPayload,
    spec_sha256: str = "d" * 64,
) -> LedgerEvent:
    return LedgerEvent(
        event_id=uuid5(NAMESPACE_URL, f"test:{event_type.value}:{trial_id}"),
        scope_kind=ScopeKind.ATTEMPT,
        scope_id=f"attempt-{trial_id}",
        event_type=event_type,
        trial_id=trial_id,
        attempt_id=f"attempt-{trial_id}",
        spec_sha256=spec_sha256,
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
        payload=payload,
    )


def _declared_spec(trial_id: str) -> TrialSpec:
    """The candidate ``_declared`` seals for a trial id."""

    return TrialSpec(
        trial_id=trial_id,
        spec_id=f"spec-{trial_id}",
        rationale="declared before execution",
        parameter_space=(("window", trial_id),),
    )


def _real_digest(trial_id: str) -> str:
    """The digest a start for this trial must carry: its declared candidate's.

    The one place a test start gets its digest. ``trial-N`` is the default
    protocol's candidate; any other id is the shape ``_declared`` seals.
    """

    by_id = {candidate.trial_id: candidate for candidate in _protocol().candidates}
    return canonical_sha256(by_id.get(trial_id) or _declared_spec(trial_id))


def _execution_started_event(trial_id: str, spec_sha256: str | None = None) -> LedgerEvent:
    return _attempt_event(
        trial_id,
        event_type=LedgerEventType.EXECUTION_STARTED,
        spec_sha256=spec_sha256 or _real_digest(trial_id),
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


def _declared(*trial_ids: str) -> LedgerEvent:
    """One preregistration naming every trial in ``trial_ids``, as one event.

    A start is admissible only against a declared trial, so a test about chain
    mechanics -- a lost race, a sequence gap -- has to declare the trials it
    appends against rather than assert around the guard. Sealing the family in a
    single event is what a real protocol does, and it keeps the chain under test
    the shape an operator's would have.
    """

    return _preregistered_event(
        _protocol(tuple(_declared_spec(trial_id) for trial_id in trial_ids))
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


def test_an_explicit_non_contiguous_sequence_is_a_gap_not_a_break(
    research_ledger_dsn: str,
    research_migration_dsn: str,
) -> None:
    """A row inserted at sequence 99 chains correctly and verifies as intact.

    This is the head-repair procedure the migration's docstring promises, run as
    a test: the owner's row is rebuilt from the tail of the event chain, the head
    is moved to match, and the next append lands on 100. If integrity demanded a
    contiguous sequence, a single repair would be indistinguishable from the
    corruption it was repairing.
    """

    ledger = _ledger(research_ledger_dsn)
    ledger.append(_declared("trial-repaired", "trial-after-repair"))
    previous_hash = bytes.fromhex(ledger.events()[-1].event_hash)
    event = _execution_started_event("trial-repaired")
    canonical_event = canonical_bytes(event)
    event_hash = ledger_store.compute_event_hash(99, previous_hash, canonical_event)

    with psycopg.connect(research_migration_dsn) as connection, connection.cursor() as cursor:
        cursor.execute("SET ROLE trading_house_owner")
        cursor.execute(
            "INSERT INTO research.trial_ledger_events ("
            "sequence, event_id, scope_kind, scope_id, trial_id, attempt_id, event_type, "
            "spec_sha256, canonical_event, event_json, payload_sha256, occurred_at, "
            "previous_hash, event_hash"
            ") VALUES (99, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                event.event_id,
                event.scope_kind.value,
                event.scope_id,
                event.trial_id,
                event.attempt_id,
                event.event_type.value,
                bytes.fromhex(event.spec_sha256),
                canonical_event,
                Jsonb(event.model_dump(mode="json")),
                bytes.fromhex(canonical_sha256(event.payload)),
                event.occurred_at,
                previous_hash,
                event_hash,
            ),
        )
        cursor.execute(
            "UPDATE research.trial_ledger_heads SET last_sequence = 99, last_event_hash = %s "
            "WHERE singleton",
            (event_hash,),
        )
        connection.commit()

    report = ledger.verify()

    assert report.valid
    assert report.checked_events == 2
    assert [record.sequence for record in ledger.events()] == [1, 99]
    # The repaired head is right, not merely tolerated: the next append chains
    # onto it instead of failing its 40001 check forever.
    following = ledger.append(_execution_started_event("trial-after-repair"))
    assert following.sequence == 100
    assert following.previous_hash == event_hash.hex()
    assert ledger.verify().valid


def test_a_renumbered_genesis_row_is_reported_rather_than_accepted(
    research_ledger_dsn: str,
    research_migration_dsn: str,
    trial_protocol: TrialProtocol,
) -> None:
    """The one link in the chain the verifier supplies instead of reading it.

    Every other check is downstream of a row, so renumbering a whole chain and
    recomputing each hash over its new number leaves the ledger perfectly
    self-consistent -- and the database's ``ledger_genesis_previous_hash`` CHECK
    agrees, because it constrains sequence 1 and the renumbering empties it. What
    is left is the claim "this is the first row *and* it says it is genesis", and
    that is the claim the verifier's own anchor holds. Built by the owner with the
    trigger disabled, which is the only role that can rewrite a sequence at all.
    """

    ledger = _ledger(research_ledger_dsn)
    genesis = _preregistered_event(trial_protocol)
    following = _execution_started_event(trial_protocol.candidates[0].trial_id)
    ledger.append(genesis)
    ledger.append(following)

    # Re-derived here rather than read back, because re-deriving it in Python is
    # the point: a chain whose hashes are correct over its forged numbers is
    # exactly the state the previous-hash link alone cannot catch. The link itself
    # is unchanged by a renumber -- only the sequence is in the preimage.
    genesis_hash = compute_event_hash(1, GENESIS_HASH, canonical_bytes(genesis))
    forged = (
        (1, 2, compute_event_hash(2, GENESIS_HASH, canonical_bytes(genesis))),
        (2, 3, compute_event_hash(3, genesis_hash, canonical_bytes(following))),
    )
    with psycopg.connect(research_migration_dsn) as connection, connection.cursor() as cursor:
        cursor.execute("SET ROLE trading_house_owner")
        cursor.execute(
            "ALTER TABLE research.trial_ledger_events "
            "DISABLE TRIGGER reject_trial_ledger_row_mutation"
        )
        # Descending old sequence, because ``sequence`` is the primary key: both
        # new numbers are above the old maximum, so moving the tail first is what
        # makes the in-place renumber need no temporary column.
        for old_sequence, new_sequence, event_hash in sorted(forged, reverse=True):
            cursor.execute(
                "UPDATE research.trial_ledger_events SET event_hash = %s, sequence = %s "
                "WHERE sequence = %s",
                (event_hash, new_sequence, old_sequence),
            )
        connection.commit()

    # The chain now says something coherent and false, which is the state a
    # verifier has to catch: both links and both hashes agree.
    assert [record.sequence for record in ledger.events()] == [2, 3]
    report = ledger.verify()

    assert not report.valid
    assert report.reason == "genesis_sequence_mismatch"
    assert report.checked_events == 0


def test_concurrent_appends_form_one_continuous_chain(research_ledger_dsn: str) -> None:
    """Eight writers, eight connections, one chain.

    Each writer reads the head before it appends, so most of them lose the race
    to the advisory lock and are told 40001. Retrying the *same* deterministic
    event is what keeps the chain whole; minting a new event id per attempt is
    what would fork it. The protocol ahead of them is there because a start is
    admissible only against a declared trial, so the eight writers declare the
    family they are about to run -- one event, as a real protocol does.
    """

    ledger = _ledger(research_ledger_dsn)
    ledger.append(_declared(*(f"t{index}" for index in range(WRITERS))))

    with ThreadPoolExecutor(max_workers=WRITERS) as pool:
        appended = list(
            pool.map(
                lambda index: ledger.append(_execution_started_event(f"t{index}")),
                range(WRITERS),
            )
        )

    ordered = ledger.events()

    assert [record.sequence for record in ordered] == list(range(1, WRITERS + 2))
    assert ordered == tuple(sorted((*appended, ordered[0]), key=lambda record: record.sequence))
    assert len({record.event_id for record in ordered}) == WRITERS + 1
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
        # Two trials, each started with its own declared digest -- the ledger no
        # longer admits the single arbitrary digest this test used to share.
        effective_specifications=2,
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


def test_the_append_function_refuses_a_stale_expected_head(
    research_ledger_dsn: str, trial_protocol: TrialProtocol
) -> None:
    """40001 is the only error the store retries, so it has to mean one thing.

    Handing the function a head that has moved on is a lost race, and it is
    reported as one. Appending anyway would let a caller believe its event
    chained onto a head that no longer exists.

    The event has to be one that is *not* already in the chain: an id the
    function recognises is a successful retry, checked before the head, and
    would answer with a row instead of the error.
    """

    _ledger(research_ledger_dsn).append(_preregistered_event(trial_protocol))
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


def test_an_unreadable_ledger_is_an_integrity_error_not_a_clean_verdict(
    research_ledger_dsn: str,
    trial_protocol: TrialProtocol,
) -> None:
    """ "Nobody could check" must never be reported as "nothing is wrong".

    A script that reads ``report.valid`` would otherwise treat an unreachable
    database as an intact ledger, which is the one answer this ledger exists to
    be unable to give. It is a different error from a refused append -- one means
    this trial was not recorded, the other means nobody can currently say
    whether any of it is -- and the two have different exit codes.
    """

    marker = "sensitive-verify-driver-marker"
    _ledger(research_ledger_dsn).append(_preregistered_event(trial_protocol))

    def failing_factory() -> psycopg.Connection[Any]:
        raise psycopg.OperationalError(marker)

    with pytest.raises(TrialLedgerIntegrityError) as raised:
        PostgresTrialLedger(failing_factory).verify()

    error = raised.value
    assert error.args == ("trial ledger integrity verification failed",)
    assert not isinstance(error.__cause__, psycopg.Error)
    assert error.__context__ is None
    rendered = "".join(traceback.format_exception(error))
    assert marker not in rendered
    assert research_ledger_dsn not in rendered


def test_reads_that_fail_stay_append_errors(research_ledger_dsn: str) -> None:
    """The repository pattern, deliberately unchanged.

    ``events``/``replay``/``counters``/``declares_trial`` have no second typed
    error to raise, and inventing one here would give a caller two ways to be
    told "the database was not there". Only ``verify`` has a second meaning to
    distinguish -- and ``declares_trial`` is here for the specific reason that a
    preflight must not be able to answer "no trial was declared" for a ledger it
    could not read.
    """

    def failing_factory() -> psycopg.Connection[Any]:
        raise psycopg.OperationalError("sensitive-read-driver-marker")

    ledger = PostgresTrialLedger(failing_factory)

    with pytest.raises(TrialLedgerAppendError):
        ledger.events()
    with pytest.raises(TrialLedgerAppendError):
        ledger.events_for("trial-1")
    with pytest.raises(TrialLedgerAppendError):
        ledger.replay()
    with pytest.raises(TrialLedgerAppendError):
        ledger.counters()
    with pytest.raises(TrialLedgerAppendError):
        ledger.declares_trial("trial-1")


def test_a_row_the_parser_cannot_read_becomes_an_append_error(
    research_ledger_dsn: str,
    trial_protocol: TrialProtocol,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed row is a store failure, not a ``ValidationError`` for the CLI.

    ``LedgerRecord`` is strict and closed, so a row the store cannot turn into a
    model is a database this build does not understand. Letting that escape would
    break the exit-code contract for a condition the caller cannot act on any
    differently -- and there is no way to build one through the function, so the
    shape is injected here rather than waited for.
    """

    ledger = _ledger(research_ledger_dsn)
    record = ledger.append(_preregistered_event(trial_protocol))
    monkeypatch.setattr(ledger_store, "_read_rows", lambda *_args, **_kwargs: (("short",),))

    with pytest.raises(TrialLedgerAppendError) as raised:
        ledger.events()
    with pytest.raises(TrialLedgerAppendError):
        ledger.events_for(record.trial_id or "trial-1")
    with pytest.raises(TrialLedgerAppendError):
        ledger.replay()

    assert raised.value.args == ("trial ledger append failed",)
    assert "short" not in "".join(traceback.format_exception(raised.value))


def test_canonical_bytes_that_do_not_parse_become_an_append_error(
    research_ledger_dsn: str,
    research_migration_dsn: str,
    trial_protocol: TrialProtocol,
) -> None:
    """Valid JSON that is not an event, stored by the only role that can store it.

    The bytes are what ``replay`` parses, so this is the shape a tampered row
    takes in practice. ``verify`` answers it as a report; ``replay`` cannot,
    because there is no event to hand back, so it raises the store's one error.
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
        cursor.execute(
            "UPDATE research.trial_ledger_events SET canonical_event = "
            "pg_catalog.convert_to('{\"not\": \"an event\"}', 'UTF8') WHERE sequence = 1"
        )
        connection.commit()

    with pytest.raises(TrialLedgerAppendError):
        ledger.replay()
    with pytest.raises(TrialLedgerAppendError):
        ledger.counters()
    assert ledger.verify().reason == "event_schema_invalid"


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
            "SET event_id = pg_catalog.gen_random_uuid()",
            "identity_column_mismatch",
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
        "identity-column",
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

    The ``identity-column`` case is the one the jsonb check alone cannot catch.
    Every identity column is projected from ``event_json`` by the append function,
    so a column that drifts leaves the jsonb agreeing with the canonical bytes and
    the row still looking intact -- the chain, the digests and the payload all
    check out, and the row's own ``event_id`` is not the event it stores.

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


# --- Phase 8A.1: a start's digest is vouched for at append time ------------------

STARTED_AT = datetime(2026, 1, 1, tzinfo=UTC)


def canonical_json_bytes(document: dict[str, Any]) -> bytes:
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode()


UNDECLARED = "d" * 64


def _start(trial_id: str, attempt_id: str, digest: str) -> LedgerEvent:
    return execution_started_event(trial_id, attempt_id, digest, STARTED_AT)


def _variant(trial_id: str, window: str) -> TrialProtocol:
    """A second protocol declaring the same trial id under a different specification."""

    spec = TrialSpec(
        trial_id=trial_id,
        spec_id=f"spec-{window}",
        rationale="declared before execution",
        parameter_space=(("window", window),),
    )
    return _protocol((spec,))


def test_a_start_carrying_the_declared_digest_is_appended(
    research_ledger_dsn: str, trial_protocol: TrialProtocol
) -> None:
    ledger = _ledger(research_ledger_dsn)
    ledger.register(trial_protocol)
    candidate = trial_protocol.candidates[0]

    record = ledger.append(_start(candidate.trial_id, "a-1", canonical_sha256(candidate)))

    assert record.event_type is LedgerEventType.EXECUTION_STARTED
    assert record.spec_sha256 == canonical_sha256(candidate)
    assert ledger.verify().valid


def test_a_start_whose_digest_no_registration_declares_is_refused_and_appends_nothing(
    research_ledger_dsn: str, trial_protocol: TrialProtocol
) -> None:
    ledger = _ledger(research_ledger_dsn)
    ledger.register(trial_protocol)
    trial_id = trial_protocol.candidates[0].trial_id
    before = ledger.events()

    # The *other* candidate's digest and the protocol's own digest are both real
    # digests in this chain, and neither is this trial's specification.
    for digest in (
        UNDECLARED,
        canonical_sha256(trial_protocol.candidates[1]),
        canonical_sha256(trial_protocol),
    ):
        with pytest.raises(TrialLedgerAppendError) as refused:
            ledger.append(_start(trial_id, "a-1", digest))
        # Guards only against chaining a driver error (whose text can carry the
        # DSN); the refusal's own cause is the fixed store failure.
        assert research_ledger_dsn not in "".join(traceback.format_exception(refused.value))

    assert ledger.events() == before
    assert ledger.counters().effective_specifications == 0


def test_every_registration_of_a_trial_is_a_declaration_and_a_third_digest_is_refused(
    research_ledger_dsn: str,
) -> None:
    """V-2: the set across registrations, not the first one found."""

    ledger = _ledger(research_ledger_dsn)
    first, second = _variant("trial-x", "one"), _variant("trial-x", "two")
    ledger.register(first)
    ledger.register(second)
    digest_one = canonical_sha256(first.candidates[0])
    digest_two = canonical_sha256(second.candidates[0])

    ledger.append(_start("trial-x", "a-1", digest_one))
    ledger.append(_start("trial-x", "a-2", digest_two))
    with pytest.raises(TrialLedgerAppendError):
        ledger.append(_start("trial-x", "a-3", UNDECLARED))

    assert ledger.counters().effective_specifications == 2
    assert ledger.verify().valid


def test_a_trial_declared_only_by_a_legacy_import_keeps_its_start_unchecked(
    research_ledger_dsn: str,
) -> None:
    ledger = _ledger(research_ledger_dsn)
    ledger.append(_legacy_event("trial-legacy"))

    record = ledger.append(_start("trial-legacy", "a-1", UNDECLARED))

    assert record.spec_sha256 == UNDECLARED
    assert ledger.verify().valid


def test_retrying_an_appended_start_succeeds_and_adds_nothing(
    research_ledger_dsn: str, trial_protocol: TrialProtocol
) -> None:
    ledger = _ledger(research_ledger_dsn)
    ledger.register(trial_protocol)
    candidate = trial_protocol.candidates[0]
    event = _start(candidate.trial_id, "a-1", canonical_sha256(candidate))

    first = ledger.append(event)
    # A later registration must not turn the first start into something the
    # retry path refuses: the digest that passed once is still declared.
    ledger.register(_variant(candidate.trial_id, "later"))
    second = ledger.append(event)

    assert second == first
    assert [record.event_type for record in ledger.events()] == [
        LedgerEventType.PREREGISTERED,
        LedgerEventType.EXECUTION_STARTED,
        LedgerEventType.PREREGISTERED,
    ]
    assert ledger.verify().valid


def test_history_holding_a_drifted_start_still_replays_and_verifies(
    research_ledger_dsn: str, trial_protocol: TrialProtocol
) -> None:
    """Events already in the chain are never re-judged.

    The drifted row is written through the database's own append function, which
    is the path every row took before the store checked anything, so this is the
    local chain's ``att-b2`` shape rather than a forged one.
    """

    ledger = _ledger(research_ledger_dsn)
    ledger.register(trial_protocol)
    trial_id = trial_protocol.candidates[0].trial_id
    ledger.append(_start(trial_id, "a-1", canonical_sha256(trial_protocol.candidates[0])))
    drifted = _start(trial_id, "a-2", UNDECLARED)
    with pytest.raises(TrialLedgerAppendError):
        ledger.append(drifted)
    _raw_append(research_ledger_dsn, ledger, drifted)

    report = ledger.verify()
    assert report.valid
    assert report.checked_events == 3
    assert len(ledger.replay()) == 3
    assert ledger.counters().effective_specifications == 2


def _raw_append(dsn: str, ledger: PostgresTrialLedger, event: LedgerEvent) -> None:
    """Write an event through the database's append function, bypassing the store's check.

    The path every row took before 8A.1, so a row built here is history rather
    than a forgery.
    """

    tail = bytes.fromhex(ledger.events()[-1].event_hash)
    with open_runtime_connection(SecretStr(dsn)) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM research.append_trial_ledger_event(%s, %s, %s, %s)",
                (
                    canonical_bytes(event),
                    Jsonb(event.model_dump(mode="json")),
                    tail,
                    bytes.fromhex(canonical_sha256(event.payload)),
                ),
            )
        connection.commit()


def test_a_drifted_start_already_in_the_chain_can_be_retried_byte_identical(
    research_ledger_dsn: str, trial_protocol: TrialProtocol
) -> None:
    """Scenario A: the vouch must not refuse what is already history."""

    ledger = _ledger(research_ledger_dsn)
    ledger.register(trial_protocol)
    drifted = _start(trial_protocol.candidates[0].trial_id, "a-1", UNDECLARED)
    _raw_append(research_ledger_dsn, ledger, drifted)
    before = ledger.events()

    again = ledger.append(drifted)

    assert again == before[-1]
    assert ledger.events() == before


def test_a_start_whose_trial_was_registered_after_it_can_still_be_retried(
    research_ledger_dsn: str,
) -> None:
    """Scenario B, and the legacy-and-registered rule: a registration binds from then on."""

    ledger = _ledger(research_ledger_dsn)
    ledger.append(_legacy_event("trial-lb"))
    start = _start("trial-lb", "a-1", UNDECLARED)
    first = ledger.append(start)
    registered = _variant("trial-lb", "later")
    ledger.register(registered)
    before = ledger.events()

    assert ledger.append(start) == first
    assert ledger.events() == before
    # Both legacy and registered now: vouched against the registration.
    with pytest.raises(TrialLedgerAppendError):
        ledger.append(_start("trial-lb", "a-2", UNDECLARED))
    ledger.append(_start("trial-lb", "a-3", canonical_sha256(registered.candidates[0])))


def test_a_same_id_start_with_different_bytes_is_still_refused(
    research_ledger_dsn: str, trial_protocol: TrialProtocol
) -> None:
    ledger = _ledger(research_ledger_dsn)
    ledger.register(trial_protocol)
    trial_id = trial_protocol.candidates[0].trial_id
    _raw_append(research_ledger_dsn, ledger, _start(trial_id, "a-1", UNDECLARED))
    before = ledger.events()
    other_bytes = execution_started_event(
        trial_id, "a-1", UNDECLARED, datetime(2026, 2, 2, tzinfo=UTC)
    )

    with pytest.raises(TrialLedgerAppendError):
        ledger.append(other_bytes)

    assert ledger.events() == before


def test_the_retry_skip_compares_the_bytes_and_not_only_the_id(
    research_ledger_dsn: str, trial_protocol: TrialProtocol
) -> None:
    """The database's own conflict refusal would also stop a same-id event, so the
    end-to-end case above cannot tell a byte comparison from an id comparison; this
    pins the skip's own predicate."""

    ledger = _ledger(research_ledger_dsn)
    ledger.register(trial_protocol)
    event = _start(trial_protocol.candidates[0].trial_id, "a-1", UNDECLARED)
    _raw_append(research_ledger_dsn, ledger, event)
    changed = canonical_bytes(
        execution_started_event(
            trial_protocol.candidates[0].trial_id,
            "a-1",
            UNDECLARED,
            datetime(2026, 2, 2, tzinfo=UTC),
        )
    )

    with (
        open_runtime_connection(SecretStr(research_ledger_dsn)) as connection,
        connection.cursor() as cursor,
    ):
        assert ledger_store._already_appended(cursor, event.event_id, canonical_bytes(event))
        assert not ledger_store._already_appended(cursor, event.event_id, changed)


def test_a_stored_protocol_that_does_not_parse_refuses_the_start(
    research_ledger_dsn: str, trial_protocol: TrialProtocol
) -> None:
    """Fail closed: a declaration that cannot be read vouches for nothing."""

    ledger = _ledger(research_ledger_dsn)
    broken = _preregistered_event(trial_protocol).model_dump(mode="json")
    del broken["payload"]["protocol"]["candidates"][0]["rationale"]
    tail = GENESIS_HASH
    with open_runtime_connection(SecretStr(research_ledger_dsn)) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM research.append_trial_ledger_event(%s, %s, %s, %s)",
                (canonical_json_bytes(broken), Jsonb(broken), tail, b"p" * 32),
            )
        connection.commit()
    candidate = trial_protocol.candidates[0]

    with pytest.raises(TrialLedgerAppendError):
        ledger.append(_start(candidate.trial_id, "a-1", canonical_sha256(candidate)))

    assert len(ledger.events()) == 1


def test_vouching_reads_on_the_connection_the_append_runs_on(
    research_ledger_dsn: str, trial_protocol: TrialProtocol
) -> None:
    """V-5: one connection per append, so the check and the row share a transaction.

    A check made on a second connection would read a different snapshot from the
    one the row is chained onto, and a registration landing between the two could
    be missed or invented. Counting the connections the factory is asked for is
    what makes "same transaction" a measured fact.
    """

    opened = []

    def counting_factory() -> psycopg.Connection[Any]:
        opened.append(1)
        return open_runtime_connection(SecretStr(research_ledger_dsn))

    ledger = PostgresTrialLedger(counting_factory)
    ledger.register(trial_protocol)
    candidate = trial_protocol.candidates[0]
    opened.clear()

    ledger.append(_start(candidate.trial_id, "a-1", canonical_sha256(candidate)))

    assert len(opened) == 1
