"""Canonical JSON bytes for research evidence."""

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
