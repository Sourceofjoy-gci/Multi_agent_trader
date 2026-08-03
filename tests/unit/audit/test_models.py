from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from trading_house.audit.models import AuditEvent, AuditRecord, IntegrityReport


@pytest.fixture
def valid_event_data() -> dict[str, object]:
    return {
        "schema_version": 1,
        "event_id": uuid4(),
        "event_type": "risk.decision",
        "occurred_at": datetime(2026, 8, 3, 12, 0, tzinfo=UTC),
        "actor": "risk-engine",
        "actor_type": "service",
        "correlation_id": uuid4(),
        "causation_id": uuid4(),
        "payload": {"decision": "APPROVED"},
        "source_component": "risk",
        "subject_id": "proposal-1",
    }


def test_event_rejects_naive_occurrence_time(valid_event_data: dict[str, object]) -> None:
    valid_event_data["occurred_at"] = datetime(2026, 8, 3, 12, 0)

    with pytest.raises(ValidationError, match="timezone-aware"):
        AuditEvent.model_validate(valid_event_data)


@pytest.mark.parametrize("field_name", ["event_type", "actor", "actor_type"])
def test_event_requires_nonempty_envelope_identifiers(
    valid_event_data: dict[str, object], field_name: str
) -> None:
    valid_event_data[field_name] = " "

    with pytest.raises(ValidationError):
        AuditEvent.model_validate(valid_event_data)


def test_event_rejects_non_uuid_correlation_identifier(valid_event_data: dict[str, object]) -> None:
    valid_event_data["correlation_id"] = "not-a-uuid"

    with pytest.raises(ValidationError, match="UUID"):
        AuditEvent.model_validate(valid_event_data)


def test_record_is_frozen_and_revalidates_model_copy(valid_event_data: dict[str, object]) -> None:
    event = AuditEvent.model_validate(valid_event_data)
    record = AuditRecord(
        sequence_number=1,
        event_id=event.event_id,
        canonical_event=b"{}",
        event_json=event.model_dump(mode="json"),
        previous_hash=bytes(32),
        entry_hash=b"x" * 32,
        received_at=datetime(2026, 8, 3, 12, 1, tzinfo=UTC),
    )

    with pytest.raises(ValidationError, match="frozen"):
        record.sequence_number = 2  # type: ignore[misc]
    with pytest.raises(ValidationError):
        record.model_copy(update={"entry_hash": b"short"})


def test_integrity_report_requires_consistent_valid_state() -> None:
    valid = IntegrityReport(valid=True, checked_entries=2)
    invalid = IntegrityReport(
        valid=False,
        checked_entries=1,
        first_invalid_sequence=2,
        reason="entry_hash_mismatch",
    )

    assert valid.first_invalid_sequence is None
    assert valid.reason is None
    assert invalid.reason == "entry_hash_mismatch"


@pytest.mark.parametrize(
    "report",
    [
        {"valid": True, "checked_entries": 1, "first_invalid_sequence": 1},
        {"valid": False, "checked_entries": 1},
        {
            "valid": False,
            "checked_entries": 1,
            "first_invalid_sequence": 2,
            "reason": "",
        },
        {
            "valid": False,
            "checked_entries": 1,
            "first_invalid_sequence": 1,
            "reason": "entry_hash_mismatch",
        },
    ],
)
def test_integrity_report_rejects_inconsistent_state(report: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        IntegrityReport.model_validate(report)
