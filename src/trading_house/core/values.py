"""Primitive value types shared by every canonical contract."""

from decimal import Decimal
from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator


class CanonicalModel(BaseModel):
    """Base model for canonical data exchanged between trading services."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")


NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=512)]
FiniteFloat = Annotated[float, Field(allow_inf_nan=False)]
PositiveFiniteFloat = Annotated[float, Field(gt=0, allow_inf_nan=False)]
NonNegativeFiniteFloat = Annotated[float, Field(ge=0, allow_inf_nan=False)]
Probability = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]

QuantityUnit = Literal["lots", "shares", "base_units", "contracts"]

PositiveDecimal = Annotated[Decimal, Field(gt=0)]
NonNegativeDecimal = Annotated[Decimal, Field(ge=0)]
Price = Annotated[Decimal, Field(gt=0)]
BasisPoints = Annotated[Decimal, Field(ge=0)]
InstrumentId = Annotated[str, StringConstraints(pattern=r"^[a-z]+(_[a-z]+)*\.[a-z0-9_]+$")]
BookId = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*$", max_length=64)]


class AssetClass(str, Enum):  # noqa: UP042
    FX = "fx"
    METAL = "metal"
    EQUITY_CFD = "equity_cfd"


class Horizon(str, Enum):  # noqa: UP042
    SCALP = "scalp"
    SWING = "swing"


class TimeInForce(str, Enum):  # noqa: UP042
    GTC = "GTC"
    DAY = "DAY"
    IOC = "IOC"
    FOK = "FOK"


class IntentState(str, Enum):  # noqa: UP042
    SUBMITTING = "SUBMITTING"
    CONFIRMED = "CONFIRMED"
    UNKNOWN = "UNKNOWN"
    RECONCILING = "RECONCILING"
    FAILED = "FAILED"
    REJECTED = "REJECTED"


class Quantity(CanonicalModel):
    """A venue-neutral amount. Zero is permitted only where zero is a valid outcome."""

    amount: Annotated[Decimal, Field(ge=0)]
    unit: QuantityUnit

    @field_validator("amount", mode="before")
    @classmethod
    def reject_boolean_amount(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("boolean is not a valid quantity")
        return value


class PositiveQuantity(Quantity):
    """The only quantity an executable intent or open position may carry."""

    amount: Annotated[Decimal, Field(gt=0)]
