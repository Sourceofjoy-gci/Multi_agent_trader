"""Canonical JSON bytes and domain-separated digests for research evidence.

Research evidence and the audit log are two separate chains, and this module
belongs to the first one. ``trading_house.audit.canonical`` owns the audit
chain and its ``trading-house:audit:v1`` separator; ``DOMAIN_SEPARATOR`` below
is this chain's, and it is the reason a digest from either side can never be
mistaken for a digest from the other.

The separator is hashed in front of the canonical bytes, so a research digest
is ``sha256(b"trading-house:research:v1" + canonical_bytes(model))``. Strip the
prefix and you are left with a bare SHA-256 over the same bytes -- which is a
shape the audit chain also produces. That is the whole point: the two chains
encode different things, so the prefix, not the encoding, is what keeps their
digests apart. Hashing research evidence through ``audit.canonical``, or an
audit entry through these functions, yields bytes that verify against the
wrong chain and nothing will notice at the call site.

The encoding itself is sorted keys, compact separators, UTF-8, and no NaN or
infinity -- stable across processes and Python versions, so a digest taken
today still verifies when the evidence is re-read in a year. It is deliberately
not RFC 8785: this repo's own ``Decimal`` and UTC-timestamp contract governs
research evidence, and the audit chain's format is not this chain's business.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel

from trading_house.core.errors import SchemaValidationError

DOMAIN_SEPARATOR = b"trading-house:research:v1"


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
    return hashlib.sha256(DOMAIN_SEPARATOR + canonical_bytes(model)).hexdigest()
