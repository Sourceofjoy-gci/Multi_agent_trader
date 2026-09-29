"""The trial ledger's composition shim.

``cli.py`` is the composition root for every other command, and this is the one
place it is not, for the same reason ``ops/backtest.py`` and ``ops/guard.py``
exist: the trial commands are the sixth thing ``cli.py`` wires, and the wiring
that has to agree with itself (ledger DSN, evidence root, the events an evidence
bundle produces) is worth having in one small module instead of six call sites.

Two things are deliberately *not* here. The migration-head check belongs to the
CLI, because "the database is at the wrong revision" is a command-level answer
with its own exit code, not something a constructor decides to raise. And
``PostgresTrialLedger`` is constructed by the CLI after that check passes, so
the store itself stays ignorant of where its DSN came from.

The DSN is optional in ``RuntimeSettings`` on purpose. Requiring a second
database at the application boundary would stop ``order submit`` and
``guard run`` from starting on a host that has never run a trial, so the
absence of one becomes a ``ConfigurationError`` here, at the single boundary
that cannot work without it.

The same boundary is also the only place that can compare the two DSNs, and
the comparison is load-bearing rather than tidy: the split between the two
databases is the whole operational claim of this phase, and a DSN that names
the application database would satisfy every other check -- the ledger tables
exist there, the head is a chain, ``verify`` passes -- while quietly making
that claim false. A refusal is cheap and a silent collapse is not.
"""

from __future__ import annotations

from datetime import datetime
from uuid import NAMESPACE_URL, uuid5

import psycopg
from psycopg.conninfo import conninfo_to_dict
from pydantic import SecretStr

from trading_house.core.errors import ConfigurationError
from trading_house.research.evidence import EvidenceBundle, EvidenceStore
from trading_house.research.trial_ledger import (
    EvidenceSealedPayload,
    ExecutionStartedPayload,
    LedgerEvent,
    LedgerEventType,
    ResultRecordedPayload,
    ScopeKind,
    TrialLedger,
)
from trading_house.settings import RuntimeSettings


def build_evidence_store(settings: RuntimeSettings) -> EvidenceStore:
    return EvidenceStore(settings.evidence_root)


def _dbname(dsn: SecretStr) -> str | None:
    """The database a DSN names, or ``None`` when it cannot be read as one.

    Parsed rather than pattern-matched so every spelling the driver accepts --
    URI or keyword/value, percent-encoded password, the database named in the
    path or in ``dbname`` -- is compared the same way. Only the name is extracted
    and only the name is ever returned, because a refusal must be able to say two
    DSNs collide without carrying either credential into a log line.

    ``None`` covers two cases, and both are answers rather than gaps. A conninfo
    the driver itself rejects has no database to collide with, and a conninfo
    that omits ``dbname`` will connect to the role's own default -- which is
    equally unnamed here. Neither can be shown to be the *same* database, so
    neither is refused on that basis; the driver answers for a malformed one, and
    a DSN without a ``dbname`` is a deployment whose ledger database is a
    guess.
    """

    try:
        dbname = conninfo_to_dict(dsn.get_secret_value()).get("dbname")
    except psycopg.Error:
        return None
    return dbname if isinstance(dbname, str) else None


def research_ledger_dsn(settings: RuntimeSettings) -> SecretStr:
    """The research DSN, or the one error a trial command may report for it.

    Two refusals, both here because this is the only boundary that needs a
    second database at all: the DSN is absent, or it names the database the
    application already uses. The first is a host that has never run a trial; the
    second is a misconfiguration that would otherwise be reported as success.
    """

    research = settings.research_ledger_dsn
    if research is None:
        raise ConfigurationError()
    research_db = _dbname(research)
    if research_db is not None and research_db == _dbname(settings.database_dsn):
        raise ConfigurationError()
    return research


def execution_started_event(
    trial_id: str, attempt_id: str, spec_sha256: str, started_at: datetime
) -> LedgerEvent:
    """The event an operator's own start earns, before any bundle exists.

    The id is derived from the trial and the attempt alone -- the two things that
    say *which* execution this is -- so it names one attempt rather than one run
    of it. A different specification under the same attempt id is a different
    event in every other way and is refused as the conflict it is.

    ``occurred_at`` is a time the operator declared rather than one recovered from
    a document, and that is not an inconsistency with the two builders below: a
    start has no bundle, so there is nothing to recover it from. Claiming
    otherwise -- borrowing a protocol's data-window end, or a result's
    ``occurred_at`` -- would put a time on the row that no artefact supports, and
    this event's entire claim is that the execution began when the operator said
    it began. It is declared provenance rather than proof of order, for the same
    reason a bundle's ``registered_at`` is: the database event's own
    ``recorded_at`` is the only registration-order authority here, and nothing in
    the chain can observe when a backtest actually began.

    Taking that value as an argument rather than reading the clock here is what
    makes the command retryable. A retry reuses the declared value and so derives
    identical bytes, which the chain recognises as the same event; a retry under a
    different value is a different body under an id the chain already holds, and
    is refused as the conflict it is.
    """

    return LedgerEvent(
        event_id=uuid5(
            NAMESPACE_URL,
            f"trading-house:trial-event:start:{trial_id}:{attempt_id}",
        ),
        scope_kind=ScopeKind.ATTEMPT,
        scope_id=attempt_id,
        event_type=LedgerEventType.EXECUTION_STARTED,
        trial_id=trial_id,
        attempt_id=attempt_id,
        # Carried, not checked. Nothing in this phase can tell whether the digest
        # names the candidate the preregistration declared, so the chain preserves
        # what the operator supplied and ``count`` counts distinct supplied
        # digests. See the README's "What Phase 8A does not implement".
        spec_sha256=spec_sha256,
        occurred_at=started_at,
        payload=ExecutionStartedPayload(
            event_type=LedgerEventType.EXECUTION_STARTED,
            attempt_id=attempt_id,
            execution_started_at=started_at,
        ),
    )


def result_recorded_event(bundle: EvidenceBundle) -> LedgerEvent:
    """The event a result earns, once its bundle has been accepted.

    The id is derived from the trial, the attempt, and the result digest, so a
    re-run of the same attempt records the same event rather than a second one,
    and a *different* result for the same attempt is refused as the conflict it
    is. ``occurred_at`` is the bundle's own declared time, never a clock read
    here: an event whose bytes change between two attempts at recording it is an
    event that cannot be retried.
    """

    return LedgerEvent(
        event_id=uuid5(
            NAMESPACE_URL,
            f"trading-house:trial-result:{bundle.trial_id}:{bundle.attempt_id}"
            f":{bundle.source_result_sha256}",
        ),
        scope_kind=ScopeKind.ATTEMPT,
        scope_id=bundle.attempt_id,
        event_type=LedgerEventType.RESULT_RECORDED,
        trial_id=bundle.trial_id,
        attempt_id=bundle.attempt_id,
        spec_sha256=bundle.spec_sha256,
        occurred_at=bundle.provenance.occurred_at,
        payload=ResultRecordedPayload(
            event_type=LedgerEventType.RESULT_RECORDED,
            attempt_id=bundle.attempt_id,
            source_result_sha256=bundle.source_result_sha256,
        ),
    )


def evidence_sealed_event(bundle: EvidenceBundle, evidence_sha256: str) -> LedgerEvent:
    """The event an evidence file earns, addressed by the digest that names it.

    Keyed on the evidence digest rather than on the attempt, because the digest
    is what a later reader holds: this event's job is to say "the bytes
    ``<digest>`` are the evidence for this attempt". The retry it has to survive
    is the *same* attempt recorded twice -- the evidence store is
    content-addressed, so one bundle seals to one digest, and an id derived from
    that digest is a row the chain recognises instead of a second row.

    Two *different* attempts of one trial do not collide here, but nothing
    excludes them: a bundle carries its own ``attempt_id``, so their bytes differ
    and so do their digests. The key is what the evidence store already keys on,
    not a claim that only one attempt can ever exist.
    """

    return LedgerEvent(
        event_id=uuid5(NAMESPACE_URL, f"trading-house:trial-evidence-sealed:{evidence_sha256}"),
        scope_kind=ScopeKind.ATTEMPT,
        scope_id=bundle.attempt_id,
        event_type=LedgerEventType.EVIDENCE_SEALED,
        trial_id=bundle.trial_id,
        attempt_id=bundle.attempt_id,
        spec_sha256=bundle.spec_sha256,
        # From the bundle's own provenance, never a clock read here: a clock
        # read would make the same evidence seal to two different events on a
        # re-run, and the retry would be refused for looking like a conflict.
        occurred_at=bundle.provenance.registered_at,
        payload=EvidenceSealedPayload(
            event_type=LedgerEventType.EVIDENCE_SEALED,
            attempt_id=bundle.attempt_id,
            evidence_sha256=evidence_sha256,
        ),
    )


def seal_bundle(
    bundle: EvidenceBundle,
    *,
    ledger: TrialLedger,
    store: EvidenceStore,
) -> str:
    """Write one bundle and append the two events that reference it.

    Returns the evidence digest, which is the address the bundle now has.

    ``research trial record`` and ``research trial scenarios`` both call this.
    An orchestrator that sealed by a second route would be able to produce a
    bundle the chain does not point at, or an event whose digest is not the one
    on disk -- and neither would be noticed, because both sides would look
    complete on their own.

    The order is write-then-append, so a failed append leaves a document nothing
    references rather than a row whose document is missing. The unreferenced side
    of that pair is the recoverable one.
    """

    stored = store.write(bundle)
    ledger.append(result_recorded_event(bundle))
    ledger.append(evidence_sealed_event(bundle, stored.sha256))
    return stored.sha256
