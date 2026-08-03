import hashlib
import struct
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from trading_house.audit.canonical import (
    DOMAIN_SEPARATOR,
    GENESIS_HASH,
    canonicalize_event,
    compute_entry_hash,
)
from trading_house.audit.models import AuditEvent
from trading_house.core.errors import SchemaValidationError


@pytest.fixture
def event_factory():
    def create(
        *,
        event_id: UUID | None = None,
        payload: object | None = None,
    ) -> AuditEvent:
        return AuditEvent(
            schema_version=1,
            event_id=event_id or uuid4(),
            event_type="constitution.loaded",
            occurred_at=datetime(2026, 8, 3, 12, 0, tzinfo=UTC),
            actor="startup-service",
            actor_type="service",
            correlation_id=None,
            causation_id=None,
            payload={} if payload is None else payload,
            source_component="constitution-loader",
            subject_id="risk-constitution",
        )

    return create


def test_canonicalization_is_independent_of_payload_key_order(event_factory) -> None:
    left = event_factory(payload={"b": 2, "a": 1})
    right = event_factory(payload={"a": 1, "b": 2}, event_id=left.event_id)

    assert canonicalize_event(left) == canonicalize_event(right)


def test_hash_preimage_matches_protocol(event_factory) -> None:
    event_bytes = canonicalize_event(event_factory())
    expected = hashlib.sha256(
        DOMAIN_SEPARATOR + struct.pack(">q", 1) + GENESIS_HASH + event_bytes
    ).digest()

    assert compute_entry_hash(1, GENESIS_HASH, event_bytes) == expected


@pytest.mark.parametrize("sequence_number", [0, -1, 2**63])
def test_hash_rejects_out_of_range_sequence_numbers(sequence_number: int) -> None:
    with pytest.raises(SchemaValidationError):
        compute_entry_hash(sequence_number, GENESIS_HASH, b"{}")


@pytest.mark.parametrize("previous_hash", [b"", b"x" * 31, b"x" * 33])
def test_hash_rejects_non_sha256_previous_hashes(previous_hash: bytes) -> None:
    with pytest.raises(SchemaValidationError):
        compute_entry_hash(1, previous_hash, b"{}")


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_canonicalization_rejects_nonfinite_payload_numbers(event_factory, value: float) -> None:
    event = event_factory(payload={"value": value})

    with pytest.raises(SchemaValidationError) as error:
        canonicalize_event(event)

    assert str(error.value) == "schema validation failed"
    assert error.value.__cause__ is not None


def test_canonicalization_uses_an_immutable_payload_snapshot(event_factory) -> None:
    source_payload = {"nested": {"values": [1, 2]}}
    event = event_factory(payload=source_payload)
    before = canonicalize_event(event)
    source_payload["nested"]["values"].append(3)

    assert canonicalize_event(event) == before
    with pytest.raises(TypeError):
        event.payload["nested"] = {}  # type: ignore[index]
