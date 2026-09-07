"""Shared builders for the risk tests, imported rather than duplicated.

The ``constitution`` fixture loads the real signed constitution, so the gates
are tested against the values that will actually bind in production rather
than against a fixture that can drift away from them.
"""

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from trading_house.constitution.loader import load_constitution
from trading_house.constitution.models import Constitution
from trading_house.core.instruments import FillPolicy, FinancingModel, InstrumentContract
from trading_house.core.schemas import Side, TradeProposal
from trading_house.core.values import AssetClass

CONFIG_DIR = Path(__file__).resolve().parents[3] / "config"

NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)


@pytest.fixture(scope="session")
def constitution() -> Constitution:
    loaded = load_constitution(
        CONFIG_DIR / "risk_constitution.yaml",
        CONFIG_DIR / "risk_constitution.yaml.sig",
        CONFIG_DIR / "risk_constitution.public.pem",
    )
    return loaded.constitution


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


def _proposal(**overrides: object) -> TradeProposal:
    fields: dict[str, object] = {
        "event_time": NOW,
        "availability_time": NOW,
        "processing_time": NOW,
        "source": "test",
        "proposal_id": "p-1",
        "strategy_id": "s-1",
        "strategy_version": "1",
        "book": "fx_scalp",
        "instrument_id": "fx.eurusd",
        "side": Side.BUY,
        "horizon_seconds": 60,
        "entry_condition": "test",
        "entry_price_ref": Decimal("1.10000"),
        "invalidation_price": Decimal("1.09700"),
        "max_holding_seconds": 300,
        "expected_return_bps": 5.0,
        "expected_return_stdev_bps": 2.0,
        "expected_cost_bps": 1.0,
        "win_probability": 0.55,
        "calibration_id": "c-1",
        "required_liquidity": {"amount": Decimal("1"), "unit": "lots"},
        "regime_ref": "r-1",
        "features_snapshot_id": "f-1",
    }
    fields.update(overrides)
    return TradeProposal.model_validate(fields)


def _facts(**overrides: object) -> dict[str, object]:
    facts: dict[str, object] = {
        "firm_equity": Decimal("100000"),
        "atr": Decimal("0.00050"),
        "median_spread_points": Decimal("10"),
        "tick_spread_points": Decimal("10"),
        "tick_time": NOW,
    }
    facts.update(overrides)
    return facts
