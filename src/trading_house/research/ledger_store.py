"""The trial ledger's PostgreSQL repository: append, read, replay, verify.

The audit repository is the template for every decision here, because it
already answers the question this chain asks -- how does a process append to a
hash chain it is not allowed to hold the keys to? -- and its answers were
reviewed. Three of them are worth restating:

* **One connection per operation, committed by the context manager.** The store
  never calls ``commit`` or ``rollback``; a transaction that ends because an
  exception left the ``with`` block is the only rollback there is, so a failed
  append cannot leave a partial row behind.
* **Failures are one error.** Every driver failure becomes
  ``TrialLedgerAppendError`` with a credential-free private cause. The driver's
  own message names the constraint or the connection string, which is exactly
  what must not be echoed out of a refused append.
* **The chain is the authority, not a summary row.** The head the store submits
  is re-derived from the last event in the chain rather than read from
  ``research.trial_ledger_heads``, so no role is trusted to keep a mutable
  counter in step with the events. The heads table exists for the function to
  lock under the advisory lock, and the runtime holds no privilege on it.

The optimistic read-then-append is what makes concurrent writers work without a
client-side lock: each writer reads the head, submits it, and is told 40001 if
somebody else committed in between. Retrying the *same* event is safe because
an event's identity is derived from its content, so the function recognises a
retry by id before it compares heads. Retrying a *new* event id would be a
different event, and that is a caller's bug rather than a race to paper over.

``payload_sha256`` is the one column the client supplies rather than the database
computing, and the reason is in the migration: the digest is over the canonical
payload bytes, which PostgreSQL's JSON text form is not. It is therefore *not* in
the chain preimage -- a client could store a wrong payload digest and still chain
correctly -- which is precisely why ``verify`` re-derives it and reports
``payload_digest_mismatch`` rather than leaving the column on trust.

The same reasoning applies to every column the append function projects out of
``event_json``: the chain hash covers the canonical bytes, so a projected column
that drifts leaves the row chaining perfectly while describing a different event.
``verify`` therefore re-projects them and reports ``identity_column_mismatch``.
It is the one check that needed a database rather than a code reading to find,
which is why the tampering test for it is integration-only: the jsonb check passes
on exactly the row this catches.

One more check is a consequence of that same reading. The genesis row's previous
hash is the one link in the chain the verifier supplies rather than reads off the
row before it, and the database's CHECK pins 32 zero bytes to ``sequence = 1``
alone -- so a chain whose first row has been renumbered satisfies every hash
check while claiming a genesis it never had. ``verify`` therefore anchors the
first row's sequence at 1 and reports ``genesis_sequence_mismatch``; every other
sequence is still free to skip, because a burned number is a gap rather than a
break.
"""

from __future__ import annotations

import hashlib
import json
import struct
from collections.abc import Callable
from typing import Any, Never, Protocol
from uuid import NAMESPACE_URL, uuid5

import psycopg
from psycopg.types.json import Jsonb

from trading_house.core.errors import TrialLedgerAppendError, TrialLedgerIntegrityError
from trading_house.research.canonical import canonical_bytes, canonical_sha256
from trading_house.research.trial_ledger import (
    LedgerEvent,
    LedgerEventType,
    LedgerIntegrityReport,
    LedgerRecord,
    PreregisteredPayload,
    RegistrationState,
    ScopeKind,
    TrialCounters,
    TrialProtocol,
    TrialSpec,
    trial_counters,
)

# The chain's own separator, and not ``research.canonical``'s: this chain links
# a sequence and a previous hash, so its preimage has three parts the evidence
# chain's has not got. Matching the value migration 0007 hashes with is the only
# thing that keeps a Python-computed hash equal to a database-computed one, and
# every ``verify()`` that passes is that agreement being tested.
DOMAIN_SEPARATOR = b"trading-house:trial-ledger:v1"
GENESIS_HASH = bytes(32)

# ponytail: eight writers can each lose the race to the seven that commit ahead
# of them, so a three-retry budget can refuse a run that raced nothing. This is
# a liveness budget, not a safety property -- the chain check is what keeps the
# ledger correct, and every attempt still submits the same deterministic event.
MAX_STALE_HEAD_RETRIES = 8

# The column list is written out in full, in the table's own order, and the row
# indexes below are positions in it. ``SELECT *`` from the append function uses
# that same order, so the two readers cannot disagree about a column -- and a
# column list that did drift would fail every test that reads a record, rather
# than quietly returning the wrong digest.
_ROW_WIDTH = 17
_ALL_EVENTS_SQL = (
    "SELECT sequence, event_id, scope_kind, scope_id, trial_id, attempt_id, event_type, "
    "spec_sha256, canonical_event, event_json, payload_sha256, occurred_at, "
    "recorded_at, previous_hash, event_hash, legacy_import, legacy_reason "
    "FROM research.trial_ledger_events ORDER BY sequence"
)
_TRIAL_EVENTS_SQL = (
    "SELECT sequence, event_id, scope_kind, scope_id, trial_id, attempt_id, event_type, "
    "spec_sha256, canonical_event, event_json, payload_sha256, occurred_at, "
    "recorded_at, previous_hash, event_hash, legacy_import, legacy_reason "
    "FROM research.trial_ledger_events WHERE trial_id = %s ORDER BY sequence"
)
_TAIL_HASH_SQL = (
    "SELECT event_hash FROM research.trial_ledger_events ORDER BY sequence DESC LIMIT 1"
)
# An outcome recorded against a trial is only admissible if that trial was
# declared, either by a preregistered protocol naming it as a candidate or by an
# explicit legacy import. Containment, not a column: a protocol seals its whole
# candidate family inside one event, so "was this trial declared" is a question
# about the payload's contents.
_LINEAGE_SQL = """
SELECT EXISTS (
    SELECT 1
    FROM research.trial_ledger_events
    WHERE (event_type = 'legacy_imported' AND trial_id = %s)
       OR (event_type = 'preregistered' AND event_json @> %s::pg_catalog.jsonb)
)
"""
# The registrations that declare a trial, as their canonical bytes: the candidate
# family is parsed from the chain's own representation, like every other reader.
# The containment document is ``_lineage_parameters``' second argument, so this
# read and ``_LINEAGE_SQL`` ask about the same set of events.
_DECLARING_PROTOCOLS_SQL = (
    "SELECT canonical_event FROM research.trial_ledger_events "
    "WHERE event_type = 'preregistered' AND event_json @> %s::pg_catalog.jsonb"
)
# ponytail: the containment arm is a sequential scan of a chain that is one row per
# trial, not per candidate. A GIN index on event_json turns it into a lookup; do that
# when a chain is large enough to measure, not before.
#
# The event types that may not be appended for a trial with no preregistration and
# no legacy lineage. A start belongs here rather than only with the outcomes
# because an execution nobody declared is still a draw from the search space: it
# is the one row here whose absence from the chain would be invisible, because
# ``record`` is only ever reached by somebody who already holds an attempt id.
_REQUIRES_REGISTRATION = frozenset(
    {
        LedgerEventType.EXECUTION_STARTED,
        LedgerEventType.RESULT_RECORDED,
        LedgerEventType.FAILED,
        LedgerEventType.EVIDENCE_SEALED,
    }
)


class ConnectionFactory(Protocol):
    """Open one distinct runtime connection for a repository operation."""

    def __call__(self) -> psycopg.Connection[tuple[Any, ...]]: ...


class _LedgerStoreFailure(Exception):
    """Credential- and event-free diagnostic cause for store failures."""


class _OperationFailure:
    """Non-sensitive sentinel returned after discarding an operation failure."""


class _StaleHead(_OperationFailure):
    """A concurrent append moved the head; the same event may be retried."""


class _UnregisteredTrial(Exception):
    """Internal marker: the trial this outcome names was never declared."""


class _UnvouchedSpecification(Exception):
    """Internal marker: a start's digest is not one its trial's registrations declare."""


_OPERATION_FAILED = _OperationFailure()
_STALE_HEAD = _StaleHead()


def compute_event_hash(sequence: int, previous_hash: bytes, canonical_event: bytes) -> bytes:
    """Hash one ledger event the way ``0007`` does, byte for byte.

    Signed big-endian int64 for the sequence, because that is what
    ``pg_catalog.int8send`` produces; anything else is a different preimage and
    a verifier that agreed with itself while disagreeing with the database would
    be worse than no verifier at all.
    """

    return hashlib.sha256(
        DOMAIN_SEPARATOR + struct.pack(">q", sequence) + previous_hash + canonical_event
    ).digest()


def _event_from_canonical(canonical_event: bytes) -> LedgerEvent:
    """Rebuild an event from the bytes, not from the jsonb.

    The canonical bytes are the chain's own representation, and the function
    refuses to store a row whose bytes and jsonb disagree -- so parsing the
    bytes is parsing the authority. It also sidesteps a trap the models set
    deliberately: ``CanonicalModel`` is strict, so a UUID or a timestamp that
    arrives as a JSON string is only coerced on the JSON path, and
    ``model_validate`` on the jsonb column would refuse every row.
    """

    return LedgerEvent.model_validate_json(canonical_event)


def _event_from_row(row: tuple[Any, ...]) -> LedgerEvent:
    return _event_from_canonical(row[8])


def _record_from_row(row: tuple[Any, ...] | None) -> LedgerRecord:
    if row is None or len(row) != _ROW_WIDTH:
        raise ValueError("trial ledger database returned an invalid row")
    return LedgerRecord(
        sequence=row[0],
        event_id=row[1],
        # The CHECK constraints make these two closed sets in the database and
        # the models make them closed in Python, so a value the column accepted
        # and the model does not is a schema change, not a row to guess at: it
        # raises here and becomes one TrialLedgerAppendError.
        scope_kind=ScopeKind(row[2]),
        scope_id=row[3],
        trial_id=row[4],
        attempt_id=row[5],
        event_type=LedgerEventType(row[6]),
        spec_sha256=bytes(row[7]).hex(),
        event_json=row[9],
        payload_sha256=bytes(row[10]).hex(),
        previous_hash=bytes(row[13]).hex(),
        event_hash=bytes(row[14]).hex(),
        legacy=row[15],
        legacy_reason=row[16],
        recorded_at=row[12],
    )


def _close_connection(connection: psycopg.Connection[tuple[Any, ...]]) -> bool:
    try:
        connection.close()
    except Exception:
        return False
    return True


def _read_rows(
    connection_factory: ConnectionFactory,
    query: str,
    parameters: tuple[Any, ...] | None,
) -> tuple[tuple[Any, ...], ...] | _OperationFailure:
    connection: psycopg.Connection[tuple[Any, ...]] | None = None
    outcome: tuple[tuple[Any, ...], ...] | _OperationFailure = _OPERATION_FAILED
    try:
        connection = connection_factory()
        with connection, connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute(query, parameters)
            outcome = tuple(cursor.fetchall())
    except Exception:
        outcome = _OPERATION_FAILED
    finally:
        if connection is not None and not _close_connection(connection):
            outcome = _OPERATION_FAILED
    return outcome


def _lineage_parameters(trial_id: str) -> tuple[str, Jsonb]:
    """The two arguments ``_LINEAGE_SQL`` takes, in its own order.

    One place, because the containment question asked by the append guard and the
    one asked by ``declares_trial`` have to be the *same* question -- two copies of
    the containment document would eventually disagree, and the disagreement would
    be invisible until a trial was admitted by one and refused by the other.
    """

    return (trial_id, Jsonb({"payload": {"protocol": {"candidates": [{"trial_id": trial_id}]}}}))


def _is_registered(cursor: psycopg.Cursor[tuple[Any, ...]], trial_id: str) -> bool:
    cursor.execute(_LINEAGE_SQL, _lineage_parameters(trial_id))
    row = cursor.fetchone()
    return bool(row is not None and row[0])


def _start_digest_is_vouched(
    cursor: psycopg.Cursor[tuple[Any, ...]], trial_id: str, spec_sha256: str
) -> bool:
    """Whether a start's digest is one some registration declares for its trial.

    The set of digests across *every* registration naming the trial, because a
    trial may be registered by more than one protocol and the honest reading of
    that is "any declared specification", not "the first one found". A trial
    declared only by a legacy import has no registration to compare with and is
    exempt. Runs on the append's own cursor, so it reads the chain the row is
    about to extend. A stored protocol that does not parse raises, which the
    caller turns into a refused append rather than a pass.
    """

    cursor.execute(_DECLARING_PROTOCOLS_SQL, (_lineage_parameters(trial_id)[1],))
    rows = cursor.fetchall()
    if not rows:
        return True
    declared = {
        canonical_sha256(candidate)
        for row in rows
        for candidate in _preregistered_candidates(_event_from_canonical(bytes(row[0])))
        if candidate.trial_id == trial_id
    }
    return spec_sha256 in declared


def _preregistered_candidates(event: LedgerEvent) -> tuple[TrialSpec, ...]:
    payload = event.payload
    return payload.protocol.candidates if isinstance(payload, PreregisteredPayload) else ()


def _tail_hash(cursor: psycopg.Cursor[tuple[Any, ...]]) -> bytes:
    cursor.execute(_TAIL_HASH_SQL)
    row = cursor.fetchone()
    return GENESIS_HASH if row is None else bytes(row[0])


def _append_operation(
    connection_factory: ConnectionFactory,
    event: LedgerEvent,
) -> LedgerRecord | _OperationFailure:
    connection: psycopg.Connection[tuple[Any, ...]] | None = None
    outcome: LedgerRecord | _OperationFailure = _OPERATION_FAILED
    try:
        canonical_event = canonical_bytes(event)
        event_json = event.model_dump(mode="json")
        payload_sha256 = bytes.fromhex(canonical_sha256(event.payload))
        connection = connection_factory()
        with connection, connection.cursor() as cursor:
            # An event that names no trial names nothing at all: it is
            # unattributable to any protocol and a permanent row nobody can
            # remove, and a start counted without its trial would raise the audit
            # count while leaving the lottery it was drawn from unnamed -- the
            # worst shape a row in this chain can have. Treated as unregistered.
            unregistered = event.event_type in _REQUIRES_REGISTRATION and (
                event.trial_id is None or not _is_registered(cursor, event.trial_id)
            )
            if unregistered:
                raise _UnregisteredTrial()
            # The one event type whose digest feeds ``trial_counters`` is vouched
            # for here, where the row is written. Events already in the chain are
            # never re-judged: replay and verify do not come through this path.
            if (
                event.event_type is LedgerEventType.EXECUTION_STARTED
                and event.trial_id is not None
                and not _start_digest_is_vouched(cursor, event.trial_id, event.spec_sha256)
            ):
                raise _UnvouchedSpecification()
            expected_previous_hash = _tail_hash(cursor)
            cursor.execute(
                "SELECT * FROM research.append_trial_ledger_event(%s, %s, %s, %s)",
                (canonical_event, Jsonb(event_json), expected_previous_hash, payload_sha256),
            )
            outcome = _record_from_row(cursor.fetchone())
    except psycopg.errors.SerializationFailure:
        # The only retryable failure: somebody else appended between this
        # writer's head read and its append. Everything else -- a duplicate
        # event, a constraint, an unreachable database -- is an answer, and
        # asking again would only get the same one.
        outcome = _STALE_HEAD
    except Exception:
        outcome = _OPERATION_FAILED
    finally:
        if connection is not None and not _close_connection(connection):
            outcome = _OPERATION_FAILED
    return outcome


def _raise_trial_ledger_append_error() -> Never:
    raise TrialLedgerAppendError() from _LedgerStoreFailure("trial ledger store operation failed")


def _raise_trial_ledger_integrity_error() -> Never:
    """The ledger could not be *read*, which is not a verdict on it.

    A detected corruption is a ``LedgerIntegrityReport`` an operator can read; a
    database that would not answer is a failure of the command, and reporting it
    as anything else would let a script treat "nobody could check" as "nothing is
    wrong". Same redaction as the append error: the driver's message names the
    connection, and stays on the private cause.
    """

    raise TrialLedgerIntegrityError() from _LedgerStoreFailure(
        "trial ledger store operation failed"
    )


def _rows_mapped[T](
    rows: tuple[tuple[Any, ...], ...], convert: Callable[[tuple[Any, ...]], T]
) -> tuple[T, ...]:
    """Map rows through a parser, turning a parser failure into one typed error.

    A row the store cannot turn into a model is not a ``ValidationError`` a CLI
    should have to know about: it is an append that cannot be honoured, and
    ``_raise_trial_ledger_append_error`` is the only error this repository's
    callers catch. Letting the raw error escape would break the exit-code
    contract for a condition the caller cannot act on differently.
    """

    try:
        return tuple(convert(row) for row in rows)
    except Exception:
        _raise_trial_ledger_append_error()


class PostgresTrialLedger:
    """Append and read the trial ledger through PostgreSQL transactions."""

    def __init__(self, connection_factory: ConnectionFactory) -> None:
        self._connection_factory = connection_factory

    def register(self, protocol: TrialProtocol) -> tuple[TrialSpec, ...]:
        """Seal one protocol with its whole candidate family, and return it.

        The event id is derived from the protocol's canonical digest, so
        registering the same protocol twice is one event read back rather than a
        second registration -- and a protocol that differs in any byte is a
        different id, not a conflict. ``occurred_at`` is the end of the data
        window the protocol declares, for the same reason: a wall-clock stamp
        would make the retry's canonical bytes differ from the original's, and
        the idempotency would be an accident of timing instead of a property.
        """

        digest = canonical_sha256(protocol)
        self.append(
            LedgerEvent(
                event_id=uuid5(NAMESPACE_URL, f"trading-house:trial-protocol:{digest}"),
                scope_kind=ScopeKind.PROTOCOL,
                scope_id=protocol.protocol_id,
                event_type=LedgerEventType.PREREGISTERED,
                spec_sha256=digest,
                occurred_at=protocol.data.end,
                payload=PreregisteredPayload(
                    event_type=LedgerEventType.PREREGISTERED,
                    protocol=protocol,
                    registration_state=RegistrationState.PROSPECTIVE,
                ),
            )
        )
        return protocol.candidates

    def append(self, event: LedgerEvent) -> LedgerRecord:
        """Append one canonical event, retrying only a lost race."""

        for _ in range(MAX_STALE_HEAD_RETRIES + 1):
            outcome = _append_operation(self._connection_factory, event)
            if not isinstance(outcome, _OperationFailure):
                return outcome
            if not isinstance(outcome, _StaleHead):
                break
        _raise_trial_ledger_append_error()

    def events(self) -> tuple[LedgerRecord, ...]:
        """Return the whole chain from a short, read-only transaction."""

        return self._records(_ALL_EVENTS_SQL, None)

    def events_for(self, trial_id: str) -> tuple[LedgerRecord, ...]:
        """Return one trial's events, in chain order, through the index."""

        return self._records(_TRIAL_EVENTS_SQL, (trial_id,))

    def declares_trial(self, trial_id: str) -> bool:
        """Whether any lineage row already declares this trial.

        The same containment question ``append`` asks before it lets an outcome
        through, exposed as its own read so a caller that writes something
        *beside* the chain can ask it first. It is a preflight and not the rule:
        the chain is append-only, so a declaration cannot be withdrawn between the
        two reads, and ``append``'s own check is what refuses a caller that
        skipped this entirely -- which is why nothing here is permission.
        """

        rows = _read_rows(self._connection_factory, _LINEAGE_SQL, _lineage_parameters(trial_id))
        if isinstance(rows, _OperationFailure):
            _raise_trial_ledger_append_error()
        return bool(rows) and rows[0][0] is True

    def replay(self) -> tuple[LedgerEvent, ...]:
        """Return the chain as validated events, for counters and readers."""

        rows = _read_rows(self._connection_factory, _ALL_EVENTS_SQL, None)
        if isinstance(rows, _OperationFailure):
            _raise_trial_ledger_append_error()
        return _rows_mapped(rows, _event_from_row)

    def counters(self) -> TrialCounters:
        """The deflation denominator, counted from the chain rather than kept."""

        return trial_counters(self.replay())

    def verify(self) -> LedgerIntegrityReport:
        """Re-derive every hash in the chain from the bytes PostgreSQL stored.

        Answers rather than raises, because "is the ledger intact?" is a
        question an operator asks and needs a plain reason back. The one thing
        that is not an answer is not being able to read the ledger at all, which
        is why that raises ``TrialLedgerIntegrityError`` instead of reporting.
        """

        rows = _read_rows(self._connection_factory, _ALL_EVENTS_SQL, None)
        if isinstance(rows, _OperationFailure):
            _raise_trial_ledger_integrity_error()
        return _verify_rows(rows)

    def _records(self, query: str, parameters: tuple[Any, ...] | None) -> tuple[LedgerRecord, ...]:
        rows = _read_rows(self._connection_factory, query, parameters)
        if isinstance(rows, _OperationFailure):
            _raise_trial_ledger_append_error()
        return _rows_mapped(rows, _record_from_row)


def _failure(checked_events: int, reason: str) -> LedgerIntegrityReport:
    return LedgerIntegrityReport(valid=False, checked_events=checked_events, reason=reason)


def _row_identity(row: tuple[Any, ...]) -> tuple[Any, ...]:
    """The columns the append function projects out of ``event_json``, in row order.

    Deliberately every projected column and no others. ``sequence`` and
    ``recorded_at`` are the database's own, ``canonical_event``/``event_json``/
    ``previous_hash``/``event_hash``/``payload_sha256``/``spec_sha256`` are
    checked separately, and what is left is the set whose only source is the
    client document -- so leaving any of them out is what makes a row able to
    look intact while describing a different event.
    """

    return (row[1], row[2], row[3], row[4], row[5], row[6], row[11], row[15], row[16])


def _event_identity(event: LedgerEvent) -> tuple[Any, ...]:
    return (
        event.event_id,
        event.scope_kind.value,
        event.scope_id,
        event.trial_id,
        event.attempt_id,
        event.event_type.value,
        event.occurred_at,
        event.legacy,
        event.legacy_reason,
    )


def _verify_rows(rows: tuple[tuple[Any, ...], ...]) -> LedgerIntegrityReport:
    expected_previous_hash = GENESIS_HASH
    checked_events = 0

    for index, row in enumerate(rows):
        try:
            if len(row) != _ROW_WIDTH:
                return _failure(checked_events, "row_invalid")
            sequence = row[0]
            if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
                return _failure(checked_events, "row_invalid")
            # The chain's first link is the only one whose previous hash the
            # verifier supplies rather than reads off the row before it, and the
            # database's CHECK pins that hash to 32 zero bytes for ``sequence = 1``
            # only. So a row that is *first* but numbered above 1 has had its
            # sequence renumbered, and a verifier that stops at the hash link
            # cannot see it: the link is satisfied by the same forged sequence the
            # hash was recomputed over, and every other check passes. Numbering
            # from 1 is what makes "this is the genesis row" and "this row says it
            # is genesis" the same statement. Skips are still allowed everywhere
            # else -- a burned sequence is a gap, not a break.
            if index == 0 and sequence != 1:
                return _failure(checked_events, "genesis_sequence_mismatch")

            previous_hash = bytes(row[13])
            if previous_hash != expected_previous_hash:
                return _failure(checked_events, "previous_hash_mismatch")

            canonical_event = row[8]
            if not isinstance(canonical_event, bytes):
                return _failure(checked_events, "row_invalid")
            event = _event_from_canonical(canonical_event)
            # The stored bytes must be the canonical encoding of what they
            # decode to, and must agree with the jsonb column. One check, two
            # ways for the row to be lying.
            if canonical_bytes(event) != canonical_event:
                return _failure(checked_events, "canonical_event_mismatch")
            if not isinstance(row[9], dict) or json.loads(canonical_event) != row[9]:
                return _failure(checked_events, "event_json_mismatch")
            # And every column the function projected out of that jsonb must
            # still say what the jsonb says. Without this a row can be entirely
            # self-consistent -- hash chain, digests and payload all correct --
            # while its ``event_id`` column names a different event, and the
            # ledger's own retry-by-id lookup would then miss it.
            if _row_identity(row) != _event_identity(event):
                return _failure(checked_events, "identity_column_mismatch")

            if bytes(row[7]) != bytes.fromhex(event.spec_sha256):
                return _failure(checked_events, "spec_digest_mismatch")
            if bytes(row[10]) != bytes.fromhex(canonical_sha256(event.payload)):
                return _failure(checked_events, "payload_digest_mismatch")
            if compute_event_hash(sequence, previous_hash, canonical_event) != bytes(row[14]):
                return _failure(checked_events, "event_hash_mismatch")
        except Exception:
            # One catch for every way a row fails to verify, deliberately: a row
            # carrying a non-hex digest makes ``bytes.fromhex`` raise, and that is
            # an invalid row rather than a broken verifier. The cost is that a bug
            # in this loop also reports ``event_schema_invalid``; split the catches
            # per check if a failure here ever has to be told apart.
            return _failure(checked_events, "event_schema_invalid")

        expected_previous_hash = bytes(row[14])
        checked_events += 1

    return LedgerIntegrityReport(valid=True, checked_events=checked_events, reason=None)
