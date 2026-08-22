"""Ordered, fail-closed readiness gate driven by recording fakes."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from trading_house.audit.canonical import compute_entry_hash
from trading_house.audit.models import AuditEvent, AuditRecord, IntegrityReport
from trading_house.constitution.loader import LoadedConstitution
from trading_house.constitution.models import Constitution, parse_constitution_yaml
from trading_house.core.clock import FixedClock
from trading_house.core.errors import (
    AuditAppendError,
    AuditIntegrityError,
    ConfigurationError,
    DatabaseUnavailableError,
    MigrationMismatchError,
    SignatureVerificationError,
)
from trading_house.ops.health import HealthReport, HealthService

PROJECT_ROOT = Path(__file__).resolve().parents[3]
CONSTITUTION_SHA = "a" * 64
KEY_FINGERPRINT = "b" * 64
SECRET_DSN = "postgresql://runtime:super-secret-password@localhost/trading_house"  # noqa: S105

EXPECTED_ORDER = [
    "constitution.verify",
    "database.connect",
    "database.revision",
    "audit.verify",
    "audit.append:startup",
    "audit.append:constitution_loaded",
]


def _constitution() -> Constitution:
    return parse_constitution_yaml(
        (PROJECT_ROOT / "config" / "risk_constitution.yaml").read_bytes()
    )


def _loaded_constitution() -> LoadedConstitution:
    return LoadedConstitution(
        constitution=_constitution(),
        constitution_sha256=CONSTITUTION_SHA,
        public_key_fingerprint=KEY_FINGERPRINT,
    )


def _record_for(event: AuditEvent, sequence: int) -> AuditRecord:
    """Build a genuine record so fakes cannot drift from the real contract."""

    canonical = event.model_dump_json().encode("utf-8")
    return AuditRecord(
        sequence_number=sequence,
        event_id=event.event_id,
        canonical_event=canonical,
        event_json=event.model_dump(mode="json"),
        previous_hash=bytes(32),
        entry_hash=compute_entry_hash(sequence, bytes(32), canonical),
        received_at=datetime(2026, 8, 22, 9, 0, tzinfo=UTC),
    )


class FakeConnection:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


@dataclass
class Harness:
    """Records every stage and can fail any one of them on demand."""

    calls: list[str] = field(default_factory=list)
    fail_at: str | None = None
    error: Exception | None = None
    integrity: IntegrityReport = field(
        default_factory=lambda: IntegrityReport(valid=True, checked_entries=3)
    )
    appended: list[AuditEvent] = field(default_factory=list)
    connection: FakeConnection = field(default_factory=FakeConnection)

    def _stage(self, name: str) -> None:
        self.calls.append(name)
        if self.fail_at == name and self.error is not None:
            raise self.error

    def load_constitution(self) -> LoadedConstitution:
        self._stage("constitution.verify")
        return _loaded_constitution()

    def open_connection(self) -> FakeConnection:
        self._stage("database.connect")
        return self.connection

    def assert_revision(self, connection: Any) -> None:
        del connection
        self._stage("database.revision")

    def verify(self) -> IntegrityReport:
        self._stage("audit.verify")
        return self.integrity

    def append(self, event: AuditEvent) -> AuditRecord:
        self._stage(f"audit.append:{event.event_type}")
        self.appended.append(event)
        return _record_for(event, len(self.appended))


def _service(harness: Harness) -> HealthService:
    return HealthService(
        load_constitution=harness.load_constitution,
        open_connection=harness.open_connection,
        assert_revision=harness.assert_revision,
        ledger=harness,
        clock=FixedClock(datetime(2026, 8, 22, 9, 0, tzinfo=UTC)),
        application_version="0.1.0",
    )


def test_ready_report_runs_every_stage_in_order() -> None:
    harness = Harness()

    report = _service(harness).run()

    assert harness.calls == EXPECTED_ORDER
    assert report.ready is True
    assert report.constitution_version == 1
    assert report.constitution_sha256 == CONSTITUTION_SHA
    assert report.public_key_fingerprint == KEY_FINGERPRINT
    assert report.audit_entries_verified == 3
    assert report.checked_at == datetime(2026, 8, 22, 9, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("stage", "error"),
    [
        ("constitution.verify", SignatureVerificationError()),
        ("constitution.verify", ConfigurationError()),
        ("database.connect", DatabaseUnavailableError()),
        ("database.revision", MigrationMismatchError()),
        ("audit.verify", AuditAppendError()),
        ("audit.append:startup", AuditAppendError()),
        ("audit.append:constitution_loaded", AuditAppendError()),
    ],
)
def test_failing_stage_stops_the_gate_and_stays_typed(stage: str, error: Exception) -> None:
    harness = Harness(fail_at=stage, error=error)

    with pytest.raises(type(error)):
        _service(harness).run()

    assert harness.calls == EXPECTED_ORDER[: EXPECTED_ORDER.index(stage) + 1]


def test_invalid_audit_chain_raises_audit_integrity_error() -> None:
    harness = Harness(
        integrity=IntegrityReport(
            valid=False,
            checked_entries=2,
            first_invalid_sequence=3,
            reason="entry_hash_mismatch",
        )
    )

    with pytest.raises(AuditIntegrityError):
        _service(harness).run()

    assert harness.calls == EXPECTED_ORDER[: EXPECTED_ORDER.index("audit.verify") + 1]
    assert harness.appended == []


def test_connection_is_closed_even_when_a_later_stage_fails() -> None:
    harness = Harness(fail_at="audit.append:startup", error=AuditAppendError())

    with pytest.raises(AuditAppendError):
        _service(harness).run()

    assert harness.connection.closed is True


def test_connection_is_closed_on_success() -> None:
    harness = Harness()

    _service(harness).run()

    assert harness.connection.closed is True


def test_both_audit_events_carry_verification_metadata() -> None:
    harness = Harness()

    _service(harness).run()

    assert [event.event_type for event in harness.appended] == [
        "startup",
        "constitution_loaded",
    ]
    for event in harness.appended:
        payload = event.payload
        assert isinstance(payload, dict)
        assert payload["constitution_version"] == 1
        assert payload["constitution_sha256"] == CONSTITUTION_SHA
        assert payload["public_key_fingerprint"] == KEY_FINGERPRINT
        assert payload["application_version"] == "0.1.0"
        assert payload["checked_at"] == "2026-08-22T09:00:00Z"
        assert event.occurred_at == datetime(2026, 8, 22, 9, 0, tzinfo=UTC)


def test_report_never_exposes_secret_material() -> None:
    harness = Harness()

    report = _service(harness).run()

    rendered = f"{report!r} {report}"
    assert "super-secret-password" not in rendered
    assert SECRET_DSN not in rendered


def test_report_is_frozen() -> None:
    report = _service(Harness()).run()

    with pytest.raises((AttributeError, TypeError)):
        report.ready = False  # type: ignore[misc]


def test_health_service_never_applies_migrations() -> None:
    """The gate checks the revision; it must never advance it."""

    harness = Harness()
    upgrades: list[object] = []

    def assert_revision(connection: Any) -> None:
        del connection
        harness.calls.append("database.revision")

    service = HealthService(
        load_constitution=harness.load_constitution,
        open_connection=harness.open_connection,
        assert_revision=assert_revision,
        ledger=harness,
        clock=FixedClock(datetime(2026, 8, 22, 9, 0, tzinfo=UTC)),
        application_version="0.1.0",
    )
    service.run()

    assert upgrades == []
    assert harness.calls == EXPECTED_ORDER


def test_report_type_is_exported() -> None:
    assert HealthReport.__name__ == "HealthReport"


def test_service_accepts_any_callable_shaped_dependencies() -> None:
    """The gate depends on protocols, not concrete Phase 0 classes."""

    harness = Harness()
    loader: Callable[[], LoadedConstitution] = harness.load_constitution

    service = HealthService(
        load_constitution=loader,
        open_connection=harness.open_connection,
        assert_revision=harness.assert_revision,
        ledger=harness,
        clock=FixedClock(datetime(2026, 8, 22, 9, 0, tzinfo=UTC)),
        application_version="0.1.0",
    )

    assert service.run().ready is True
