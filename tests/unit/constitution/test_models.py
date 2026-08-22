from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from trading_house.constitution.models import Constitution, parse_constitution_yaml
from trading_house.core.errors import ConfigurationError


@pytest.fixture
def valid_data() -> dict[str, object]:
    return {
        "version": 1,
        "signature_required": True,
        "books": {
            "core": {
                "capital_fraction": Decimal("0.90"),
                "risk_per_trade_pct": Decimal("0.35"),
                "max_concurrent_positions": 8,
                "daily_loss_stop_pct": Decimal("1.5"),
                "max_drawdown_halt_pct": Decimal("8.0"),
                "max_gross_leverage": Decimal("5.0"),
            },
            "sleeve": {
                "capital_fraction": Decimal("0.10"),
                "risk_per_trade_pct": Decimal("1.5"),
                "max_concurrent_positions": 2,
                "daily_loss_stop_pct": Decimal("8.0"),
                "max_drawdown_halt_pct": Decimal("40.0"),
                "max_gross_leverage": Decimal("20.0"),
            },
        },
        "firm": {
            "max_total_drawdown_halt_pct": Decimal("10.0"),
            "max_correlated_cluster_risk_pct": Decimal("1.0"),
            "max_single_symbol_risk_pct": Decimal("0.7"),
            "max_orders_per_minute": 30,
            "max_consecutive_rejects": 5,
        },
        "prohibitions": {
            "martingale_sizing": "forbidden",
            "averaging_into_losers": "forbidden_unless_declared_in_strategy_spec",
            "stop_removal": "forbidden",
            "stop_widening": "forbidden",
            "leverage_increase_after_loss": "forbidden",
            "trading_without_protective_stop": "forbidden",
        },
        "safe_mode_triggers": {
            "max_tick_age_seconds": Decimal("5"),
            "max_spread_multiple_of_median": Decimal("3.0"),
            "max_clock_drift_ms": 500,
            "reconciliation_mismatch": True,
            "slippage_breach_sigma": Decimal("3.0"),
        },
    }


def test_checked_in_constitution_matches_spec() -> None:
    model = parse_constitution_yaml(Path("config/risk_constitution.yaml").read_bytes())

    assert model.version == 1
    assert model.signature_required is True
    assert model.books.core.capital_fraction == Decimal("0.90")
    assert model.books.core.risk_per_trade_pct == Decimal("0.35")
    assert model.books.core.max_concurrent_positions == 8
    assert model.books.core.daily_loss_stop_pct == Decimal("1.5")
    assert model.books.core.max_drawdown_halt_pct == Decimal("8.0")
    assert model.books.core.max_gross_leverage == Decimal("5.0")
    assert model.books.sleeve.capital_fraction == Decimal("0.10")
    assert model.books.sleeve.risk_per_trade_pct == Decimal("1.5")
    assert model.books.sleeve.max_concurrent_positions == 2
    assert model.books.sleeve.daily_loss_stop_pct == Decimal("8.0")
    assert model.books.sleeve.max_drawdown_halt_pct == Decimal("40.0")
    assert model.books.sleeve.max_gross_leverage == Decimal("20.0")
    assert model.firm.max_total_drawdown_halt_pct == Decimal("10.0")
    assert model.firm.max_correlated_cluster_risk_pct == Decimal("1.0")
    assert model.firm.max_single_symbol_risk_pct == Decimal("0.7")
    assert model.firm.max_orders_per_minute == 30
    assert model.firm.max_consecutive_rejects == 5
    assert model.prohibitions.martingale_sizing == "forbidden"
    assert model.prohibitions.averaging_into_losers == "forbidden_unless_declared_in_strategy_spec"
    assert model.prohibitions.stop_removal == "forbidden"
    assert model.prohibitions.stop_widening == "forbidden"
    assert model.prohibitions.leverage_increase_after_loss == "forbidden"
    assert model.prohibitions.trading_without_protective_stop == "forbidden"
    assert model.safe_mode_triggers.max_tick_age_seconds == Decimal("5")
    assert model.safe_mode_triggers.max_spread_multiple_of_median == Decimal("3.0")
    assert model.safe_mode_triggers.max_clock_drift_ms == 500
    assert model.safe_mode_triggers.reconciliation_mismatch is True
    assert model.safe_mode_triggers.slippage_breach_sigma == Decimal("3.0")


def test_capital_fractions_must_sum_exactly_to_one(valid_data: dict[str, object]) -> None:
    books = valid_data["books"]
    assert isinstance(books, dict)
    sleeve = books["sleeve"]
    assert isinstance(sleeve, dict)
    sleeve["capital_fraction"] = Decimal("0.11")

    with pytest.raises(ValidationError, match="sum exactly to 1"):
        Constitution.model_validate(valid_data)


def test_requires_both_books(valid_data: dict[str, object]) -> None:
    books = valid_data["books"]
    assert isinstance(books, dict)
    books.pop("sleeve")

    with pytest.raises(ValidationError, match="sleeve"):
        Constitution.model_validate(valid_data)


def test_requires_signature(valid_data: dict[str, object]) -> None:
    valid_data["signature_required"] = False

    with pytest.raises(ValidationError, match="True"):
        Constitution.model_validate(valid_data)


@pytest.mark.parametrize("alias", [1, 1.0, Decimal("1")])
@pytest.mark.parametrize(
    "field_path",
    [
        ("signature_required",),
        ("safe_mode_triggers", "reconciliation_mismatch"),
    ],
)
def test_mandatory_flags_reject_numeric_true_aliases(
    valid_data: dict[str, object], field_path: tuple[str, ...], alias: object
) -> None:
    target: dict[str, object] = valid_data
    for key in field_path[:-1]:
        nested = target[key]
        assert isinstance(nested, dict)
        target = nested
    target[field_path[-1]] = alias

    with pytest.raises(ValidationError, match="valid boolean"):
        Constitution.model_validate(valid_data)


def test_rejects_wrong_prohibition_literal(valid_data: dict[str, object]) -> None:
    prohibitions = valid_data["prohibitions"]
    assert isinstance(prohibitions, dict)
    prohibitions["martingale_sizing"] = "allowed"

    with pytest.raises(ValidationError, match="martingale_sizing"):
        Constitution.model_validate(valid_data)


def test_forbids_unknown_fields(valid_data: dict[str, object]) -> None:
    valid_data["unknown"] = "value"

    with pytest.raises(ValidationError, match="Extra inputs"):
        Constitution.model_validate(valid_data)


def test_forbids_nested_unknown_fields(valid_data: dict[str, object]) -> None:
    books = valid_data["books"]
    assert isinstance(books, dict)
    core = books["core"]
    assert isinstance(core, dict)
    core["unapproved_limit"] = Decimal("1")

    with pytest.raises(ValidationError, match="Extra inputs"):
        Constitution.model_validate(valid_data)


def test_rejects_negative_limits(valid_data: dict[str, object]) -> None:
    firm = valid_data["firm"]
    assert isinstance(firm, dict)
    firm["max_single_symbol_risk_pct"] = Decimal("-0.7")

    with pytest.raises(ValidationError, match="greater than 0"):
        Constitution.model_validate(valid_data)


@pytest.mark.parametrize(
    "field_path",
    [
        ("books", "core", "risk_per_trade_pct"),
        ("books", "core", "daily_loss_stop_pct"),
        ("books", "core", "max_drawdown_halt_pct"),
        ("firm", "max_total_drawdown_halt_pct"),
        ("firm", "max_correlated_cluster_risk_pct"),
        ("firm", "max_single_symbol_risk_pct"),
    ],
)
def test_percentage_limits_reject_values_above_100(
    valid_data: dict[str, object], field_path: tuple[str, ...]
) -> None:
    target: dict[str, object] = valid_data
    for key in field_path[:-1]:
        nested = target[key]
        assert isinstance(nested, dict)
        target = nested
    target[field_path[-1]] = Decimal("100.01")

    with pytest.raises(ValidationError, match="less than or equal to 100"):
        Constitution.model_validate(valid_data)


def test_percentage_limit_accepts_100(valid_data: dict[str, object]) -> None:
    books = valid_data["books"]
    assert isinstance(books, dict)
    core = books["core"]
    assert isinstance(core, dict)
    core["risk_per_trade_pct"] = Decimal("100")

    assert Constitution.model_validate(valid_data).books.core.risk_per_trade_pct == Decimal("100")


def test_models_are_immutable(valid_data: dict[str, object]) -> None:
    model = Constitution.model_validate(valid_data)

    with pytest.raises(ValidationError, match="frozen"):
        model.firm.max_orders_per_minute = 60  # type: ignore[misc]


@pytest.mark.parametrize(
    "payload",
    [
        b"version: [",
        b"- not-a-mapping",
        b"version: 1\nsignature_required: true\nsecret: do-not-disclose",
    ],
)
def test_yaml_failures_raise_redacted_configuration_error(payload: bytes) -> None:
    with pytest.raises(ConfigurationError) as error:
        parse_constitution_yaml(payload)

    assert str(error.value) == "configuration invalid"
    assert "do-not-disclose" not in str(error.value)


def test_yaml_float_scalars_remain_decimal(valid_data: dict[str, object]) -> None:
    source = (
        "version: 1\n"
        "signature_required: true\n"
        "books:\n"
        "  core:\n"
        "    capital_fraction: 0.90\n"
        "    risk_per_trade_pct: 0.35\n"
        "    max_concurrent_positions: 8\n"
        "    daily_loss_stop_pct: 1.5\n"
        "    max_drawdown_halt_pct: 8.0\n"
        "    max_gross_leverage: 5.0\n"
        "  sleeve:\n"
        "    capital_fraction: 0.10\n"
        "    risk_per_trade_pct: 1.5\n"
        "    max_concurrent_positions: 2\n"
        "    daily_loss_stop_pct: 8.0\n"
        "    max_drawdown_halt_pct: 40.0\n"
        "    max_gross_leverage: 20.0\n"
        "firm:\n"
        "  max_total_drawdown_halt_pct: 10.0\n"
        "  max_correlated_cluster_risk_pct: 1.0\n"
        "  max_single_symbol_risk_pct: 0.7\n"
        "  max_orders_per_minute: 30\n"
        "  max_consecutive_rejects: 5\n"
        "prohibitions:\n"
        "  martingale_sizing: forbidden\n"
        "  averaging_into_losers: forbidden_unless_declared_in_strategy_spec\n"
        "  stop_removal: forbidden\n"
        "  stop_widening: forbidden\n"
        "  leverage_increase_after_loss: forbidden\n"
        "  trading_without_protective_stop: forbidden\n"
        "safe_mode_triggers:\n"
        "  max_tick_age_seconds: 5\n"
        "  max_spread_multiple_of_median: 3.0\n"
        "  max_clock_drift_ms: 500\n"
        "  reconciliation_mismatch: true\n"
        "  slippage_breach_sigma: 3.0\n"
    )

    model = parse_constitution_yaml(source.encode())

    assert model.books.core.capital_fraction == Decimal("0.90")


@pytest.mark.parametrize("scalar", ["1:2.3", ".nan", ".inf"])
def test_yaml_float_conversion_failures_are_redacted(scalar: str) -> None:
    source = (
        Path("config/risk_constitution.yaml")
        .read_bytes()
        .replace(b"capital_fraction: 0.90", f"capital_fraction: {scalar}".encode())
    )

    with pytest.raises(ConfigurationError) as error:
        parse_constitution_yaml(source)

    assert str(error.value) == "configuration invalid"
    assert scalar not in str(error.value)
