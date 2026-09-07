"""The venue-neutral description of a tradable instrument.

Every distance is in price units. Points are an MT5 encoding and are the
adapter's business, not the contract's.
"""

from enum import Enum
from typing import Self

from pydantic import Field, model_validator

from trading_house.core.values import (
    AssetClass,
    CanonicalModel,
    InstrumentId,
    NonEmptyStr,
    PositiveDecimal,
)


class FillPolicy(str, Enum):  # noqa: UP042
    IOC = "IOC"
    FOK = "FOK"
    RETURN = "RETURN"


class FinancingModel(str, Enum):  # noqa: UP042
    SWAP = "swap"
    DIVIDEND_ADJUSTMENT = "dividend_adjustment"


class InstrumentContract(CanonicalModel):
    """What the venue guarantees about one instrument, in neutral terms."""

    instrument_id: InstrumentId
    asset_class: AssetClass
    base_currency: NonEmptyStr
    quote_currency: NonEmptyStr
    price_increment: PositiveDecimal
    # MT5's `point`. Equal to price_increment on most FX symbols and NOT the
    # same field: a stored Bar.spread is an integer count of these, so without
    # it a spread cannot be converted to a price distance.
    point_size: PositiveDecimal
    quantity_increment: PositiveDecimal
    quantity_min: PositiveDecimal
    quantity_max: PositiveDecimal
    value_per_price_increment: PositiveDecimal
    min_stop_distance: PositiveDecimal
    freeze_distance: PositiveDecimal
    session_calendar_id: NonEmptyStr
    financing: FinancingModel
    # Encodes MT5's trade modes exactly: both false means close-only (no new
    # positions in either direction, only closing existing ones).
    can_open_long: bool
    can_open_short: bool
    supported_fills: frozenset[FillPolicy] = Field(min_length=1)

    @model_validator(mode="after")
    def quantity_bounds_are_coherent(self) -> Self:
        if self.quantity_max < self.quantity_min:
            raise ValueError("quantity_max must not be below quantity_min")
        if self.quantity_min % self.quantity_increment != 0:
            raise ValueError("quantity_min must be a multiple of quantity_increment")
        return self
