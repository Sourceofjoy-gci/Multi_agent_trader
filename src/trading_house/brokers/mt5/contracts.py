# src/trading_house/brokers/mt5/contracts.py
"""Map MetaTrader 5 symbol metadata onto the venue-neutral InstrumentContract.

Three conversions carry real risk and are handled explicitly here:

* MetaTrader 5 returns floats; the canonical contracts are Decimal. Every
  conversion goes through ``Decimal(str(value))`` because ``Decimal(0.1)`` is
  ``0.1000000000000000055511151231257827…``, and a silent artifact in
  ``value_per_price_increment`` propagates straight into position sizing.
* ``stops_level`` and ``freeze_level`` are in POINTS; the contract is in price
  units. A broker reporting zero means "no minimum", which a PositiveDecimal
  cannot express, so the value floors to one price increment — a stop must be
  at least one tick from the market regardless.
* ``supported_fills`` needs the filling bitmask AND the execution mode.
  Deriving it from the bitmask alone silently drops RETURN.
"""

from __future__ import annotations

from decimal import Decimal

from trading_house.brokers.mt5.boundary import (
    SYMBOL_FILLING_FOK,
    SYMBOL_FILLING_IOC,
    SYMBOL_TRADE_EXECUTION_EXCHANGE,
    SYMBOL_TRADE_EXECUTION_MARKET,
    SYMBOL_TRADE_MODE_DISABLED,
    SYMBOL_TRADE_MODE_FULL,
    SYMBOL_TRADE_MODE_LONGONLY,
    SYMBOL_TRADE_MODE_SHORTONLY,
    Mt5SymbolInfo,
)
from trading_house.core.errors import ConfigurationError
from trading_house.core.instruments import FillPolicy, FinancingModel, InstrumentContract
from trading_house.core.values import AssetClass, InstrumentId

_PREFIX_ASSET_CLASS = {
    "fx": AssetClass.FX,
    "metal": AssetClass.METAL,
    "equity_cfd": AssetClass.EQUITY_CFD,
}

_LONG_TRADE_MODES = frozenset({SYMBOL_TRADE_MODE_FULL, SYMBOL_TRADE_MODE_LONGONLY})
_SHORT_TRADE_MODES = frozenset({SYMBOL_TRADE_MODE_FULL, SYMBOL_TRADE_MODE_SHORTONLY})
_RETURN_EXECUTION_MODES = frozenset(
    {SYMBOL_TRADE_EXECUTION_MARKET, SYMBOL_TRADE_EXECUTION_EXCHANGE}
)


def decimal_of(value: float) -> Decimal:
    """Convert without binary artifacts. Never use ``Decimal(float)`` directly."""

    return Decimal(str(value))


def asset_class_for(instrument_id: str) -> AssetClass:
    """Derive the asset class from the instrument id's prefix."""

    prefix = instrument_id.split(".", 1)[0]
    asset_class = _PREFIX_ASSET_CLASS.get(prefix)
    if asset_class is None:
        raise ConfigurationError()
    return asset_class


def supported_fills(filling_mode: int, trade_exemode: int) -> frozenset[FillPolicy]:
    """Derive fill policies from the bitmask AND the execution mode.

    Always returns at least ``{RETURN}`` — never empty — which is what makes
    ``InstrumentContract.supported_fills``'s ``min_length=1`` safe.
    """

    policies: set[FillPolicy] = set()
    if filling_mode & SYMBOL_FILLING_FOK:
        policies.add(FillPolicy.FOK)
    if filling_mode & SYMBOL_FILLING_IOC:
        policies.add(FillPolicy.IOC)
    if trade_exemode in _RETURN_EXECUTION_MODES or not policies:
        policies.add(FillPolicy.RETURN)
    return frozenset(policies)


def _financing_for(asset_class: AssetClass) -> FinancingModel:
    if asset_class is AssetClass.EQUITY_CFD:
        return FinancingModel.DIVIDEND_ADJUSTMENT
    return FinancingModel.SWAP


def to_instrument_contract(
    info: Mt5SymbolInfo, *, instrument_id: InstrumentId
) -> InstrumentContract:
    """Build the neutral contract, rejecting symbols that cannot be traded."""

    if info.trade_mode == SYMBOL_TRADE_MODE_DISABLED:
        raise ConfigurationError()

    asset_class = asset_class_for(instrument_id)
    point = decimal_of(info.point)
    price_increment = decimal_of(info.trade_tick_size)
    fills = supported_fills(info.filling_mode, info.trade_exemode)

    return InstrumentContract(
        instrument_id=instrument_id,
        asset_class=asset_class,
        base_currency=info.currency_base,
        quote_currency=info.currency_profit,
        price_increment=price_increment,
        quantity_increment=decimal_of(info.volume_step),
        quantity_min=decimal_of(info.volume_min),
        quantity_max=decimal_of(info.volume_max),
        value_per_price_increment=decimal_of(info.trade_tick_value_loss),
        min_stop_distance=max(decimal_of(info.trade_stops_level) * point, price_increment),
        freeze_distance=max(decimal_of(info.trade_freeze_level) * point, price_increment),
        session_calendar_id=f"{asset_class.value}.default",
        financing=_financing_for(asset_class),
        can_open_long=info.trade_mode in _LONG_TRADE_MODES,
        can_open_short=info.trade_mode in _SHORT_TRADE_MODES,
        supported_fills=fills,
    )
