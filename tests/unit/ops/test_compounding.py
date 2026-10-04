"""The compounding report and every refusal it owes.

Bundles are real engine runs at the protocol's baseline, built with the
scenario tests' own builders. A tamper goes through ``_rebuild``, so what
reaches the report is a document the models accept whole and only the report can
refuse.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from typing import Any

import pytest

from tests.unit.ops.test_scenarios import (
    BARS,
    FOREIGN_TRIAL_ID,
    TAMPERED_WINDOW_END,
    _another_trial,
    _bundle_at,
    _costs,
    _ending_at,
    _moving,
    _protocol,
    _rebuild,
    _trial_id,
)
from trading_house.core.errors import ScenarioEvidenceError
from trading_house.ops.compounding import (
    REPLAY_FIELDS,
    CompoundingReport,
    compounding_report,
    refuse_unfaithful,
    replay_inputs,
)
from trading_house.research.backtest.result import BacktestResult
from trading_house.research.backtest.sizing import SizingMode
from trading_house.research.canonical import canonical_sha256
from trading_house.research.evidence import EvidenceBundle
from trading_house.research.trial_ledger import TrialProtocol

ONE = Decimal(1)


def _pair(
    protocol: TrialProtocol, sizing: SizingMode, change: Callable[[dict[str, Any]], None] | None
) -> tuple[str, EvidenceBundle]:
    bundle = _bundle_at(protocol, BARS, ONE, trial_id=_trial_id(protocol), sizing=sizing)
    if change is not None:
        bundle = _rebuild(bundle, change)
    return canonical_sha256(bundle), bundle


def _report(
    *,
    constant: Callable[[dict[str, Any]], None] | None = None,
    compounding: Callable[[dict[str, Any]], None] | None = None,
    protocol: TrialProtocol | None = None,
    trial_id: str | None = None,
    swap_sizing: bool = False,
) -> CompoundingReport:
    protocol = protocol or _protocol()
    a = _pair(protocol, SizingMode.CONSTANT_NOTIONAL, constant)
    b = _pair(protocol, SizingMode.COMPOUNDING, compounding)
    if swap_sizing:
        a, b = b, a
    return compounding_report(
        trial_id=trial_id if trial_id is not None else _trial_id(protocol),
        protocol=protocol,
        constant=a,
        compounding=b,
    )


def _cause(**kwargs: Any) -> str:
    with pytest.raises(ScenarioEvidenceError) as refusal:
        _report(**kwargs)
    return str(refusal.value.__cause__)


def _final_equity_moved_by(delta: str) -> Callable[[dict[str, Any]], None]:
    """The last equity observation moved by ``delta``, as one open position's unrealized
    PnL so it still reconciles to firm equity plus realized plus unrealized. A non-flat
    run owes no equality with the result's net PnL, so the model accepts it."""

    def change(payload: dict[str, Any]) -> None:
        observations = payload["mark_to_market"]["observations"]
        last = observations[-1]
        observations[-1] = {
            **last,
            "open_positions": 1,
            "unrealized_pnl": str(Decimal(last["unrealized_pnl"]) + Decimal(delta)),
            "equity": str(Decimal(last["equity"]) + Decimal(delta)),
        }

    return change


def test_the_report_carries_both_runs_and_the_signed_difference() -> None:
    report = _report(compounding=_final_equity_moved_by("250"))
    # 448.8840 before Phase 10; the signed gross-leverage cap now sizes this
    # ramp's one trade, so it earns less.
    assert report.constant_notional.final_equity == Decimal("100271.7280")
    assert report.compounding.final_equity == Decimal("100521.7280")
    assert report.final_equity_difference == Decimal("250")
    assert report.constant_notional.trades == 1
    assert report.constant_notional.net_pnl == Decimal("271.7280")
    assert report.constant_notional.attempt_id == "att-scenarios-1"
    assert report.spec_sha256 == canonical_sha256(_protocol().candidates[0])


def test_the_difference_is_negative_when_compounding_ends_lower() -> None:
    assert _report(compounding=_final_equity_moved_by("-250")).final_equity_difference == Decimal(
        "-250"
    )


def test_identical_sequences_are_reported_as_such() -> None:
    assert _report().same_trade_sequence is True


def test_a_different_trade_sequence_is_reported_and_not_refused() -> None:
    report = _report(compounding=_moving("trades", None))
    assert report.same_trade_sequence is False
    assert report.compounding.trades == report.constant_notional.trades


def test_a_blank_trial_id_is_refused() -> None:
    assert "must name the trial" in _cause(trial_id=" ")


def test_a_blank_digest_is_refused() -> None:
    protocol = _protocol()
    a = _pair(protocol, SizingMode.CONSTANT_NOTIONAL, None)
    b = _pair(protocol, SizingMode.COMPOUNDING, None)
    with pytest.raises(ScenarioEvidenceError) as refusal:
        compounding_report(
            trial_id=_trial_id(protocol), protocol=protocol, constant=("", a[1]), compounding=b
        )
    assert "carries no digest" in str(refusal.value.__cause__)


def test_a_bundle_of_another_trial_is_refused() -> None:
    cause = _cause(compounding=_another_trial())
    assert FOREIGN_TRIAL_ID in cause
    assert "was reported with a scenario sealed under" in cause


def test_a_baseline_that_is_not_constant_notional_is_refused() -> None:
    assert "baseline run was sealed under compounding" in _cause(swap_sizing=True)


def test_a_rerun_that_is_not_compounding_is_refused() -> None:
    protocol = _protocol()
    a = _pair(protocol, SizingMode.CONSTANT_NOTIONAL, None)
    with pytest.raises(ScenarioEvidenceError) as refusal:
        compounding_report(
            trial_id=_trial_id(protocol), protocol=protocol, constant=a, compounding=a
        )
    assert "rerun was sealed under constant_notional" in str(refusal.value.__cause__)


def test_a_bundle_without_an_equity_series_is_refused() -> None:
    protocol = _protocol()
    a = _pair(protocol, SizingMode.CONSTANT_NOTIONAL, None)
    b = _pair(protocol, SizingMode.COMPOUNDING, None)
    # A constant-notional bundle may legitimately carry no series; model_construct
    # is the only way to hand the report one beside a compounding pair, because the
    # compounding bundle's own validator refuses the absence.
    bare = EvidenceBundle.model_construct(**{**dict(a[1]), "mark_to_market": None})
    with pytest.raises(ScenarioEvidenceError) as refusal:
        compounding_report(
            trial_id=_trial_id(protocol),
            protocol=protocol,
            constant=(a[0], bare),
            compounding=b,
        )
    assert "carries no equity series" in str(refusal.value.__cause__)


def test_a_compounding_bundle_without_an_equity_series_is_refused() -> None:
    protocol = _protocol()
    a = _pair(protocol, SizingMode.CONSTANT_NOTIONAL, None)
    b = _pair(protocol, SizingMode.COMPOUNDING, None)
    bare = EvidenceBundle.model_construct(**{**dict(b[1]), "mark_to_market": None})
    with pytest.raises(ScenarioEvidenceError) as refusal:
        compounding_report(
            trial_id=_trial_id(protocol),
            protocol=protocol,
            constant=a,
            compounding=(b[0], bare),
        )
    assert "compounding run carries no equity series" in str(refusal.value.__cause__)


@pytest.mark.parametrize("side", ["constant", "compounding"])
def test_costs_other_than_the_baseline_at_level_one_are_refused(side: str) -> None:
    cause = _cause(**{side: _costs({"commission_per_lot_per_side": Decimal("9.99")})})
    assert "the protocol's baseline at level 1 is" in cause


def test_a_stressed_run_is_refused() -> None:
    assert "baseline at level 1" in _cause(
        compounding=_costs({"stress_multiplier": Decimal("1.5")})
    )


@pytest.mark.parametrize("side", ["constant", "compounding"])
def test_a_window_other_than_the_protocols_is_refused(side: str) -> None:
    cause = _cause(**{side: _ending_at(TAMPERED_WINDOW_END)})
    assert "the protocol declares" in cause
    assert "ran [" in cause


@pytest.mark.parametrize(
    ("field", "value"), [("result.strategy_id", "another"), ("result.strategy_version", "9")]
)
def test_another_strategy_is_refused(field: str, value: str) -> None:
    assert "the protocol declares toy 1" in _cause(compounding=_moving(field, value))


def test_a_spec_digest_that_is_not_the_candidates_is_refused() -> None:
    cause = _cause(compounding=_moving("spec_sha256", "f" * 64))
    assert "the protocol's candidate trial-1 is" in cause


def test_a_trial_the_protocol_does_not_declare_is_refused() -> None:
    protocol = _protocol()
    a = _pair(protocol, SizingMode.CONSTANT_NOTIONAL, None)
    b = _pair(protocol, SizingMode.COMPOUNDING, None)
    other = _protocol(("trial-2",))
    with pytest.raises(ScenarioEvidenceError) as refusal:
        compounding_report(trial_id="trial-1", protocol=other, constant=a, compounding=b)
    assert "declares no trial-1" in str(refusal.value.__cause__)


def test_a_different_starting_equity_is_refused() -> None:
    def other_equity(payload: dict[str, Any]) -> None:
        payload["result"]["firm_equity"] = "50000"
        payload["mark_to_market"]["firm_equity"] = "50000"
        for o in payload["mark_to_market"]["observations"]:
            o["equity"] = str(
                Decimal("50000")
                + Decimal(o["cumulative_realized_pnl"])
                + Decimal(o["unrealized_pnl"])
            )

    cause = _cause(compounding=other_equity)
    assert "replay inputs" in cause
    assert "firm_equity" in cause


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("exit_policy", {"kind": "fixed_target", "r_multiple": "1.0"}),
        ("atr_period", 3),
        ("spread_window", 11),
        ("defective_bar_tolerance", "1/10"),
        ("contract_sha256", "c" * 64),
        ("constitution_sha256", "d" * 64),
        ("instrument_id", "fx.gbpusd"),
        ("timeframe", "H1"),
    ],
)
@pytest.mark.parametrize("side", ["constant", "compounding"])
def test_each_replay_input_that_differs_between_the_runs_is_refused_by_name(
    field: str, value: object, side: str
) -> None:
    """Either run may be the odd one out; the refusal names the field that moved, and
    only that one, so a field dropped from ``REPLAY_FIELDS`` leaves a case failing."""

    cause = _cause(**{side: _moving(f"result.{field}", value)})

    assert "replay inputs" in cause
    assert cause.split("replay inputs: ")[1].startswith(field)
    assert [f for f in REPLAY_FIELDS if f in cause.split("replay inputs: ")[1]] == [field]


def test_every_backtest_result_field_is_classified_as_replay_protocol_owned_or_derived() -> None:
    """A new ``BacktestResult`` field fails here until someone decides which it is: a replay
    input the two runs must share (``REPLAY_FIELDS``), a value the protocol declares, or an
    output of the run itself."""

    protocol_owned = {"start", "end", "cost_model", "strategy_id", "strategy_version"}
    derived = {
        "run_id",
        "trades",
        "rejections",
        "bars_seen",
        "defective_bars",
        "snapshots_skipped",
        "net_pnl",
    }

    assert set(BacktestResult.model_fields) == set(REPLAY_FIELDS) | protocol_owned | derived
    assert set(REPLAY_FIELDS).isdisjoint(protocol_owned | derived)
    assert set(replay_inputs(_pair(_protocol(), SizingMode.COMPOUNDING, None)[1].result)) == set(
        REPLAY_FIELDS
    )


def test_a_run_that_ends_holding_a_position_says_so() -> None:
    flat = _report()
    open_ = _report(compounding=_final_equity_moved_by("250"))

    assert flat.constant_notional.ends_flat is True
    assert flat.compounding.ends_flat is True
    assert open_.constant_notional.ends_flat is True
    assert open_.compounding.ends_flat is False


def test_a_different_number_of_bars_is_refused() -> None:
    assert "the runs read" in _cause(compounding=_moving("result.bars_seen", 1))


def test_a_request_declares_the_replay_inputs_its_run_then_records() -> None:
    """``request_replay_inputs`` is what the pre-flight expects and ``replay_inputs`` is what
    the sealed run holds; a real run is the only honest proof that all nine agree, since
    the engine derives each from a different source (request, contract, constitution)."""

    from tests.unit.ops.test_scenarios import (
        ATR_PERIOD,
        HOLDING_SECONDS,
        SPREAD_WINDOW,
        STRATEGY_HORIZON_SECONDS,
        FakeBarReader,
        ToyStrategy,
        _contract,
        _loaded_constitution,
    )
    from trading_house.marketdata.models import Timeframe
    from trading_house.ops.backtest import build_backtester
    from trading_house.ops.compounding import request_replay_inputs
    from trading_house.research.backtest.engine import BacktestRequest

    protocol = _protocol()
    contract, constitution = _contract(), _loaded_constitution()
    request = BacktestRequest(
        strategy=ToyStrategy(
            horizon_seconds=STRATEGY_HORIZON_SECONDS, max_holding_seconds=HOLDING_SECONDS
        ),
        instrument_id="fx.eurusd",
        timeframe=Timeframe.M15,
        start=BARS[0].event_time,
        end=BARS[-1].event_time,
        firm_equity=Decimal("90000"),
        cost_model=protocol.costs.baseline,
        atr_period=ATR_PERIOD,
        spread_window=SPREAD_WINDOW,
        defective_bar_tolerance=Decimal("0.1"),
    )

    outcome = build_backtester(
        bars=FakeBarReader(BARS), contract=contract, constitution=constitution
    ).run(request)

    assert request_replay_inputs(
        request,
        contract_sha256=contract.digest(),
        constitution_sha256=constitution.constitution_sha256,
    ) == replay_inputs(outcome.result)
    assert replay_inputs(outcome.result)["firm_equity"] == Decimal("90000")


# --- Phase 8E, E-7: a bundle that carries a dataset digest carries the declared one -------------


def test_a_bundle_sealed_on_another_dataset_than_the_declared_one_is_refused() -> None:
    protocol = _protocol()
    trial = _trial_id(protocol)
    declared = protocol.data.dataset_sha256
    own = _bundle_at(protocol, BARS, ONE, trial_id=trial)
    other = _bundle_at(protocol, BARS, ONE, trial_id=trial, dataset_sha256="9" * 64)

    assert own.provenance.dataset_sha256 == declared
    refuse_unfaithful(trial, protocol, own)
    with pytest.raises(ScenarioEvidenceError) as excinfo:
        refuse_unfaithful(trial, protocol, other)

    assert str(excinfo.value) == "sealed scenarios do not match the declared cost grid"
    assert "9" * 64 in str(excinfo.value.__cause__)
    assert declared in str(excinfo.value.__cause__)


def test_a_bundle_carrying_no_dataset_digest_is_not_refused_by_the_faithfulness_check() -> None:
    """Older bundles exist; ``decide`` handles them with the blocking reason."""

    protocol = _protocol()
    trial = _trial_id(protocol)
    bare = _bundle_at(protocol, BARS, ONE, trial_id=trial, dataset_sha256=None)

    assert bare.provenance.dataset_sha256 is None
    refuse_unfaithful(trial, protocol, bare)


def test_the_compounding_report_refuses_a_run_sealed_on_other_data() -> None:
    protocol = _protocol()
    trial = _trial_id(protocol)
    constant = _bundle_at(protocol, BARS, ONE, trial_id=trial)
    forged = _bundle_at(
        protocol,
        BARS,
        ONE,
        trial_id=trial,
        sizing=SizingMode.COMPOUNDING,
        dataset_sha256="9" * 64,
    )

    with pytest.raises(ScenarioEvidenceError):
        compounding_report(
            trial_id=trial,
            protocol=protocol,
            constant=(canonical_sha256(constant), constant),
            compounding=(canonical_sha256(forged), forged),
        )
