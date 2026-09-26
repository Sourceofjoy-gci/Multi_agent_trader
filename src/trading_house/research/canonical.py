"""Canonical JSON bytes for research evidence.

Two canonical forms exist in this codebase and they are NOT interchangeable.

``trading_house.audit.canonical`` encodes RFC 8785 (JCS): sorted keys, compact
separators, and ECMAScript number formatting, with a ``trading-house:audit:v1``
domain separator folded into the hash. It is the audit chain's format and it
exists to satisfy an external canonicalization standard.

This module is a different format on purpose. It sorts keys and compacts
separators the same way, but it inherits the Phase 8 spec's own contract for
the two types RFC 8785 does not model correctly for this project: ``Decimal``
serialized as its exact string form, and timestamps serialized as UTC with a
``Z`` suffix. RFC 8785's IEEE-754 number rules would round a ``Decimal`` rate
and re-render it as a binary double, so a digest taken through JCS would not
match the money the backtest actually charged. Research evidence is therefore
hashed here and never through ``audit.canonical``.

The two chains are separately domain-separated, so a research digest can never
be mistaken for an audit digest. Keep them that way: hashing research evidence
with ``audit.canonical``, or an audit entry with these functions, silently
produces bytes that verify against the wrong chain.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel

from trading_house.core.errors import SchemaValidationError


def canonical_bytes(model: BaseModel) -> bytes:
    try:
        payload: Any = model.model_dump(mode="json")
        return json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as error:
        raise SchemaValidationError() from error


def canonical_sha256(model: BaseModel) -> str:
    return hashlib.sha256(canonical_bytes(model)).hexdigest()
