"""The complete surface a venue must present. Eight methods, nothing more."""

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol, runtime_checkable

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


class MarketSnapshot(CanonicalModel):
    quotes: tuple[Quote, ...]
    taken_at: datetime


class VenueHealth(CanonicalModel):
    connected: bool
    server_utc_offset_seconds: int
    last_quote_age_seconds: int


class ReconciliationReport(CanonicalModel):
    book: BookId
    positions: tuple[PositionState, ...]
    unmatched_venue_refs: tuple[VenueRef, ...]
    reconciled_at: datetime


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
    ) -> ExecutionOutcome: ...
    def close(self, ref: VenueRef, quantity: PositiveQuantity | None) -> ExecutionOutcome: ...
    def reconcile(self, book: BookId) -> ReconciliationReport: ...
    def health(self) -> VenueHealth: ...
