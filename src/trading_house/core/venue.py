"""Venue identity and the neutral outcome of asking a venue to act.

`RejectReason` is neutral. Raw broker return codes stay inside the venue
reference, where forensics can reach them and the risk engine cannot.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Literal, Self

from pydantic import NonNegativeInt, PositiveInt, model_validator

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
    UNKNOWN = "unknown"


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
    RejectReason.UNKNOWN: RejectClass.AUTHORITY,
}


class Mt5VenueRef(CanonicalModel):
    """Everything MT5-shaped about one submitted intent, in one place."""

    venue: Literal[Venue.MT5]
    # 0 is MT5's "no magic set" -- a manually opened or foreign position.
    # ``derive_magic`` only ever produces in-range positive values for our own
    # refs, so 0 can only ever mean "not ours", never "ours, unassigned".
    magic: NonNegativeInt
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


class DealEntry(str, Enum):  # noqa: UP042
    """Which side of a position a deal touched.

    ``OUT_BY`` (a close against an opposing position) maps to ``OUT`` at the
    boundary -- it is still a close, and the guard only needs to tell opens
    from closes, not how a close was executed.
    """

    IN = "in"  # opened or added to a position
    OUT = "out"  # closed or reduced a position
    INOUT = "inout"  # reversal: closed one side and opened the other


@dataclass(frozen=True, slots=True)
class DealRecord:
    """A neutral, execution-owned shape for one broker deal.

    Deliberately not ``Mt5Deal`` -- ``execution/`` must not import
    ``brokers/``, so the MT5 adapter maps its own deal rows into this shape
    at the boundary rather than the reconciler reaching across layers for
    a broker-specific type.
    """

    magic: int
    server_symbol: str
    volume: Decimal
    position_ticket: int
    dealt_at: datetime
    entry: DealEntry


@dataclass(frozen=True, slots=True)
class PositionRecord:
    """A neutral, execution-owned shape for one open broker position.

    Deal history and open positions answer different questions, and only the
    second one survives a broker that lost or never wrote the deal: a position
    that exists is proof the order happened. Same reason as ``DealRecord`` for
    it living here rather than in ``brokers/``.
    """

    magic: int
    server_symbol: str
    volume: Decimal
    position_ticket: int
    stop_loss: Decimal | None  # None when the broker reports 0 -- NO stop
    open_price: Decimal
    is_buy: bool
    opened_at: datetime


@dataclass(frozen=True, slots=True)
class DealMoney:
    """What one broker deal did to the balance, for the portfolio's P&L.

    Separate from ``DealRecord`` because the reconciler matches deals and never
    needs their money, and the portfolio needs their money and never matches
    them. ``net_money`` is profit plus commission, swap and fee.
    """

    magic: int
    dealt_at: datetime
    net_money: Decimal


@dataclass(frozen=True, slots=True)
class PositionMark:
    """One open position as the risk engine's portfolio sees it: where the
    broker marks it now, and what that is worth. Separate from
    ``PositionRecord`` for the reason ``DealMoney`` is separate from
    ``DealRecord``. ``stop_loss`` is ``None`` when the broker reports no stop,
    exactly as on ``PositionRecord``."""

    magic: int
    server_symbol: str
    volume: Decimal
    is_buy: bool
    stop_loss: Decimal | None
    current_price: Decimal
    unrealized_money: Decimal


class RecoveryAction(str, Enum):  # noqa: UP042
    """The only three responses to a venue rejection.

    There is deliberately no widen-the-stop action: the constitution forbids
    stop widening, and recovery may not breach a prohibition (I-15).
    """

    RETRY_WITH_FRESH_PRICE = "retry_with_fresh_price"
    REFRESH_CONTRACT_AND_RESIZE = "refresh_contract_and_resize"
    ENTER_SAFE_MODE = "enter_safe_mode"


_RECOVERY: dict[RejectClass, RecoveryAction] = {
    RejectClass.TRANSIENT: RecoveryAction.RETRY_WITH_FRESH_PRICE,
    RejectClass.CONTRACTUAL: RecoveryAction.REFRESH_CONTRACT_AND_RESIZE,
    RejectClass.AUTHORITY: RecoveryAction.ENTER_SAFE_MODE,
}


def recovery_for(reason: RejectReason) -> RecoveryAction:
    """Map a rejection to its only permitted recovery."""

    return _RECOVERY[REJECT_CLASS[reason]]
