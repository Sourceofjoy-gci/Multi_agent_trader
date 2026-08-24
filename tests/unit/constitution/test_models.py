from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from trading_house.constitution.models import Constitution, parse_constitution_yaml
from trading_house.core.errors import ConfigurationError
from trading_house.core.values import AssetClass, Horizon

CONSTITUTION_BYTES = Path("config/risk_constitution.yaml").read_bytes()


@pytest.fixture
def valid_data() -> dict[str, object]:
    return {
        "version": 1,
        "signature_required": True,
        "books": {
            "fx_scalp": {
                "capital_fraction": Decimal("0.30"),
                "horizon": "scalp",
                "asset_classes": ["fx", "metal"],
                "risk_per_trade_pct": Decimal("0.25"),
                "max_concurrent_positions": 3,
                "daily_loss_stop_pct": Decimal("1.5"),
                "max_drawdown_halt_pct": Decimal("6.0"),
                "max_gross_leverage": Decimal("10.0"),
                "limits": {
                    "horizon": "scalp",
                    "max_orders_per_minute": 15,
                    "max_spread_multiple_at_entry": Decimal("1.5"),
                    "min_expected_edge_after_cost_bps": Decimal("2.0"),
                    "max_position_duration_seconds": 300,
                    "flat_by_session_close": True,
                },
            },
            "fx_swing": {
                "capital_fraction": Decimal("0.45"),
                "horizon": "swing",
                "asset_classes": ["fx", "metal"],
                "risk_per_trade_pct": Decimal("0.75"),
                "max_concurrent_positions": 6,
                "daily_loss_stop_pct": Decimal("2.5"),
                "max_drawdown_halt_pct": Decimal("10.0"),
                "max_gross_leverage": Decimal("5.0"),
                "limits": {
                    "horizon": "swing",
                    "max_overnight_positions": 6,
                    "max_weekend_exposure_pct": Decimal("15.0"),
                    "max_swap_cost_pct_of_expected_edge": Decimal("20.0"),
                    "gap_risk_multiple": Decimal("3.0"),
                    "earnings_blackout_days": 0,
                },
            },
            "equity_swing": {
                "capital_fraction": Decimal("0.15"),
                "horizon": "swing",
                "asset_classes": ["equity_cfd"],
                "risk_per_trade_pct": Decimal("0.50"),
                "max_concurrent_positions": 4,
                "daily_loss_stop_pct": Decimal("2.0"),
                "max_drawdown_halt_pct": Decimal("8.0"),
                "max_gross_leverage": Decimal("3.0"),
                "limits": {
                    "horizon": "swing",
                    "max_overnight_positions": 4,
                    "max_weekend_exposure_pct": Decimal("10.0"),
                    "max_swap_cost_pct_of_expected_edge": Decimal("15.0"),
                    "gap_risk_multiple": Decimal("4.0"),
                    "earnings_blackout_days": 2,
                },
            },
            "sleeve": {
                "capital_fraction": Decimal("0.10"),
                "horizon": "swing",
                "asset_classes": ["fx", "metal"],
                "risk_per_trade_pct": Decimal("1.5"),
                "max_concurrent_positions": 2,
                "daily_loss_stop_pct": Decimal("8.0"),
                "max_drawdown_halt_pct": Decimal("40.0"),
                "max_gross_leverage": Decimal("20.0"),
                "limits": {
                    "horizon": "swing",
                    "max_overnight_positions": 2,
                    "max_weekend_exposure_pct": Decimal("25.0"),
                    "max_swap_cost_pct_of_expected_edge": Decimal("30.0"),
                    "gap_risk_multiple": Decimal("5.0"),
                    "earnings_blackout_days": 0,
                },
            },
        },
        "firm": {
            "max_total_drawdown_halt_pct": Decimal("10.0"),
            "max_aggregate_open_risk_pct": Decimal("4.0"),
            "max_correlated_cluster_risk_pct": Decimal("1.0"),
            "max_single_instrument_risk_pct": Decimal("0.7"),
            "max_gross_leverage": Decimal("8.0"),
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
            Horizon.SCALP: {
                "max_tick_age_seconds": Decimal("2"),
                "max_spread_multiple_of_median": Decimal("3.0"),
                "max_clock_drift_ms": 500,
                "reconciliation_mismatch": True,
                "slippage_breach_sigma": Decimal("3.0"),
            },
            Horizon.SWING: {
                "max_tick_age_seconds": Decimal("30"),
                "max_spread_multiple_of_median": Decimal("3.0"),
                "max_clock_drift_ms": 500,
                "reconciliation_mismatch": True,
                "slippage_breach_sigma": Decimal("3.0"),
            },
        },
    }


def test_checked_in_constitution_matches_spec() -> None:
    model = parse_constitution_yaml(Path("config/risk_constitution.yaml").read_bytes())

    assert model.version == 1
    assert model.signature_required is True

    fx_scalp = model.books["fx_scalp"]
    assert fx_scalp.capital_fraction == Decimal("0.30")
    assert fx_scalp.horizon is Horizon.SCALP
    assert fx_scalp.asset_classes == (AssetClass.FX, AssetClass.METAL)
    assert fx_scalp.risk_per_trade_pct == Decimal("0.25")
    assert fx_scalp.max_concurrent_positions == 3
    assert fx_scalp.daily_loss_stop_pct == Decimal("1.5")
    assert fx_scalp.max_drawdown_halt_pct == Decimal("6.0")
    assert fx_scalp.max_gross_leverage == Decimal("10.0")
    assert fx_scalp.limits.horizon is Horizon.SCALP
    assert fx_scalp.limits.max_orders_per_minute == 15
    assert fx_scalp.limits.max_spread_multiple_at_entry == Decimal("1.5")
    assert fx_scalp.limits.min_expected_edge_after_cost_bps == Decimal("2.0")
    assert fx_scalp.limits.max_position_duration_seconds == 300
    assert fx_scalp.limits.flat_by_session_close is True

    fx_swing = model.books["fx_swing"]
    assert fx_swing.capital_fraction == Decimal("0.45")
    assert fx_swing.horizon is Horizon.SWING
    assert fx_swing.asset_classes == (AssetClass.FX, AssetClass.METAL)
    assert fx_swing.limits.max_overnight_positions == 6
    assert fx_swing.limits.max_weekend_exposure_pct == Decimal("15.0")
    assert fx_swing.limits.max_swap_cost_pct_of_expected_edge == Decimal("20.0")
    assert fx_swing.limits.gap_risk_multiple == Decimal("3.0")
    assert fx_swing.limits.earnings_blackout_days == 0

    equity_swing = model.books["equity_swing"]
    assert equity_swing.capital_fraction == Decimal("0.15")
    assert equity_swing.horizon is Horizon.SWING
    assert equity_swing.asset_classes == (AssetClass.EQUITY_CFD,)
    assert equity_swing.limits.earnings_blackout_days == 2

    sleeve = model.books["sleeve"]
    assert sleeve.capital_fraction == Decimal("0.10")
    assert sleeve.horizon is Horizon.SWING
    assert sleeve.asset_classes == (AssetClass.FX, AssetClass.METAL)
    assert sleeve.max_gross_leverage == Decimal("20.0")

    assert model.firm.max_total_drawdown_halt_pct == Decimal("10.0")
    assert model.firm.max_aggregate_open_risk_pct == Decimal("4.0")
    assert model.firm.max_correlated_cluster_risk_pct == Decimal("1.0")
    assert model.firm.max_single_instrument_risk_pct == Decimal("0.7")
    assert model.firm.max_gross_leverage == Decimal("8.0")
    assert model.firm.max_orders_per_minute == 30
    assert model.firm.max_consecutive_rejects == 5
    assert model.prohibitions.martingale_sizing == "forbidden"
    assert model.prohibitions.averaging_into_losers == "forbidden_unless_declared_in_strategy_spec"
    assert model.prohibitions.stop_removal == "forbidden"
    assert model.prohibitions.stop_widening == "forbidden"
    assert model.prohibitions.leverage_increase_after_loss == "forbidden"
    assert model.prohibitions.trading_without_protective_stop == "forbidden"
    assert model.safe_mode_triggers[Horizon.SCALP].max_tick_age_seconds == Decimal("2")
    assert model.safe_mode_triggers[Horizon.SWING].max_tick_age_seconds == Decimal("30")
    assert model.safe_mode_triggers[Horizon.SCALP].max_spread_multiple_of_median == Decimal("3.0")
    assert model.safe_mode_triggers[Horizon.SCALP].max_clock_drift_ms == 500
    assert model.safe_mode_triggers[Horizon.SCALP].reconciliation_mismatch is True
    assert model.safe_mode_triggers[Horizon.SCALP].slippage_breach_sigma == Decimal("3.0")


def test_books_may_be_declared_freely() -> None:
    constitution = parse_constitution_yaml(CONSTITUTION_BYTES)
    assert set(constitution.books) == {"fx_scalp", "fx_swing", "equity_swing", "sleeve"}


def test_safe_mode_triggers_are_declared_per_horizon() -> None:
    triggers = parse_constitution_yaml(CONSTITUTION_BYTES).safe_mode_triggers
    assert set(triggers) == {Horizon.SCALP, Horizon.SWING}
    assert (
        triggers[Horizon.SCALP].max_tick_age_seconds < triggers[Horizon.SWING].max_tick_age_seconds
    )


def test_every_declared_book_horizon_has_safe_mode_triggers() -> None:
    constitution = parse_constitution_yaml(CONSTITUTION_BYTES)
    declared = {book.horizon for book in constitution.books.values()}
    assert declared <= set(constitution.safe_mode_triggers)


def test_missing_horizon_safe_mode_triggers_is_rejected() -> None:
    """fx_swing, equity_swing, and sleeve are all swing-horizon books.

    Removing the ``swing:`` safe-mode-trigger block (the last block in the
    file) must fail closed rather than silently leaving those three books
    with no safe-mode coverage. This is a truncation, not a key rename: a
    rename would instead fail earlier and for a different reason, in the
    ``Horizon(key)`` conversion, so it would not exercise
    ``every_book_horizon_has_triggers`` at all.
    """

    marker = b"\n  swing:\n"
    assert CONSTITUTION_BYTES.count(marker) == 1
    source = CONSTITUTION_BYTES[: CONSTITUTION_BYTES.index(marker)] + b"\n"

    with pytest.raises(ConfigurationError):
        parse_constitution_yaml(source)


def test_capital_fractions_must_sum_to_exactly_one() -> None:
    source = CONSTITUTION_BYTES.replace(b"capital_fraction: 0.30", b"capital_fraction: 0.31")
    with pytest.raises(ConfigurationError):
        parse_constitution_yaml(source)


def test_a_scalp_book_carries_scalp_limits() -> None:
    book = parse_constitution_yaml(CONSTITUTION_BYTES).books["fx_scalp"]
    assert book.horizon is Horizon.SCALP
    assert book.limits.max_orders_per_minute > 0
    assert book.limits.flat_by_session_close is True


def test_a_swing_book_carries_swing_limits() -> None:
    book = parse_constitution_yaml(CONSTITUTION_BYTES).books["fx_swing"]
    assert book.horizon is Horizon.SWING
    assert book.limits.max_weekend_exposure_pct > 0


def test_horizon_limits_do_not_cross_apply() -> None:
    """A swing limit silently applied to a scalp book is the failure this prevents."""

    scalp = parse_constitution_yaml(CONSTITUTION_BYTES).books["fx_scalp"].limits
    assert not hasattr(scalp, "max_weekend_exposure_pct")


def test_a_book_declares_which_asset_classes_it_may_trade() -> None:
    book = parse_constitution_yaml(CONSTITUTION_BYTES).books["equity_swing"]
    assert book.asset_classes == (AssetClass.EQUITY_CFD,)


def test_books_remain_frozen_and_closed() -> None:
    constitution = parse_constitution_yaml(CONSTITUTION_BYTES)
    with pytest.raises(ValidationError):
        constitution.books["fx_scalp"].capital_fraction = Decimal("0.5")  # type: ignore[misc]


def test_a_books_limits_must_match_its_declared_horizon(valid_data: dict[str, object]) -> None:
    """A book's own ``horizon`` and its ``limits.horizon`` must agree.

    The nested ``limits`` block is left as a fully valid ``ScalpLimits`` so
    validation reaches the cross-field check rather than failing earlier on
    the discriminated union's own shape.
    """

    books = valid_data["books"]
    assert isinstance(books, dict)
    fx_scalp = books["fx_scalp"]
    assert isinstance(fx_scalp, dict)
    fx_scalp["horizon"] = "swing"

    with pytest.raises(ValidationError, match="horizon limits must match"):
        Constitution.model_validate(valid_data)


def test_capital_fractions_must_sum_exactly_to_one(valid_data: dict[str, object]) -> None:
    books = valid_data["books"]
    assert isinstance(books, dict)
    sleeve = books["sleeve"]
    assert isinstance(sleeve, dict)
    sleeve["capital_fraction"] = Decimal("0.11")

    with pytest.raises(ValidationError, match="sum exactly to 1"):
        Constitution.model_validate(valid_data)


def test_requires_at_least_one_book(valid_data: dict[str, object]) -> None:
    """``Books`` no longer pins fixed keys; a mapping still may not be empty.

    This replaces the old ``test_requires_both_books``: with books now a freely
    declared ``Mapping[BookId, BookLimits]`` there is no fixed pair of book
    names to require. The equivalent invariant is that at least one book must
    be declared, enforced by ``Field(min_length=1)``.
    """

    valid_data["books"] = {}

    with pytest.raises(ValidationError, match="at least 1 item"):
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
        ("safe_mode_triggers", Horizon.SCALP, "reconciliation_mismatch"),
    ],
)
def test_mandatory_flags_reject_numeric_true_aliases(
    valid_data: dict[str, object], field_path: tuple[str | Horizon, ...], alias: object
) -> None:
    target: dict[str, object] = valid_data
    for key in field_path[:-1]:
        nested = target[key]
        assert isinstance(nested, dict)
        target = nested
    target[field_path[-1]] = alias

    with pytest.raises(ValidationError, match="valid boolean"):
        Constitution.model_validate(valid_data)


@pytest.mark.parametrize("yaml_scalar", ["1", '"true"'])
def test_flat_by_session_close_rejects_non_boolean_true_aliases(yaml_scalar: str) -> None:
    """A third mandatory-true flag must not regress the same hole twice-fixed elsewhere.

    ``signature_required`` and ``reconciliation_mismatch`` are both guarded against
    numeric/string aliases of ``True``; ``ScalpLimits.flat_by_session_close`` carries
    the identical guard and must reject the same aliases end to end through
    ``parse_constitution_yaml``.
    """

    source = CONSTITUTION_BYTES.replace(
        b"flat_by_session_close: true", f"flat_by_session_close: {yaml_scalar}".encode()
    )

    with pytest.raises(ConfigurationError):
        parse_constitution_yaml(source)


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
    fx_scalp = books["fx_scalp"]
    assert isinstance(fx_scalp, dict)
    fx_scalp["unapproved_limit"] = Decimal("1")

    with pytest.raises(ValidationError, match="Extra inputs"):
        Constitution.model_validate(valid_data)


def test_rejects_negative_limits(valid_data: dict[str, object]) -> None:
    firm = valid_data["firm"]
    assert isinstance(firm, dict)
    firm["max_single_instrument_risk_pct"] = Decimal("-0.7")

    with pytest.raises(ValidationError, match="greater than 0"):
        Constitution.model_validate(valid_data)


@pytest.mark.parametrize(
    "field_path",
    [
        ("books", "fx_scalp", "risk_per_trade_pct"),
        ("books", "fx_scalp", "daily_loss_stop_pct"),
        ("books", "fx_scalp", "max_drawdown_halt_pct"),
        ("books", "fx_swing", "limits", "max_weekend_exposure_pct"),
        ("books", "fx_swing", "limits", "max_swap_cost_pct_of_expected_edge"),
        ("firm", "max_total_drawdown_halt_pct"),
        ("firm", "max_aggregate_open_risk_pct"),
        ("firm", "max_correlated_cluster_risk_pct"),
        ("firm", "max_single_instrument_risk_pct"),
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
    fx_scalp = books["fx_scalp"]
    assert isinstance(fx_scalp, dict)
    fx_scalp["risk_per_trade_pct"] = Decimal("100")

    model = Constitution.model_validate(valid_data)
    assert model.books["fx_scalp"].risk_per_trade_pct == Decimal("100")


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


def test_yaml_float_scalars_remain_decimal() -> None:
    model = parse_constitution_yaml(CONSTITUTION_BYTES)

    assert model.books["fx_scalp"].capital_fraction == Decimal("0.30")
    assert isinstance(model.books["fx_scalp"].capital_fraction, Decimal)


@pytest.mark.parametrize("scalar", ["1:2.3", ".nan", ".inf"])
def test_yaml_float_conversion_failures_are_redacted(scalar: str) -> None:
    source = CONSTITUTION_BYTES.replace(
        b"capital_fraction: 0.30", f"capital_fraction: {scalar}".encode()
    )

    with pytest.raises(ConfigurationError) as error:
        parse_constitution_yaml(source)

    assert str(error.value) == "configuration invalid"
    assert scalar not in str(error.value)
