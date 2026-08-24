"""Strict, immutable risk-constitution models."""

from collections.abc import Mapping
from decimal import Decimal, DecimalException
from typing import Annotated, Literal, Self

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    NonNegativeInt,
    PositiveInt,
    ValidationError,
    field_validator,
    model_validator,
)

from trading_house.core.errors import ConfigurationError
from trading_house.core.values import AssetClass, BookId, Horizon

PositiveDecimal = Annotated[Decimal, Field(gt=0)]
Percentage = Annotated[Decimal, Field(gt=0, le=Decimal("100"))]


def _integer_to_decimal(value: object) -> object:
    if type(value) is int:
        return Decimal(value)
    return value


class ConstitutionModel(BaseModel):
    """Base model for immutable, strict risk-constitution data."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")


class ScalpLimits(ConstitutionModel):
    horizon: Literal[Horizon.SCALP]
    max_orders_per_minute: PositiveInt
    max_spread_multiple_at_entry: PositiveDecimal
    min_expected_edge_after_cost_bps: PositiveDecimal
    max_position_duration_seconds: PositiveInt
    flat_by_session_close: Literal[True]

    @field_validator(
        "max_spread_multiple_at_entry",
        "min_expected_edge_after_cost_bps",
        mode="before",
    )
    @classmethod
    def convert_integer_decimals(cls, value: object) -> object:
        return _integer_to_decimal(value)

    @field_validator("flat_by_session_close", mode="before")
    @classmethod
    def flat_by_session_close_is_true_boolean(cls, value: object) -> object:
        if value is not True:
            raise ValueError("Input should be a valid boolean True")
        return value


class SwingLimits(ConstitutionModel):
    horizon: Literal[Horizon.SWING]
    max_overnight_positions: PositiveInt
    max_weekend_exposure_pct: Percentage
    max_swap_cost_pct_of_expected_edge: Percentage
    gap_risk_multiple: PositiveDecimal
    earnings_blackout_days: NonNegativeInt

    @field_validator(
        "max_weekend_exposure_pct",
        "max_swap_cost_pct_of_expected_edge",
        "gap_risk_multiple",
        mode="before",
    )
    @classmethod
    def convert_integer_decimals(cls, value: object) -> object:
        return _integer_to_decimal(value)


HorizonLimits = Annotated[ScalpLimits | SwingLimits, Field(discriminator="horizon")]


class BookLimits(ConstitutionModel):
    capital_fraction: PositiveDecimal
    horizon: Horizon
    asset_classes: tuple[AssetClass, ...] = Field(min_length=1)
    risk_per_trade_pct: Percentage
    max_concurrent_positions: PositiveInt
    daily_loss_stop_pct: Percentage
    max_drawdown_halt_pct: Percentage
    max_gross_leverage: PositiveDecimal
    limits: HorizonLimits

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

    @field_validator("horizon", mode="before")
    @classmethod
    def convert_horizon_string(cls, value: object) -> object:
        if isinstance(value, str):
            return Horizon(value)
        return value

    @field_validator("asset_classes", mode="before")
    @classmethod
    def convert_asset_class_sequence(cls, value: object) -> object:
        if isinstance(value, list | tuple):
            return tuple(AssetClass(item) if isinstance(item, str) else item for item in value)
        return value

    @model_validator(mode="after")
    def limits_match_the_declared_horizon(self) -> Self:
        if self.limits.horizon is not self.horizon:
            raise ValueError("horizon limits must match the book's declared horizon")
        return self


class FirmLimits(ConstitutionModel):
    """Cross-book budgets binding the whole firm (I-16).

    These are the only place correlation and leverage budgets may live: all
    books share one broker account and therefore one margin pool, so a
    per-book budget would hide correlated exposure instead of containing it.
    """

    max_total_drawdown_halt_pct: Percentage
    max_aggregate_open_risk_pct: Percentage
    max_correlated_cluster_risk_pct: Percentage
    max_single_instrument_risk_pct: Percentage
    max_gross_leverage: PositiveDecimal
    max_orders_per_minute: PositiveInt
    max_consecutive_rejects: PositiveInt

    @field_validator(
        "max_total_drawdown_halt_pct",
        "max_aggregate_open_risk_pct",
        "max_correlated_cluster_risk_pct",
        "max_single_instrument_risk_pct",
        "max_gross_leverage",
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
    books: Mapping[BookId, BookLimits] = Field(min_length=1)
    firm: FirmLimits
    prohibitions: Prohibitions
    safe_mode_triggers: Mapping[Horizon, SafeModeTriggers]

    @field_validator("signature_required", mode="before")
    @classmethod
    def signature_required_is_true_boolean(cls, value: object) -> object:
        if value is not True:
            raise ValueError("Input should be a valid boolean True")
        return value

    @field_validator("safe_mode_triggers", mode="before")
    @classmethod
    def convert_safe_mode_trigger_keys(cls, value: object) -> object:
        if isinstance(value, dict):
            return {
                Horizon(key) if isinstance(key, str) else key: item for key, item in value.items()
            }
        return value

    @model_validator(mode="after")
    def capital_fractions_sum_to_one(self) -> Self:
        total = sum((book.capital_fraction for book in self.books.values()), Decimal(0))
        if total != Decimal("1"):
            raise ValueError("book capital fractions must sum exactly to 1")
        return self

    @model_validator(mode="after")
    def every_book_horizon_has_triggers(self) -> Self:
        missing = {book.horizon for book in self.books.values()} - set(self.safe_mode_triggers)
        if missing:
            raise ValueError("every declared book horizon requires safe-mode triggers")
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
