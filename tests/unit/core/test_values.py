from decimal import Decimal

import pytest
from pydantic import TypeAdapter, ValidationError

from trading_house.core.values import (
    AssetClass,
    Horizon,
    InstrumentId,
    PositiveQuantity,
    Quantity,
    TimeInForce,
)


def test_quantity_permits_zero() -> None:
    assert Quantity(amount=Decimal("0"), unit="lots").amount == Decimal("0")


def test_positive_quantity_rejects_zero() -> None:
    with pytest.raises(ValidationError):
        PositiveQuantity(amount=Decimal("0"), unit="lots")


def test_quantity_rejects_float_under_strict_mode() -> None:
    with pytest.raises(ValidationError):
        Quantity(amount=0.1, unit="lots")  # type: ignore[arg-type]


def test_quantity_rejects_bool_amount() -> None:
    with pytest.raises(ValidationError):
        Quantity(amount=True, unit="lots")  # type: ignore[arg-type]


def test_quantity_rejects_unknown_unit() -> None:
    with pytest.raises(ValidationError):
        Quantity(amount=Decimal("1"), unit="bushels")  # type: ignore[arg-type]


def test_quantity_is_frozen() -> None:
    quantity = Quantity(amount=Decimal("1"), unit="lots")
    with pytest.raises(ValidationError):
        quantity.amount = Decimal("2")


def test_enums_use_stable_wire_values() -> None:
    assert AssetClass.EQUITY_CFD.value == "equity_cfd"
    assert Horizon.SCALP.value == "scalp"
    assert TimeInForce.GTC.value == "GTC"


@pytest.mark.parametrize("value", ["fx.eurusd", "metal.xauusd", "equity_cfd.aapl"])
def test_instrument_id_accepts_the_asset_class_convention(value: str) -> None:
    assert TypeAdapter(InstrumentId).validate_python(value) == value


@pytest.mark.parametrize("value", ["EURUSD", "fx.", "fx..x", "_fx.x", "fx_.eurusd", "Fx.eurusd"])
def test_instrument_id_rejects_malformed_identifiers(value: str) -> None:
    with pytest.raises(ValidationError):
        TypeAdapter(InstrumentId).validate_python(value)
