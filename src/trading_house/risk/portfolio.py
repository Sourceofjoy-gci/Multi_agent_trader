"""What is already open, lost and sent: the portfolio the risk engine judges a
new order against.

Pure data plus the arithmetic that sums it. ``risk/`` may import ``core/`` and
``constitution/`` only, so nothing here can read a broker or a store -- the live
builder lives in ``ops/portfolio.py`` and the backtester passes ``flat()``.

Unknown is never zero. A position whose risk could not be measured is counted
in ``unmeasured_positions``, and P&L that could not be read is ``None``; the
engine refuses on both rather than treating the hole as an empty book.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Final

from pydantic import Field, NonNegativeInt, field_validator

from trading_house.core.clock import ensure_utc
from trading_house.core.errors import TimestampError
from trading_house.core.instruments import InstrumentContract
from trading_house.core.values import (
    AssetClass,
    BookId,
    CanonicalModel,
    InstrumentId,
    NonEmptyStr,
    NonNegativeDecimal,
)

ORDER_RATE_WINDOW: Final = timedelta(minutes=1)

_CURRENCY_CLUSTERED: Final = frozenset({AssetClass.FX, AssetClass.METAL})


def cluster_keys(contract: InstrumentContract) -> frozenset[str]:
    """The correlation clusters an instrument belongs to.

    FX and metals cluster by currency leg -- the master spec's "USD risk" --
    so EURUSD is ``ccy:EUR`` and ``ccy:USD``. Anything else clusters by asset
    class, the spec's "index beta". Derived from the contract rather than a
    table because membership is a classification, not a limit: the limit
    itself stays behind the constitution's signature.
    """

    if contract.asset_class in _CURRENCY_CLUSTERED:
        return frozenset({f"ccy:{contract.base_currency}", f"ccy:{contract.quote_currency}"})
    return frozenset({f"class:{contract.asset_class.value}"})


def notional_money(quantity: Decimal, price: Decimal, contract: InstrumentContract) -> Decimal:
    """Account-currency notional: 1 lot of EURUSD at 1.10000 is 110,000."""

    return quantity * (price / contract.price_increment) * contract.value_per_price_increment


def loss_to_stop(
    *,
    quantity: Decimal,
    from_price: Decimal,
    stop: Decimal,
    is_buy: bool,
    contract: InstrumentContract,
) -> Decimal:
    """The money lost if price travels from ``from_price`` to ``stop``.

    Floored at zero: a stop already locked in profit relative to the mark has
    nothing left to lose from here.
    """

    distance = from_price - stop if is_buy else stop - from_price
    if distance <= 0:
        return Decimal(0)
    return distance / contract.price_increment * contract.value_per_price_increment * quantity


class OpenExposure(CanonicalModel):
    """One open position whose risk could be measured.

    ``book`` is ``None`` for a position no book owns -- a manual trade or a
    foreign magic. It still shares the account's margin pool, so it counts
    toward every firm limit and toward no book's.
    """

    book: BookId | None
    instrument_id: InstrumentId
    cluster_keys: frozenset[NonEmptyStr] = Field(min_length=1)
    risk_money: NonNegativeDecimal
    notional_money: NonNegativeDecimal


class BookPnl(CanonicalModel):
    """One book's money so far. ``drawdown`` is peak minus current, never negative."""

    realized_today: Decimal
    unrealized: Decimal
    drawdown: NonNegativeDecimal

    def loss_today(self) -> Decimal:
        """Realised loss today plus unrealised loss, net of realised gains.

        Unrealised gains never offset: a position carrying yesterday's profit
        must not mask what was lost today.
        """

        return max(Decimal(0), -(self.realized_today + min(self.unrealized, Decimal(0))))


class OrderStamp(CanonicalModel):
    book: BookId
    submitted_at: datetime

    @field_validator("submitted_at")
    @classmethod
    def normalize(cls, value: datetime) -> datetime:
        try:
            return ensure_utc(value)
        except TimestampError as error:
            raise ValueError(str(error)) from error


class PortfolioState(CanonicalModel):
    exposures: tuple[OpenExposure, ...]
    unmeasured_positions: NonNegativeInt
    book_pnl: Mapping[BookId, BookPnl] | None
    firm_drawdown: NonNegativeDecimal | None
    order_stamps: tuple[OrderStamp, ...]
    consecutive_rejects: NonNegativeInt

    @classmethod
    def flat(cls, books: Iterable[BookId]) -> PortfolioState:
        """Nothing open, nothing lost, nothing sent.

        The backtester's state, by decision: it takes decisions only when flat,
        and a drawdown halt in a replay would cap a losing rule's reported loss
        (Phase 10 design, section 5.1).
        """

        zero = Decimal(0)
        return cls(
            exposures=(),
            unmeasured_positions=0,
            book_pnl={
                book: BookPnl(realized_today=zero, unrealized=zero, drawdown=zero) for book in books
            },
            firm_drawdown=zero,
            order_stamps=(),
            consecutive_rejects=0,
        )

    def book_exposures(self, book: BookId) -> tuple[OpenExposure, ...]:
        return tuple(exposure for exposure in self.exposures if exposure.book == book)

    def open_risk(self, *, book: BookId | None = None) -> Decimal:
        """Firm-wide open risk, or one book's when ``book`` is given."""

        exposures = self.exposures if book is None else self.book_exposures(book)
        return sum((exposure.risk_money for exposure in exposures), Decimal(0))

    def notional(self, *, book: BookId | None = None) -> Decimal:
        exposures = self.exposures if book is None else self.book_exposures(book)
        return sum((exposure.notional_money for exposure in exposures), Decimal(0))

    def instrument_risk(self, instrument_id: InstrumentId) -> Decimal:
        return sum(
            (e.risk_money for e in self.exposures if e.instrument_id == instrument_id),
            Decimal(0),
        )

    def cluster_risk(self, key: str) -> Decimal:
        return sum((e.risk_money for e in self.exposures if key in e.cluster_keys), Decimal(0))

    def orders_in_window(self, now: datetime, *, book: BookId | None = None) -> int:
        """Submissions in ``(now - 1 minute, now]``."""

        start = ensure_utc(now) - ORDER_RATE_WINDOW
        return sum(
            1
            for stamp in self.order_stamps
            if start < stamp.submitted_at <= now and (book is None or stamp.book == book)
        )
