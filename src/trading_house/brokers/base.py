"""The complete surface a venue must present. Eight methods, nothing more."""

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol, runtime_checkable

from pydantic import NonNegativeInt, field_validator

from trading_house.core.clock import ensure_utc
from trading_house.core.errors import TimestampError
from trading_house.core.instruments import InstrumentContract
from trading_house.core.schemas import OrderIntent, PositionState
from trading_house.core.values import (
    BookId,
    CanonicalModel,
    InstrumentId,
    PositiveQuantity,
    Price,
)
from trading_house.core.venue import ExecutionOutcome, PrecheckResult, VenueRef


class Quote(CanonicalModel):
    instrument_id: InstrumentId
    bid: Price
    ask: Price
    observed_at: datetime

    @field_validator("observed_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        try:
            return ensure_utc(value)
        except TimestampError as error:
            raise ValueError(str(error)) from error


class MarketSnapshot(CanonicalModel):
    quotes: tuple[Quote, ...]
    taken_at: datetime

    @field_validator("taken_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        try:
            return ensure_utc(value)
        except TimestampError as error:
            raise ValueError(str(error)) from error


class VenueHealth(CanonicalModel):
    connected: bool
    server_utc_offset_seconds: int
    last_quote_age_seconds: NonNegativeInt


class ReconciliationReport(CanonicalModel):
    book: BookId
    positions: tuple[PositionState, ...]
    unmatched_venue_refs: tuple[VenueRef, ...]
    reconciled_at: datetime

    @field_validator("reconciled_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        try:
            return ensure_utc(value)
        except TimestampError as error:
            raise ValueError(str(error)) from error


@runtime_checkable
class BrokerAdapter(Protocol):
    """Retcodes, fill negotiation, magic allocation and terminal lifecycle
    all live behind this surface, never in front of it."""

    def describe_instrument(self, instrument_id: InstrumentId) -> InstrumentContract: ...
    def snapshot(self, instrument_ids: Sequence[InstrumentId]) -> MarketSnapshot: ...
    def precheck(self, intent: OrderIntent) -> PrecheckResult: ...
    def submit(self, intent: OrderIntent) -> ExecutionOutcome: ...
    def amend_protection(
        self, ref: VenueRef, stop_loss: Price, take_profit: Price | None
    ) -> ExecutionOutcome:
        """Move a live position's protective stop, refusing to widen it.

        ``take_profit=None`` means *leave the position's existing
        take-profit alone* -- it is not a request to remove one. There is
        deliberately no way to remove a take-profit through this method,
        because nothing in this system ever needs to.
        """

    def close(self, ref: VenueRef, quantity: PositiveQuantity | None) -> ExecutionOutcome: ...
    def reconcile(self, book: BookId) -> ReconciliationReport: ...
    def health(self) -> VenueHealth: ...
