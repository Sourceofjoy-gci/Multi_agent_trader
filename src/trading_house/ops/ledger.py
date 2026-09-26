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
"""

from __future__ import annotations

from uuid import NAMESPACE_URL, uuid5

from pydantic import SecretStr

from trading_house.core.errors import ConfigurationError
from trading_house.research.evidence import EvidenceBundle, EvidenceStore
from trading_house.research.trial_ledger import (
    EvidenceSealedPayload,
    LedgerEvent,
    LedgerEventType,
    ResultRecordedPayload,
    ScopeKind,
)
from trading_house.settings import RuntimeSettings


def build_evidence_store(settings: RuntimeSettings) -> EvidenceStore:
    return EvidenceStore(settings.evidence_root)


def research_ledger_dsn(settings: RuntimeSettings) -> SecretStr:
    """The research DSN, or the one error a trial command may report for it."""

    if settings.research_ledger_dsn is None:
        raise ConfigurationError()
    return settings.research_ledger_dsn


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

    Keyed on the evidence digest rather than the attempt, because the digest is
    what a later reader will hold: this event's job is to say "the bytes
    ``<digest>`` are the evidence for this attempt", and two attempts sealing
    identical bytes are one statement, not two.
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
