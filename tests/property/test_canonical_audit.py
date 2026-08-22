"""Canonicalization and hash-chain properties for the audit ledger."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import JsonValue

from trading_house.audit.canonical import (
    GENESIS_HASH,
    canonicalize_event,
    compute_entry_hash,
)
from trading_house.audit.models import AuditEvent, AuditRecord
from trading_house.audit.verification import verify_records
from trading_house.core.errors import SchemaValidationError

JSON_SCALARS = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(10**9), max_value=10**9),
    st.text(max_size=24),
)
JSON_VALUES = st.recursive(
    JSON_SCALARS,
    lambda children: st.one_of(
        st.lists(children, max_size=4),
        st.dictionaries(st.text(min_size=1, max_size=8), children, max_size=4),
    ),
    max_leaves=12,
)
PAYLOADS = st.dictionaries(st.text(min_size=1, max_size=8), JSON_VALUES, max_size=6)


def _event(index: int, payload: JsonValue) -> AuditEvent:
    return AuditEvent(
        schema_version=1,
        event_id=uuid5(NAMESPACE_URL, f"property-event-{index}"),
        event_type="property.event",
        occurred_at=datetime(2026, 8, 22, 9, index % 60, tzinfo=UTC),
        actor="property-suite",
        actor_type="test",
        payload=payload,
        source_component="property",
    )


def _chain(events: list[AuditEvent]) -> list[AuditRecord]:
    records: list[AuditRecord] = []
    previous = GENESIS_HASH
    for sequence, event in enumerate(events, start=1):
        canonical = canonicalize_event(event)
        entry_hash = compute_entry_hash(sequence, previous, canonical)
        records.append(
            AuditRecord(
                sequence_number=sequence,
                event_id=event.event_id,
                canonical_event=canonical,
                event_json=event.model_dump(mode="json"),
                previous_hash=previous,
                entry_hash=entry_hash,
                received_at=datetime(2026, 8, 22, 10, 0, tzinfo=UTC),
            )
        )
        previous = entry_hash
    return records


@given(PAYLOADS)
def test_canonicalization_is_deterministic(payload: JsonValue) -> None:
    event = _event(0, payload)

    assert canonicalize_event(event) == canonicalize_event(event)


@given(PAYLOADS)
def test_canonical_bytes_are_valid_utf8_json(payload: JsonValue) -> None:
    canonical = canonicalize_event(_event(0, payload))

    canonical.decode("utf-8")
    assert canonical.startswith(b"{")


@given(PAYLOADS)
def test_payload_key_order_never_changes_canonical_bytes(payload: dict[str, JsonValue]) -> None:
    reordered = dict(reversed(list(payload.items())))

    assert canonicalize_event(_event(0, payload)) == canonicalize_event(_event(0, reordered))


@given(st.integers(min_value=1, max_value=2**63 - 1), st.binary(min_size=32, max_size=32))
def test_entry_hash_is_32_bytes_and_stable(sequence: int, previous: bytes) -> None:
    first = compute_entry_hash(sequence, previous, b"canonical")
    second = compute_entry_hash(sequence, previous, b"canonical")

    assert first == second
    assert len(first) == 32


@given(
    st.integers(min_value=1, max_value=2**62),
    st.binary(min_size=32, max_size=32),
    st.binary(max_size=64),
)
def test_changing_the_sequence_changes_the_hash(
    sequence: int, previous: bytes, canonical: bytes
) -> None:
    assert compute_entry_hash(sequence, previous, canonical) != compute_entry_hash(
        sequence + 1, previous, canonical
    )


@given(st.binary(min_size=32, max_size=32), st.binary(max_size=64))
def test_changing_the_previous_hash_changes_the_hash(previous: bytes, canonical: bytes) -> None:
    other = bytes(byte ^ 0xFF for byte in previous)

    assert compute_entry_hash(1, previous, canonical) != compute_entry_hash(1, other, canonical)


@given(st.integers(min_value=-(10**6), max_value=0))
def test_non_positive_sequences_are_always_rejected(sequence: int) -> None:
    with pytest.raises(SchemaValidationError):
        compute_entry_hash(sequence, GENESIS_HASH, b"canonical")


@given(st.binary(max_size=31))
def test_short_previous_hashes_are_always_rejected(previous: bytes) -> None:
    with pytest.raises(SchemaValidationError):
        compute_entry_hash(1, previous, b"canonical")


@given(st.lists(PAYLOADS, min_size=1, max_size=6))
def test_any_honestly_built_chain_verifies(payloads: list[JsonValue]) -> None:
    records = _chain([_event(index, payload) for index, payload in enumerate(payloads)])

    report = verify_records(records)

    assert report.valid is True
    assert report.checked_entries == len(records)


@given(st.lists(PAYLOADS, min_size=2, max_size=6))
def test_dropping_any_record_breaks_the_chain(payloads: list[JsonValue]) -> None:
    records = _chain([_event(index, payload) for index, payload in enumerate(payloads)])

    report = verify_records(records[1:])

    assert report.valid is False


@given(st.lists(PAYLOADS, min_size=1, max_size=5))
def test_reordering_a_chain_is_detected(payloads: list[JsonValue]) -> None:
    records = _chain([_event(index, payload) for index, payload in enumerate(payloads)])
    if len(records) < 2:
        return

    report = verify_records(list(reversed(records)))

    assert report.valid is False


@given(st.sampled_from([float("nan"), float("inf"), float("-inf")]))
def test_non_finite_numbers_never_canonicalize(value: float) -> None:
    with pytest.raises(SchemaValidationError):
        canonicalize_event(_event(0, {"value": value}))
