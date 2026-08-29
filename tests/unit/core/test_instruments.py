from decimal import Decimal

import pytest
from pydantic import ValidationError

from trading_house.core.instruments import FillPolicy, FinancingModel, InstrumentContract
from trading_house.core.values import AssetClass

VALID: dict[str, object] = {
    "instrument_id": "fx.eurusd",
    "asset_class": AssetClass.FX,
    "base_currency": "EUR",
    "quote_currency": "USD",
    "price_increment": Decimal("0.00001"),
    "quantity_increment": Decimal("0.01"),
    "quantity_min": Decimal("0.01"),
    "quantity_max": Decimal("100"),
    "value_per_price_increment": Decimal("1"),
    "min_stop_distance": Decimal("0.0002"),
    "freeze_distance": Decimal("0.0001"),
    "session_calendar_id": "fx.24x5",
    "financing": FinancingModel.SWAP,
    "opens_new_positions": True,
    "shortable": True,
    "supported_fills": frozenset({FillPolicy.IOC, FillPolicy.FOK}),
}


def test_valid_contract_builds() -> None:
    contract = InstrumentContract(**VALID)
    assert contract.asset_class is AssetClass.FX
    assert contract.price_increment == Decimal("0.00001")


def test_contract_is_frozen() -> None:
    contract = InstrumentContract(**VALID)
    with pytest.raises(ValidationError):
        contract.shortable = False


def test_quantity_bounds_must_be_ordered() -> None:
    with pytest.raises(ValidationError, match="quantity_max"):
        InstrumentContract(**{**VALID, "quantity_max": Decimal("0.001")})


def test_quantity_min_must_be_a_multiple_of_the_increment() -> None:
    with pytest.raises(ValidationError, match="quantity_increment"):
        InstrumentContract(**{**VALID, "quantity_min": Decimal("0.015")})


def test_at_least_one_fill_policy_is_required() -> None:
    with pytest.raises(ValidationError):
        InstrumentContract(**{**VALID, "supported_fills": frozenset()})


def test_equity_cfd_may_be_long_only() -> None:
    contract = InstrumentContract(
        **{
            **VALID,
            "instrument_id": "equity_cfd.aapl",
            "asset_class": AssetClass.EQUITY_CFD,
            "financing": FinancingModel.DIVIDEND_ADJUSTMENT,
            "shortable": False,
            "session_calendar_id": "xnas.regular",
        }
    )
    assert contract.shortable is False


def test_distances_are_rejected_when_not_positive() -> None:
    with pytest.raises(ValidationError):
        InstrumentContract(**{**VALID, "min_stop_distance": Decimal("0")})
