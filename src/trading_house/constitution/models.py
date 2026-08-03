"""Strict, immutable risk-constitution models."""

from decimal import Decimal, DecimalException
from typing import Annotated, Literal, Self

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PositiveInt,
    ValidationError,
    field_validator,
    model_validator,
)

from trading_house.core.errors import ConfigurationError

PositiveDecimal = Annotated[Decimal, Field(gt=0)]
Percentage = Annotated[Decimal, Field(gt=0, le=Decimal("100"))]


def _integer_to_decimal(value: object) -> object:
    if type(value) is int:
        return Decimal(value)
    return value


class ConstitutionModel(BaseModel):
    """Base model for immutable, strict risk-constitution data."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")


class BookLimits(ConstitutionModel):
    capital_fraction: PositiveDecimal
    risk_per_trade_pct: Percentage
    max_concurrent_positions: PositiveInt
    daily_loss_stop_pct: Percentage
    max_drawdown_halt_pct: Percentage
    max_gross_leverage: PositiveDecimal

    @field_validator(
        "capital_fraction",
        "risk_per_trade_pct",
        "daily_loss_stop_pct",
        "max_drawdown_halt_pct",
        "max_gross_leverage",
        mode="before",
    )
    @classmethod
    def convert_integer_decimals(cls, value: object) -> object:
        return _integer_to_decimal(value)


class Books(ConstitutionModel):
    core: BookLimits
    sleeve: BookLimits


class FirmLimits(ConstitutionModel):
    max_total_drawdown_halt_pct: Percentage
    max_correlated_cluster_risk_pct: Percentage
    max_single_symbol_risk_pct: Percentage
    max_orders_per_minute: PositiveInt
    max_consecutive_rejects: PositiveInt

    @field_validator(
        "max_total_drawdown_halt_pct",
        "max_correlated_cluster_risk_pct",
        "max_single_symbol_risk_pct",
        mode="before",
    )
    @classmethod
    def convert_integer_decimals(cls, value: object) -> object:
        return _integer_to_decimal(value)


class Prohibitions(ConstitutionModel):
    martingale_sizing: Literal["forbidden"]
    averaging_into_losers: Literal["forbidden_unless_declared_in_strategy_spec"]
    stop_removal: Literal["forbidden"]
    stop_widening: Literal["forbidden"]
    leverage_increase_after_loss: Literal["forbidden"]
    trading_without_protective_stop: Literal["forbidden"]


class SafeModeTriggers(ConstitutionModel):
    max_tick_age_seconds: PositiveDecimal
    max_spread_multiple_of_median: PositiveDecimal
    max_clock_drift_ms: PositiveInt
    reconciliation_mismatch: Literal[True]
    slippage_breach_sigma: PositiveDecimal

    @field_validator(
        "max_tick_age_seconds",
        "max_spread_multiple_of_median",
        "slippage_breach_sigma",
        mode="before",
    )
    @classmethod
    def convert_integer_decimals(cls, value: object) -> object:
        return _integer_to_decimal(value)

    @field_validator("reconciliation_mismatch", mode="before")
    @classmethod
    def reconciliation_mismatch_is_true_boolean(cls, value: object) -> object:
        if value is not True:
            raise ValueError("Input should be a valid boolean True")
        return value


class Constitution(ConstitutionModel):
    version: PositiveInt
    signature_required: Literal[True]
    books: Books
    firm: FirmLimits
    prohibitions: Prohibitions
    safe_mode_triggers: SafeModeTriggers

    @field_validator("signature_required", mode="before")
    @classmethod
    def signature_required_is_true_boolean(cls, value: object) -> object:
        if value is not True:
            raise ValueError("Input should be a valid boolean True")
        return value

    @model_validator(mode="after")
    def capital_fractions_sum_to_one(self) -> Self:
        if self.books.core.capital_fraction + self.books.sleeve.capital_fraction != Decimal("1"):
            raise ValueError("book capital fractions must sum exactly to 1")
        return self


class DecimalSafeLoader(yaml.SafeLoader):
    """Safe YAML loader that preserves float scalar precision as Decimals."""


def _construct_decimal(loader: DecimalSafeLoader, node: yaml.ScalarNode) -> Decimal:
    return Decimal(loader.construct_scalar(node))


DecimalSafeLoader.add_constructor("tag:yaml.org,2002:float", _construct_decimal)


def parse_constitution_yaml(data: bytes) -> Constitution:
    """Parse a checked-in constitution without exposing invalid source data."""

    try:
        parsed = yaml.load(data, Loader=DecimalSafeLoader)  # noqa: S506
        if not isinstance(parsed, dict):
            raise ConfigurationError()
        return Constitution.model_validate(parsed)
    except (yaml.YAMLError, UnicodeDecodeError, DecimalException, ValidationError) as error:
        raise ConfigurationError() from error
