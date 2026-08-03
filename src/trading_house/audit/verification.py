"""Independent, fail-closed verification for ordered audit records."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta
from typing import Never
from uuid import UUID

from pydantic import ValidationError

from trading_house.audit.canonical import GENESIS_HASH, canonicalize_event, compute_entry_hash
from trading_house.audit.models import AuditEvent, AuditRecord, IntegrityReport


def _failure(sequence_number: int, checked_entries: int, reason: str) -> IntegrityReport:
    return IntegrityReport(
        valid=False,
        checked_entries=checked_entries,
        first_invalid_sequence=sequence_number,
        reason=reason,
    )


def _reject_json_constant(_: str) -> Never:
    raise ValueError("non-standard JSON constant")


def _json_values_equal(left: object, right: object) -> bool:
    """Compare JSON trees without Python's bool/int equality coercion."""

    if isinstance(left, Mapping) and isinstance(right, Mapping):
        if left.keys() != right.keys():
            return False
        return all(_json_values_equal(left[key], right[key]) for key in left)
    if (
        isinstance(left, Sequence)
        and not isinstance(left, (str, bytes, bytearray))
        and isinstance(right, Sequence)
        and not isinstance(right, (str, bytes, bytearray))
    ):
        return len(left) == len(right) and all(
            _json_values_equal(left_item, right_item)
            for left_item, right_item in zip(left, right, strict=True)
        )
    return type(left) is type(right) and left == right


def _received_at_is_utc(value: object) -> bool:
    if not isinstance(value, datetime) or value.tzinfo is None:
        return False
    try:
        return value.utcoffset() == timedelta(0)
    except (OverflowError, ValueError):
        return False


def _occurred_at_is_utc(value: object) -> bool | None:
    if not isinstance(value, Mapping):
        return None
    occurred_at = value.get("occurred_at")
    if not isinstance(occurred_at, str):
        return None
    try:
        parsed = datetime.fromisoformat(occurred_at)
        if parsed.tzinfo is None:
            return False
        return parsed.utcoffset() == timedelta(0)
    except (OverflowError, ValueError):
        return None


def _validation_reason(error: ValidationError) -> str:
    if any(item["loc"] == ("occurred_at",) for item in error.errors(include_url=False)):
        return "occurred_at_not_utc"
    return "event_schema_invalid"


def verify_records(records: Iterable[AuditRecord]) -> IntegrityReport:
    """Verify records once, in supplied order, stopping at the first failure."""

    expected_sequence = 1
    expected_previous_hash = GENESIS_HASH
    seen_event_ids: set[UUID] = set()
    checked_entries = 0

    for record in records:
        try:
            sequence_number = record.sequence_number
            if type(sequence_number) is not int or not 1 <= sequence_number <= 2**63 - 1:
                return _failure(expected_sequence, checked_entries, "record_invalid")
            if sequence_number != expected_sequence:
                return _failure(sequence_number, checked_entries, "sequence_gap")

            event_id = record.event_id
            if not isinstance(event_id, UUID):
                return _failure(sequence_number, checked_entries, "record_invalid")
            if event_id in seen_event_ids:
                return _failure(sequence_number, checked_entries, "duplicate_event_id")

            previous_hash = record.previous_hash
            if sequence_number == 1:
                if type(previous_hash) is not bytes or previous_hash != GENESIS_HASH:
                    return _failure(sequence_number, checked_entries, "genesis_hash_mismatch")
            elif type(previous_hash) is not bytes or previous_hash != expected_previous_hash:
                return _failure(sequence_number, checked_entries, "previous_hash_mismatch")

            canonical_event = record.canonical_event
            if type(canonical_event) is not bytes:
                return _failure(sequence_number, checked_entries, "record_invalid")
            try:
                decoded_event = canonical_event.decode("utf-8", errors="strict")
            except UnicodeDecodeError:
                return _failure(sequence_number, checked_entries, "canonical_utf8_invalid")
            try:
                parsed_event = json.loads(decoded_event, parse_constant=_reject_json_constant)
            except (json.JSONDecodeError, ValueError):
                return _failure(sequence_number, checked_entries, "canonical_json_invalid")

            if not _json_values_equal(parsed_event, record.event_json):
                return _failure(sequence_number, checked_entries, "event_json_mismatch")
            if _occurred_at_is_utc(parsed_event) is False:
                return _failure(sequence_number, checked_entries, "occurred_at_not_utc")

            try:
                event = AuditEvent.model_validate_json(canonical_event)
            except ValidationError as error:
                return _failure(sequence_number, checked_entries, _validation_reason(error))

            if event.event_id != event_id:
                return _failure(sequence_number, checked_entries, "event_id_mismatch")
            if canonicalize_event(event) != canonical_event:
                return _failure(sequence_number, checked_entries, "canonical_event_mismatch")
            if (
                compute_entry_hash(sequence_number, previous_hash, canonical_event)
                != record.entry_hash
            ):
                return _failure(sequence_number, checked_entries, "entry_hash_mismatch")
            if not _received_at_is_utc(record.received_at):
                return _failure(sequence_number, checked_entries, "received_at_not_utc")
        except Exception:
            return _failure(expected_sequence, checked_entries, "record_invalid")

        seen_event_ids.add(event_id)
        expected_previous_hash = record.entry_hash
        expected_sequence += 1
        checked_entries += 1

    return IntegrityReport(valid=True, checked_entries=checked_entries)
