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
    Mt5Position,
    Mt5SymbolInfo,
)
from trading_house.core.errors import ConfigurationError
from trading_house.core.instruments import FillPolicy, FinancingModel, InstrumentContract
from trading_house.core.values import AssetClass, InstrumentId
from trading_house.core.venue import PositionRecord


def decimal_of(value: float) -> Decimal:
    """Convert without binary artifacts. Never use ``Decimal(float)`` directly.

    MetaTrader 5 returns floats; the canonical contracts are Decimal. Every
    conversion goes through this because ``Decimal(0.1)`` is
    ``0.1000000000000000055511151231257827…``, and a silent artifact in
    ``value_per_price_increment`` propagates straight into position sizing.
    """

    return Decimal(str(value))


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


def asset_class_for(instrument_id: str) -> AssetClass:
    """Derive the asset class from the instrument id's prefix."""

    prefix = instrument_id.split(".", 1)[0]
    asset_class = _PREFIX_ASSET_CLASS.get(prefix)
    if asset_class is None:
        raise ConfigurationError()
    return asset_class


def supported_fills(filling_mode: int, trade_exemode: int) -> frozenset[FillPolicy]:
    """Derive fill policies from the bitmask AND the execution mode.

    A symbol whose broker reports no usable filling mode is refused rather
    than given a default. ``RETURN`` is not permitted under Request or Instant
    execution, so defaulting to it to satisfy
    ``InstrumentContract.supported_fills``'s ``min_length=1`` would put a fill
    policy the broker never offered into a domain model -- the fabrication
    this project refuses everywhere else, done to appease a schema.
    """

    policies: set[FillPolicy] = set()
    if filling_mode & SYMBOL_FILLING_FOK:
        policies.add(FillPolicy.FOK)
    if filling_mode & SYMBOL_FILLING_IOC:
        policies.add(FillPolicy.IOC)
    if trade_exemode in _RETURN_EXECUTION_MODES:
        policies.add(FillPolicy.RETURN)
    if not policies:
        raise ConfigurationError()
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
        point_size=point,
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


def position_record_of(position: Mt5Position) -> PositionRecord:
    """Map one broker position into the neutral, execution-owned shape.

    MT5 reports "no stop" as 0.0, which is also a syntactically valid price.
    Carrying the zero through would make an unprotected position
    indistinguishable from one protected at zero, so it becomes ``None``
    here, once, rather than at every caller.
    """

    return PositionRecord(
        magic=position.magic,
        server_symbol=position.server_symbol,
        volume=decimal_of(position.volume),
        position_ticket=position.ticket,
        stop_loss=decimal_of(position.sl) if position.sl != 0.0 else None,
        open_price=decimal_of(position.price_open),
        is_buy=position.is_buy,
        opened_at=position.opened_at,
    )
