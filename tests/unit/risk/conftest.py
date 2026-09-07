"""Shared builders for the risk tests, imported rather than duplicated."""

from decimal import Decimal

from trading_house.core.instruments import FillPolicy, FinancingModel, InstrumentContract
from trading_house.core.values import AssetClass


def _contract(**overrides: object) -> InstrumentContract:
    """A 5-digit FX contract. 1 lot of EURUSD moves $1 per 0.00001 of price."""

    fields: dict[str, object] = {
        "instrument_id": "fx.eurusd",
        "asset_class": AssetClass.FX,
        "base_currency": "EUR",
        "quote_currency": "USD",
        "price_increment": Decimal("0.00001"),
        "point_size": Decimal("0.00001"),
        "quantity_increment": Decimal("0.01"),
        "quantity_min": Decimal("0.01"),
        "quantity_max": Decimal("100"),
        "value_per_price_increment": Decimal("1"),
        "min_stop_distance": Decimal("0.00001"),
        "freeze_distance": Decimal("0.00001"),
        "session_calendar_id": "fx.default",
        "financing": FinancingModel.SWAP,
        "can_open_long": True,
        "can_open_short": True,
        "supported_fills": frozenset({FillPolicy.IOC}),
    }
    fields.update(overrides)
    return InstrumentContract.model_validate(fields)
