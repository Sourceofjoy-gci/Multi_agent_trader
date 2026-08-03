"""RFC 8785 canonical JSON and SHA-256 audit-entry hashing."""

import hashlib
import math
import struct
from typing import Never

import rfc8785

from trading_house.audit.models import AuditEvent
from trading_house.core.errors import SchemaValidationError

DOMAIN_SEPARATOR = b"trading-house:audit:v1"
GENESIS_HASH = bytes(32)
_MAX_SEQUENCE_NUMBER = 2**63 - 1


def _raise_schema_validation() -> Never:
    raise SchemaValidationError()


def _reject_nonfinite_numbers(value: object) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("canonical JSON cannot contain non-finite numbers")
    if isinstance(value, dict):
        for item in value.values():
            _reject_nonfinite_numbers(item)
    elif isinstance(value, list):
        for item in value:
            _reject_nonfinite_numbers(item)


def canonicalize_event(event: AuditEvent) -> bytes:
    """Encode a validated audit event as exact RFC 8785 canonical JSON bytes."""

    try:
        event_json = event.model_dump(mode="json")
        _reject_nonfinite_numbers(event_json)
        return rfc8785.dumps(event_json)
    except (OverflowError, TypeError, ValueError) as error:
        raise SchemaValidationError() from error


def compute_entry_hash(sequence_number: int, previous_hash: bytes, canonical_event: bytes) -> bytes:
    """Hash one protocol entry using PostgreSQL-compatible signed int64 bytes."""

    if (
        isinstance(sequence_number, bool)
        or not isinstance(sequence_number, int)
        or not 1 <= sequence_number <= _MAX_SEQUENCE_NUMBER
    ):
        _raise_schema_validation()
    if not isinstance(previous_hash, bytes) or len(previous_hash) != 32:
        _raise_schema_validation()
    if not isinstance(canonical_event, bytes):
        _raise_schema_validation()
    return hashlib.sha256(
        DOMAIN_SEPARATOR + struct.pack(">q", sequence_number) + previous_hash + canonical_event
    ).digest()
