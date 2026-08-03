"""Immutable schemas for the append-only audit ledger."""

from collections.abc import Mapping
from datetime import datetime
from typing import Annotated, Any, NoReturn, Self, cast
from uuid import UUID

from pydantic import ConfigDict, Field, JsonValue, NonNegativeInt, PositiveInt, field_validator

from trading_house.core.clock import ensure_utc
from trading_house.core.errors import TimestampError
from trading_house.core.schemas import CanonicalModel, NonEmptyStr

SequenceNumber = Annotated[int, Field(ge=1, le=2**63 - 1)]


class FrozenDict(dict[str, JsonValue]):
    """A dictionary that preserves the immutable model boundary recursively."""

    @staticmethod
    def _immutable(*_: object, **__: object) -> NoReturn:
        raise TypeError("audit payload is immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    __ior__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable


class FrozenList(list[JsonValue]):
    """A list that preserves the immutable model boundary recursively."""

    @staticmethod
    def _immutable(*_: object, **__: object) -> NoReturn:
        raise TypeError("audit payload is immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    __iadd__ = _immutable
    __imul__ = _immutable
    append = _immutable
    clear = _immutable
    extend = _immutable
    insert = _immutable
    pop = _immutable
    remove = _immutable
    reverse = _immutable
    sort = _immutable


def _freeze_json(value: JsonValue) -> JsonValue:
    if isinstance(value, dict):
        return cast(JsonValue, FrozenDict({key: _freeze_json(item) for key, item in value.items()}))
    if isinstance(value, list):
        return cast(JsonValue, FrozenList([_freeze_json(item) for item in value]))
    return value


class AuditModel(CanonicalModel):
    """Strict audit model base that revalidates all copied boundary data."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    def model_copy(
        self, *, update: Mapping[str, Any] | None = None, deep: bool = False
    ) -> Self:
        del deep
        data = self.model_dump(mode="python", round_trip=True)
        if update is not None:
            data.update(update)
        return type(self).model_validate(data)


class AuditEvent(AuditModel):
    """The full, canonical envelope persisted by the audit ledger."""

    schema_version: PositiveInt
    event_id: UUID
    event_type: NonEmptyStr
    occurred_at: datetime
    actor: NonEmptyStr
    actor_type: NonEmptyStr
    correlation_id: UUID | None = None
    causation_id: UUID | None = None
    payload: JsonValue
    source_component: NonEmptyStr | None = None
    subject_id: NonEmptyStr | None = None

    @field_validator("occurred_at")
    @classmethod
    def normalize_occurred_at(cls, value: datetime) -> datetime:
        try:
            return ensure_utc(value)
        except TimestampError as error:
            raise ValueError(str(error)) from error

    @field_validator("payload")
    @classmethod
    def freeze_payload(cls, value: JsonValue) -> JsonValue:
        return _freeze_json(value)


class AuditRecord(AuditModel):
    """An immutable row returned by the audit append/read boundary."""

    sequence_number: SequenceNumber
    event_id: UUID
    canonical_event: bytes
    event_json: JsonValue
    previous_hash: bytes
    entry_hash: bytes
    received_at: datetime

    @field_validator("event_json")
    @classmethod
    def freeze_event_json(cls, value: JsonValue) -> JsonValue:
        return _freeze_json(value)

    @field_validator("previous_hash", "entry_hash")
    @classmethod
    def hash_is_sha256(cls, value: bytes) -> bytes:
        if len(value) != 32:
            raise ValueError("hash must be exactly 32 bytes")
        return value

    @field_validator("received_at")
    @classmethod
    def normalize_received_at(cls, value: datetime) -> datetime:
        try:
            return ensure_utc(value)
        except TimestampError as error:
            raise ValueError(str(error)) from error


class IntegrityReport(AuditModel):
    """The deterministic result of checking an ordered audit chain."""

    valid: bool
    checked_entries: NonNegativeInt
    first_invalid_sequence: SequenceNumber | None = None
    reason: NonEmptyStr | None = None

    def model_post_init(self, __context: Any) -> None:
        if self.valid:
            if self.first_invalid_sequence is not None or self.reason is not None:
                raise ValueError("valid reports cannot contain failure details")
            return
        if self.first_invalid_sequence is None or self.reason is None:
            raise ValueError("invalid reports require a failing sequence and reason")
