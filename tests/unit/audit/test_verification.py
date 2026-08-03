from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import pytest

from trading_house.audit.canonical import GENESIS_HASH, canonicalize_event, compute_entry_hash
from trading_house.audit.models import AuditEvent, AuditRecord
from trading_house.audit.verification import verify_records


def _event(index: int = 1) -> AuditEvent:
    return AuditEvent(
        schema_version=1,
        event_id=UUID(f"00000000-0000-0000-0000-{index:012d}"),
        event_type="verification.test",
        occurred_at=datetime(2026, 8, 3, 10, index, tzinfo=UTC),
        actor="verification-suite",
        actor_type="test",
        correlation_id=None,
        causation_id=None,
        payload={"accepted": True, "index": index},
        source_component="unit-tests",
        subject_id=f"entry-{index}",
    )


def _record(
    event: AuditEvent,
    *,
    sequence_number: int,
    previous_hash: bytes,
    canonical_event: bytes | None = None,
    event_json: object | None = None,
    received_at: datetime | None = None,
) -> AuditRecord:
    encoded = canonicalize_event(event) if canonical_event is None else canonical_event
    return AuditRecord(
        sequence_number=sequence_number,
        event_id=event.event_id,
        canonical_event=encoded,
        event_json=event.model_dump(mode="json") if event_json is None else event_json,
        previous_hash=previous_hash,
        entry_hash=compute_entry_hash(sequence_number, previous_hash, encoded),
        received_at=received_at or datetime(2026, 8, 3, 11, sequence_number, tzinfo=UTC),
    )


@pytest.fixture
def valid_records() -> list[AuditRecord]:
    records: list[AuditRecord] = []
    previous_hash = GENESIS_HASH
    for sequence_number in range(1, 4):
        record = _record(
            _event(sequence_number),
            sequence_number=sequence_number,
            previous_hash=previous_hash,
        )
        records.append(record)
        previous_hash = record.entry_hash
    return records


def _assert_failure(
    records: Iterable[AuditRecord],
    *,
    sequence_number: int,
    checked_entries: int,
    reason: str,
) -> None:
    report = verify_records(records)
    assert not report.valid
    assert report.first_invalid_sequence == sequence_number
    assert report.checked_entries == checked_entries
    assert report.reason == reason


def _unchecked_record(**updates: Any) -> AuditRecord:
    record = _record(_event(), sequence_number=1, previous_hash=GENESIS_HASH)
    return AuditRecord.model_construct(**(record.__dict__ | updates))


def test_empty_ledger_is_valid() -> None:
    report = verify_records(iter(()))
    assert report.valid
    assert report.checked_entries == 0
    assert report.first_invalid_sequence is None
    assert report.reason is None


def test_valid_chain_is_checked_in_supplied_order(valid_records: list[AuditRecord]) -> None:
    report = verify_records(iter(valid_records))
    assert report.valid
    assert report.checked_entries == 3


@pytest.mark.parametrize(
    ("sequence_number", "checked_entries"),
    [(2, 0), (4, 1)],
)
def test_sequence_must_be_continuous_from_one(
    valid_records: list[AuditRecord], sequence_number: int, checked_entries: int
) -> None:
    records = valid_records[:2] if checked_entries else valid_records[:1]
    failing_index = checked_entries
    records[failing_index] = AuditRecord.model_construct(
        **(records[failing_index].__dict__ | {"sequence_number": sequence_number})
    )
    _assert_failure(
        records,
        sequence_number=sequence_number,
        checked_entries=checked_entries,
        reason="sequence_gap",
    )


def test_duplicate_event_uuid_is_rejected(valid_records: list[AuditRecord]) -> None:
    valid_records[1] = AuditRecord.model_construct(
        **(valid_records[1].__dict__ | {"event_id": valid_records[0].event_id})
    )
    _assert_failure(
        valid_records,
        sequence_number=2,
        checked_entries=1,
        reason="duplicate_event_id",
    )


def test_first_record_requires_exact_genesis_link(valid_records: list[AuditRecord]) -> None:
    valid_records[0] = valid_records[0].model_copy(update={"previous_hash": b"g" * 32})
    _assert_failure(
        valid_records,
        sequence_number=1,
        checked_entries=0,
        reason="genesis_hash_mismatch",
    )


def test_later_record_requires_previous_entry_hash(valid_records: list[AuditRecord]) -> None:
    valid_records[1] = valid_records[1].model_copy(update={"previous_hash": b"p" * 32})
    _assert_failure(
        valid_records,
        sequence_number=2,
        checked_entries=1,
        reason="previous_hash_mismatch",
    )


def test_canonical_bytes_must_be_strict_utf8() -> None:
    record = _unchecked_record(canonical_event=b"\xff")
    _assert_failure(
        [record],
        sequence_number=1,
        checked_entries=0,
        reason="canonical_utf8_invalid",
    )


def test_canonical_bytes_must_be_json() -> None:
    record = _unchecked_record(canonical_event=b"not-json")
    _assert_failure(
        [record],
        sequence_number=1,
        checked_entries=0,
        reason="canonical_json_invalid",
    )


def test_parsed_bytes_must_equal_stored_json_without_bool_int_coercion() -> None:
    event = _event()
    event_json = event.model_dump(mode="json")
    event_json["payload"]["index"] = True  # type: ignore[index]
    record = _record(
        event,
        sequence_number=1,
        previous_hash=GENESIS_HASH,
        event_json=event_json,
    )
    _assert_failure(
        [record],
        sequence_number=1,
        checked_entries=0,
        reason="event_json_mismatch",
    )


def test_parsed_event_requires_strict_audit_event_validation() -> None:
    event_json = _event().model_dump(mode="json")
    event_json["schema_version"] = True
    encoded = json.dumps(event_json, separators=(",", ":"), sort_keys=True).encode()
    record = _unchecked_record(
        canonical_event=encoded,
        event_json=event_json,
        entry_hash=compute_entry_hash(1, GENESIS_HASH, encoded),
    )
    _assert_failure(
        [record],
        sequence_number=1,
        checked_entries=0,
        reason="event_schema_invalid",
    )


def test_row_event_uuid_must_match_validated_envelope_uuid() -> None:
    record = _record(_event(), sequence_number=1, previous_hash=GENESIS_HASH)
    record = AuditRecord.model_construct(
        **(record.__dict__ | {"event_id": UUID("ffffffff-ffff-ffff-ffff-ffffffffffff")})
    )
    _assert_failure(
        [record],
        sequence_number=1,
        checked_entries=0,
        reason="event_id_mismatch",
    )


def test_event_occurrence_timestamp_must_be_aware_utc() -> None:
    event_json = _event().model_dump(mode="json")
    event_json["occurred_at"] = "2026-08-03T10:01:00"
    encoded = json.dumps(event_json, separators=(",", ":"), sort_keys=True).encode()
    record = _unchecked_record(
        canonical_event=encoded,
        event_json=event_json,
        entry_hash=compute_entry_hash(1, GENESIS_HASH, encoded),
    )
    _assert_failure(
        [record],
        sequence_number=1,
        checked_entries=0,
        reason="occurred_at_not_utc",
    )


def test_event_occurrence_timestamp_rejects_non_utc_offset() -> None:
    event_json = _event().model_dump(mode="json")
    event_json["occurred_at"] = "2026-08-03T12:01:00+02:00"
    encoded = json.dumps(event_json, separators=(",", ":"), sort_keys=True).encode()
    record = _unchecked_record(
        canonical_event=encoded,
        event_json=event_json,
        entry_hash=compute_entry_hash(1, GENESIS_HASH, encoded),
    )
    _assert_failure(
        [record],
        sequence_number=1,
        checked_entries=0,
        reason="occurred_at_not_utc",
    )


def test_event_bytes_must_equal_recanonicalized_valid_event() -> None:
    event = _event()
    encoded = canonicalize_event(event).replace(b'"actor":', b'"actor" :', 1)
    record = _record(
        event,
        sequence_number=1,
        previous_hash=GENESIS_HASH,
        canonical_event=encoded,
    )
    _assert_failure(
        [record],
        sequence_number=1,
        checked_entries=0,
        reason="canonical_event_mismatch",
    )


def test_entry_hash_is_recomputed_from_row_content(valid_records: list[AuditRecord]) -> None:
    valid_records[1] = valid_records[1].model_copy(update={"entry_hash": b"x" * 32})
    _assert_failure(
        valid_records,
        sequence_number=2,
        checked_entries=1,
        reason="entry_hash_mismatch",
    )


def test_database_receipt_timestamp_must_be_aware_utc() -> None:
    record = _unchecked_record(received_at=datetime(2026, 8, 3, 11, 1))
    _assert_failure(
        [record],
        sequence_number=1,
        checked_entries=0,
        reason="received_at_not_utc",
    )


def test_non_utc_receipt_offset_is_rejected() -> None:
    record = _unchecked_record(
        received_at=datetime(2026, 8, 3, 13, 1, tzinfo=timezone(timedelta(hours=2)))
    )
    _assert_failure(
        [record],
        sequence_number=1,
        checked_entries=0,
        reason="received_at_not_utc",
    )


def test_corrupt_model_construct_content_returns_invalid_instead_of_raising() -> None:
    record = _unchecked_record(canonical_event=object())
    _assert_failure(
        [record],
        sequence_number=1,
        checked_entries=0,
        reason="record_invalid",
    )


def test_verifier_stops_at_deterministic_first_failure(
    valid_records: list[AuditRecord],
) -> None:
    first = valid_records[0].model_copy(update={"entry_hash": b"x" * 32})

    def records() -> Iterator[AuditRecord]:
        yield first
        raise AssertionError("verifier read beyond the first invalid row")

    _assert_failure(
        records(),
        sequence_number=1,
        checked_entries=0,
        reason="entry_hash_mismatch",
    )


def test_sequence_failure_precedes_all_later_row_checks(valid_records: list[AuditRecord]) -> None:
    corrupt = AuditRecord.model_construct(
        **(
            valid_records[1].__dict__
            | {
                "canonical_event": object(),
                "event_id": valid_records[0].event_id,
                "sequence_number": 2,
            }
        )
    )
    _assert_failure(
        [corrupt],
        sequence_number=2,
        checked_entries=0,
        reason="sequence_gap",
    )
