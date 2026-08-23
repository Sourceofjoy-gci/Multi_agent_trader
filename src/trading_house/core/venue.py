"""Venue identity and the neutral outcome of asking a venue to act.

`RejectReason` is neutral. Raw broker return codes stay inside the venue
reference, where forensics can reach them and the risk engine cannot.
"""

from enum import Enum
from typing import Literal, Self

from pydantic import PositiveInt, model_validator

from trading_house.core.values import (
    CanonicalModel,
    NonEmptyStr,
    PositiveQuantity,
    Price,
)


class Venue(str, Enum):  # noqa: UP042
    MT5 = "mt5"


class RejectClass(str, Enum):  # noqa: UP042
    """How recovery must respond. See spec section 4.4."""

    TRANSIENT = "transient"
    CONTRACTUAL = "contractual"
    AUTHORITY = "authority"


class RejectReason(str, Enum):  # noqa: UP042
    REQUOTE = "requote"
    PRICE_CHANGED = "price_changed"
    TIMEOUT = "timeout"
    DISCONNECTED = "disconnected"
    INVALID_STOPS = "invalid_stops"
    INVALID_QUANTITY = "invalid_quantity"
    MARKET_CLOSED = "market_closed"
    UNSUPPORTED_FILL = "unsupported_fill"
    INSUFFICIENT_FUNDS = "insufficient_funds"
    TRADE_DISABLED = "trade_disabled"
    ACCOUNT_DISABLED = "account_disabled"


REJECT_CLASS: dict[RejectReason, RejectClass] = {
    RejectReason.REQUOTE: RejectClass.TRANSIENT,
    RejectReason.PRICE_CHANGED: RejectClass.TRANSIENT,
    RejectReason.TIMEOUT: RejectClass.TRANSIENT,
    RejectReason.DISCONNECTED: RejectClass.TRANSIENT,
    RejectReason.INVALID_STOPS: RejectClass.CONTRACTUAL,
    RejectReason.INVALID_QUANTITY: RejectClass.CONTRACTUAL,
    RejectReason.MARKET_CLOSED: RejectClass.CONTRACTUAL,
    RejectReason.UNSUPPORTED_FILL: RejectClass.CONTRACTUAL,
    RejectReason.INSUFFICIENT_FUNDS: RejectClass.AUTHORITY,
    RejectReason.TRADE_DISABLED: RejectClass.AUTHORITY,
    RejectReason.ACCOUNT_DISABLED: RejectClass.AUTHORITY,
}


class Mt5VenueRef(CanonicalModel):
    """Everything MT5-shaped about one submitted intent, in one place."""

    venue: Literal[Venue.MT5]
    magic: PositiveInt
    server_symbol: NonEmptyStr
    order_ticket: PositiveInt | None = None
    position_ticket: PositiveInt | None = None
    retcode: int | None = None


# One variant today. Pydantic needs a genuine union for a discriminator, so
# this stays a bare alias until a second venue exists, at which point it
# widens without any consumer changing:
#   VenueRef = Annotated[Mt5VenueRef | XVenueRef, Field(discriminator="venue")]
VenueRef = Mt5VenueRef


class ExecutionOutcome(CanonicalModel):
    """What the venue actually did. Never carries a raw broker message."""

    accepted: bool
    venue_ref: VenueRef | None
    filled_quantity: PositiveQuantity | None
    fill_price: Price | None
    reject_reason: RejectReason | None

    @model_validator(mode="after")
    def outcome_is_internally_consistent(self) -> Self:
        if self.accepted and self.reject_reason is not None:
            raise ValueError("an accepted outcome cannot carry a reject_reason")
        if not self.accepted and self.reject_reason is None:
            raise ValueError("a rejected outcome requires a reject_reason")
        if not self.accepted and (self.filled_quantity is not None or self.fill_price is not None):
            raise ValueError("a rejected outcome cannot carry a fill")
        if self.accepted and self.venue_ref is None:
            raise ValueError("an accepted outcome requires a venue_ref")
        if (self.filled_quantity is None) != (self.fill_price is None):
            raise ValueError("filled_quantity and fill_price must be present together")
        return self


class PrecheckResult(CanonicalModel):
    """The venue's opinion before the market is touched."""

    would_accept: bool
    reject_reason: RejectReason | None

    @model_validator(mode="after")
    def failure_is_explained(self) -> Self:
        if not self.would_accept and self.reject_reason is None:
            raise ValueError("a failed precheck requires a reject_reason")
        if self.would_accept and self.reject_reason is not None:
            raise ValueError("a passing precheck cannot carry a reject_reason")
        return self
