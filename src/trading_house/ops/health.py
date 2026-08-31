"""Ordered, fail-closed readiness gate for the Phase 0 foundation.

The gate runs exactly one sequence and stops at the first failure:

    constitution.verify -> database.connect -> database.revision
    -> audit.verify -> venue.reconcile
    -> audit.append:startup -> audit.append:constitution_loaded

Typed configuration, signature, database, migration and audit errors propagate
unchanged. The gate never applies a migration and never reports a degraded
ready state.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import uuid4

from pydantic import JsonValue

from trading_house.audit.models import AuditEvent, AuditRecord, IntegrityReport
from trading_house.brokers.base import ReconciliationReport
from trading_house.constitution.loader import LoadedConstitution
from trading_house.core.clock import Clock, ensure_utc
from trading_house.core.errors import AuditIntegrityError
from trading_house.core.values import BookId

AUDIT_SCHEMA_VERSION = 1
STARTUP_EVENT = "startup"
CONSTITUTION_LOADED_EVENT = "constitution_loaded"
_ACTOR = "trading-house"
_ACTOR_TYPE = "service"
_SOURCE_COMPONENT = "ops.health"


class RuntimeConnection(Protocol):
    """The only capability the gate needs from a database connection."""

    def close(self) -> None: ...


class ConstitutionSource(Protocol):
    def __call__(self) -> LoadedConstitution: ...


class AuditLedger(Protocol):
    def verify(self) -> IntegrityReport: ...
    def append(self, event: AuditEvent) -> AuditRecord: ...


class BookReconciler(Protocol):
    def __call__(self) -> Mapping[BookId, ReconciliationReport]: ...


@dataclass(frozen=True, slots=True)
class HealthReport:
    """The immutable result of a completed readiness gate."""

    ready: bool
    application_version: str
    constitution_version: int
    constitution_sha256: str
    public_key_fingerprint: str
    audit_entries_verified: int
    books_reconciled: tuple[BookId, ...]
    open_positions: int
    checked_at: datetime


def _iso_z(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


class HealthService[ConnectionT: RuntimeConnection]:
    """Report ready only after every Phase 0 foundation check has passed.

    The connection type is preserved from ``open_connection`` through to
    ``assert_revision``, so callers keep their concrete driver type while the
    gate itself needs nothing but ``close()``.
    """

    def __init__(
        self,
        *,
        load_constitution: ConstitutionSource,
        open_connection: Callable[[], ConnectionT],
        assert_revision: Callable[[ConnectionT], None],
        ledger: AuditLedger,
        reconcile_books: BookReconciler,
        clock: Clock,
        application_version: str,
    ) -> None:
        self._load_constitution = load_constitution
        self._open_connection = open_connection
        self._assert_revision = assert_revision
        self._ledger = ledger
        self._reconcile_books = reconcile_books
        self._clock = clock
        self._application_version = application_version

    def run(self) -> HealthReport:
        """Run every gate in order and return the report, or raise a typed error."""

        loaded = self._load_constitution()
        connection = self._open_connection()
        try:
            self._assert_revision(connection)
            integrity = self._ledger.verify()
            if not integrity.valid:
                raise AuditIntegrityError()

            # Reconciliation reports; it does not fail readiness. Phase 1 owns
            # no positions of its own and has no intent ledger to compare
            # venue state against, so a mismatch -- including a manually
            # opened demo trade reconcile() cannot attribute to any book --
            # is not yet something this gate can judge as wrong. Mismatch
            # detection arrives in Phase 3, once an intent ledger exists.
            reports = self._reconcile_books()
            books_reconciled = tuple(sorted(reports.keys()))
            # A position with no declared magic range is reported as an
            # unmatched ref under every book, since it belongs to none of
            # them (see Mt5BrokerAdapter.reconcile). Summing per-book ref
            # counts would count that one orphan once per book; counting
            # distinct position_ticket values counts it once.
            open_positions = len(
                {
                    ref.position_ticket
                    for report in reports.values()
                    for ref in report.unmatched_venue_refs
                }
            )

            checked_at = ensure_utc(self._clock.now())
            payload = self._payload(loaded, checked_at)
            self._ledger.append(self._event(STARTUP_EVENT, checked_at, payload))
            self._ledger.append(self._event(CONSTITUTION_LOADED_EVENT, checked_at, payload))
        finally:
            connection.close()

        return HealthReport(
            ready=True,
            application_version=self._application_version,
            constitution_version=loaded.constitution.version,
            constitution_sha256=loaded.constitution_sha256,
            public_key_fingerprint=loaded.public_key_fingerprint,
            audit_entries_verified=integrity.checked_entries,
            books_reconciled=books_reconciled,
            open_positions=open_positions,
            checked_at=checked_at,
        )

    def _payload(self, loaded: LoadedConstitution, checked_at: datetime) -> dict[str, JsonValue]:
        return {
            "constitution_version": loaded.constitution.version,
            "constitution_sha256": loaded.constitution_sha256,
            "public_key_fingerprint": loaded.public_key_fingerprint,
            "application_version": self._application_version,
            "checked_at": _iso_z(checked_at),
        }

    def _event(
        self, event_type: str, occurred_at: datetime, payload: dict[str, JsonValue]
    ) -> AuditEvent:
        return build_audit_event(event_type, occurred_at, payload)


def build_audit_event(
    event_type: str,
    occurred_at: datetime,
    payload: dict[str, JsonValue],
    *,
    source_component: str = _SOURCE_COMPONENT,
) -> AuditEvent:
    """Build one audit-ledger envelope with this service's fixed actor identity.

    Shared with callers outside the gate itself -- the CLI's ``health``
    command reuses this to append gateway lifecycle events through the same
    audit interface, rather than inventing a second way to build one.
    """

    return AuditEvent(
        schema_version=AUDIT_SCHEMA_VERSION,
        event_id=uuid4(),
        event_type=event_type,
        occurred_at=ensure_utc(occurred_at),
        actor=_ACTOR,
        actor_type=_ACTOR_TYPE,
        payload=payload,
        source_component=source_component,
    )
