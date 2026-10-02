"""Phase 8D1: the policy digest, the holdout lifecycle, the nine gates and the decision.

Every expected value below is written out from the spec tables (P-5, P-7, P-8, P-11 and the
signed constants), not read back from the code under test: a boundary case names the number
the spec states and the side of it a value falls on.

The gates are exercised through ``evaluate_gates`` over a hand-built ``StatisticalEvidence``
whose every measurement passes by a wide margin, with one slot changed at a time. Gate 9 is
UNAVAILABLE for every candidate (the capacity diagnostic has one status), so no input can
make all nine pass through the real evaluator; the decision rules are therefore also driven
directly with synthetic gate outcomes.
"""

from __future__ import annotations

import ast
import hashlib
import json
from datetime import UTC, date, datetime
from itertools import count
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import pytest
from pydantic import ValidationError

from tests.unit.ops.test_scenarios import _preregistered_event, _protocol
from trading_house.core.errors import PromotionRefusedError
from trading_house.research import promotion
from trading_house.research.canonical import DOMAIN_SEPARATOR, canonical_sha256
from trading_house.research.promotion import (
    GATE_NAMES,
    REASON_COST_NOT_ATTRIBUTED,
    REASON_NEGATIVE_EXPECTANCY,
    REASON_NO_DATASET_HASH,
    REASON_NO_LOCKED_HOLDOUT,
    REASON_NO_MARK_TO_MARKET,
    REASON_NOT_PREREGISTERED,
    Decision,
    EvidenceFacts,
    GateInputs,
    GateResult,
    GateStatus,
    HoldoutStatus,
    ValidationReport,
    blocking_reasons,
    decide,
    derive_holdout,
    evaluate_gates,
    policy_digest_input,
    policy_sha256,
)
from trading_house.research.trial_ledger import (
    CostAttributionStatus,
    EvidenceSealedPayload,
    GateDecidedPayload,
    HoldoutSpec,
    HoldoutState,
    LedgerEvent,
    LedgerEventType,
    LegacyImportedPayload,
    RegistrationState,
    ReturnSeriesBasis,
    ScopeKind,
    TrialCounters,
    TrialSpec,
)
from trading_house.research.validation.bootstrap import BootstrapResult
from trading_house.research.validation.capacity import CapacityDiagnostic, CapacityStatus
from trading_house.research.validation.coverage import CoverageResult, CpcvP5Result
from trading_house.research.validation.evidence import EvidenceDigests, StatisticalEvidence
from trading_house.research.validation.measurement import Measurement
from trading_house.research.validation.montecarlo import McResult
from trading_house.research.validation.pbo import PboResult
from trading_house.research.validation.psr import DsrResult

TRIAL = "trial-1"
NOW = datetime(2026, 9, 21, 9, 0, tzinfo=UTC)
D = "d" * 64
E = "e" * 64
SPEC = _protocol((TRIAL,)).validation

# --- the policy digest ------------------------------------------------------------------


def test_the_policy_digest_is_the_hash_of_these_exact_bytes() -> None:
    """The preimage is written out by hand: the constants of 8C3 and the protocol's spec."""

    expected_preimage = (
        '{"cpcv_p5_quantile":0.05,"drawdown_tolerance":1e-12,"dsr_minimum":0.95,'
        '"max_drawdown":0.1,"mc_policy_version":"8c-mc-1","min_oos_trades":30,'
        '"min_regimes":2,"min_wfa_folds":1,"pbo_maximum":0.5,"validation":'
        '{"bootstrap_c":"6.7","bootstrap_replicates":10000,"cpcv_folds":6,'
        '"embargo_hours":16,"primary_metric":"net_expectancy","purge_hours":16,'
        '"trial_count_rule":"conservative-selection-lotteries",'
        '"wfa_test_months":6,"wfa_train_months":24,"wfa_validation_months":6}}'
    )

    expected = hashlib.sha256(DOMAIN_SEPARATOR + expected_preimage.encode()).hexdigest()

    assert policy_sha256(SPEC) == expected
    assert policy_sha256(SPEC) == canonical_sha256(policy_digest_input(SPEC))


def test_a_legacy_trial_has_a_policy_digest_with_no_spec() -> None:
    legacy = policy_digest_input(None)

    assert legacy.validation is None
    assert policy_sha256(None) != policy_sha256(SPEC)
    assert '"validation":null' in json.dumps(
        json.loads(legacy.model_dump_json()), sort_keys=True, separators=(",", ":")
    )


@pytest.mark.parametrize(
    ("constant", "moved"),
    [
        ("DSR_MINIMUM", 0.94),
        ("PBO_MAXIMUM", 0.51),
        ("MAX_DRAWDOWN", 0.11),
        ("DRAWDOWN_TOLERANCE", 1e-9),
        ("MIN_OOS_TRADES", 29),
        ("MIN_REGIMES", 1),
        ("CPCV_P5_QUANTILE", 0.06),
        ("MIN_WFA_FOLDS", 2),
        ("MC_POLICY_VERSION", "8c-mc-2"),
    ],
)
def test_moving_any_one_signed_constant_moves_the_digest(
    monkeypatch: pytest.MonkeyPatch, constant: str, moved: object
) -> None:
    before = policy_sha256(SPEC)

    monkeypatch.setattr(promotion, constant, moved)

    assert policy_sha256(SPEC) != before


@pytest.mark.parametrize(
    "change",
    [
        {"primary_metric": "other_metric"},
        {"wfa_train_months": 25},
        {"wfa_validation_months": 7},
        {"wfa_test_months": 7},
        {"purge_hours": 17},
        {"embargo_hours": 17},
        {"cpcv_folds": 7},
        {"bootstrap_replicates": 10001},
        {"bootstrap_c": "6.8"},
        {"trial_count_rule": "other-rule"},
    ],
)
def test_moving_any_one_spec_field_moves_the_digest(change: dict[str, Any]) -> None:
    from decimal import Decimal

    update = {k: Decimal(v) if k == "bootstrap_c" else v for k, v in change.items()}

    assert policy_sha256(SPEC.model_copy(update=update)) != policy_sha256(SPEC)


def test_the_digest_input_holds_every_signed_constant_and_nothing_else() -> None:
    assert set(promotion.PolicyDigestInput.model_fields) == {
        "dsr_minimum",
        "pbo_maximum",
        "max_drawdown",
        "drawdown_tolerance",
        "min_oos_trades",
        "min_regimes",
        "cpcv_p5_quantile",
        "min_wfa_folds",
        "mc_policy_version",
        "validation",
    }


def test_promotion_is_pure_it_imports_no_io_ledger_store_or_clock() -> None:
    """P-1: the module reads nothing. Checked on its import graph and on its calls."""

    source = Path(promotion.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module)
    forbidden = {
        "os",
        "pathlib",
        "psycopg",
        "time",
        "datetime",
        "tempfile",
        "subprocess",
        "socket",
        "random",
        "trading_house.ops",
        "trading_house.database",
        "trading_house.core.clock",
        "trading_house.research.ledger_store",
        "trading_house.research.evidence",
    }
    assert [
        name for name in imported if name in forbidden or name.startswith("trading_house.ops")
    ] == []
    assert "now(" not in source
    assert "open(" not in source


# --- the holdout lifecycle ---------------------------------------------------------------

_ids = count()


def _event(
    payload: Any,
    event_type: LedgerEventType,
    *,
    trial_id: str | None = TRIAL,
    legacy: bool = False,
) -> LedgerEvent:
    return LedgerEvent(
        event_id=uuid5(NAMESPACE_URL, f"test-event-{next(_ids)}"),
        scope_kind=ScopeKind.TRIAL,
        scope_id="scope",
        event_type=event_type,
        trial_id=trial_id,
        spec_sha256=D,
        occurred_at=NOW,
        payload=payload,
        legacy=legacy,
        legacy_reason="late import" if legacy else None,
    )


def _registered(state: HoldoutState, *, trial_id: str = TRIAL, version: str = "1") -> LedgerEvent:
    protocol = _protocol((trial_id,)).model_copy(update={"protocol_version": version})
    if state in {HoldoutState.NOT_DEFINED}:
        holdout = HoldoutSpec(state=state)
    else:
        holdout = HoldoutSpec(
            state=state,
            start=datetime(2026, 1, 1, tzinfo=UTC),
            end=datetime(2026, 2, 1, tzinfo=UTC),
            dataset_sha256="9" * 64,
        )
    return _preregistered_event(protocol.model_copy(update={"holdout": holdout}))


def _legacy(trial_id: str = TRIAL) -> LedgerEvent:
    return _event(
        LegacyImportedPayload(
            event_type=LedgerEventType.LEGACY_IMPORTED,
            trial=TrialSpec(
                trial_id=trial_id, spec_id="legacy", rationale="late", parameter_space=()
            ),
            evidence_sha256=D,
            source_result_sha256=E,
            legacy_reason="late import",
        ),
        LedgerEventType.LEGACY_IMPORTED,
        trial_id=trial_id,
        legacy=True,
    )


def _sealed(digest: str, trial_id: str = TRIAL) -> LedgerEvent:
    return _event(
        EvidenceSealedPayload(
            event_type=LedgerEventType.EVIDENCE_SEALED, attempt_id="a", evidence_sha256=digest
        ),
        LedgerEventType.EVIDENCE_SEALED,
        trial_id=trial_id,
    )


def _decided(trial_id: str = TRIAL) -> LedgerEvent:
    return _event(
        GateDecidedPayload(
            event_type=LedgerEventType.GATE_DECIDED, decision="REJECTED", report_sha256=E
        ),
        LedgerEventType.GATE_DECIDED,
        trial_id=trial_id,
    )


O1, O2, W = "1" * 64, "2" * 64, "3" * 64
OPENED = HoldoutState.OPENED
STATES = {
    O1: HoldoutState.OPENED,
    O2: HoldoutState.OPENED,
    W: HoldoutState.LOCKED,
    "4" * 64: HoldoutState.CONSUMED,
    "5" * 64: HoldoutState.CONTAMINATED,
    "6" * 64: HoldoutState.NOT_DEFINED,
}


def _derive(*events: LedgerEvent, trial_id: str = TRIAL) -> HoldoutStatus:
    return derive_holdout(trial_id, events, STATES)


def test_legacy_evidence_is_contaminated_whatever_else_the_chain_says() -> None:
    status = _derive(_registered(HoldoutState.LOCKED), _legacy())

    assert status.state is HoldoutState.CONTAMINATED
    assert "legacy" in status.reason


def test_a_trial_no_registration_declares_has_no_holdout() -> None:
    assert _derive().state is HoldoutState.NOT_DEFINED
    assert _derive(_registered(HoldoutState.LOCKED, trial_id="other")).state is (
        HoldoutState.NOT_DEFINED
    )


@pytest.mark.parametrize(
    ("registered", "derived"),
    [
        (HoldoutState.NOT_DEFINED, HoldoutState.NOT_DEFINED),
        (HoldoutState.LOCKED, HoldoutState.LOCKED),
        (HoldoutState.OPENED, HoldoutState.CONTAMINATED),
        (HoldoutState.CONSUMED, HoldoutState.CONTAMINATED),
        (HoldoutState.CONTAMINATED, HoldoutState.CONTAMINATED),
    ],
)
def test_the_registered_holdout_state_gives_the_starting_state(
    registered: HoldoutState, derived: HoldoutState
) -> None:
    status = _derive(_registered(registered))

    assert status.state is derived
    assert status.opened_bundle_count == 0


def test_one_opened_bundle_opens_the_holdout() -> None:
    status = _derive(_registered(HoldoutState.LOCKED), _sealed(O1))

    assert (status.state, status.opened_bundle_count) == (HoldoutState.OPENED, 1)


def test_a_bundle_sealed_after_the_holdout_was_consumed_also_counts_as_an_opening() -> None:
    status = _derive(_registered(HoldoutState.LOCKED), _sealed("4" * 64))

    assert status.state is HoldoutState.OPENED


def test_a_decision_after_the_opening_consumes_the_holdout() -> None:
    status = _derive(_registered(HoldoutState.LOCKED), _sealed(O1), _decided())

    assert (status.state, status.opened_bundle_count) == (HoldoutState.CONSUMED, 1)


def test_a_decision_before_the_opening_does_not_consume_it() -> None:
    status = _derive(_registered(HoldoutState.LOCKED), _decided(), _sealed(O1))

    assert status.state is HoldoutState.OPENED


def test_a_decision_with_no_opening_leaves_a_locked_holdout_locked() -> None:
    assert _derive(_registered(HoldoutState.LOCKED), _decided()).state is HoldoutState.LOCKED


def test_another_trials_opening_and_decision_do_not_touch_this_trial() -> None:
    status = _derive(
        _registered(HoldoutState.LOCKED),
        _sealed(O1, trial_id="other"),
        _decided(trial_id="other"),
        _legacy(trial_id="other"),
    )

    assert status.state is HoldoutState.LOCKED


def test_a_second_opened_bundle_contaminates_and_it_never_heals() -> None:
    twice = _derive(_registered(HoldoutState.LOCKED), _sealed(O1), _sealed(O2))
    after_consumption = _derive(
        _registered(HoldoutState.LOCKED), _sealed(O1), _decided(), _sealed(O2)
    )

    assert (twice.state, twice.opened_bundle_count) == (HoldoutState.CONTAMINATED, 2)
    assert after_consumption.state is HoldoutState.CONTAMINATED


def test_sealing_the_same_opened_bundle_twice_is_not_a_second_opening() -> None:
    status = _derive(_registered(HoldoutState.LOCKED), _sealed(O1), _sealed(O1))

    assert (status.state, status.opened_bundle_count) == (HoldoutState.OPENED, 1)


def test_an_opening_with_no_locked_holdout_is_an_inspection_before_lock() -> None:
    status = _derive(_registered(HoldoutState.NOT_DEFINED), _sealed(O1))

    assert status.state is HoldoutState.CONTAMINATED


def test_a_bundle_that_says_its_holdout_was_contaminated_contaminates() -> None:
    status = _derive(_registered(HoldoutState.LOCKED), _sealed("5" * 64))

    assert status.state is HoldoutState.CONTAMINATED


@pytest.mark.parametrize("digest", [W, "6" * 64])
def test_bundles_run_on_the_research_window_leave_the_holdout_alone(digest: str) -> None:
    assert _derive(_registered(HoldoutState.LOCKED), _sealed(digest)).state is (HoldoutState.LOCKED)


def test_two_registrations_that_are_not_one_protocol_contaminate() -> None:
    status = _derive(
        _registered(HoldoutState.LOCKED), _registered(HoldoutState.LOCKED, version="2")
    )

    assert status.state is HoldoutState.CONTAMINATED


def test_the_same_registration_twice_is_one_registration() -> None:
    locked = _registered(HoldoutState.LOCKED)

    assert _derive(locked, locked).state is HoldoutState.LOCKED


def test_sealed_evidence_with_no_known_holdout_state_is_refused_not_guessed() -> None:
    with pytest.raises(PromotionRefusedError):
        derive_holdout(TRIAL, (_registered(HoldoutState.LOCKED), _sealed("7" * 64)), STATES)


# --- the gates ----------------------------------------------------------------------------


def _m(name: str, value: float | None, *, digest: str = D, mtm: bool = True) -> Measurement:
    if value is None:
        return Measurement.undefined(
            name,
            f"{name} could not be measured",
            evidence_sha256=(digest,),
            basis_is_mark_to_market=mtm,
        )
    return Measurement.defined(name, value, evidence_sha256=(digest,), basis_is_mark_to_market=mtm)


def _evidence(trial_id: str = TRIAL, *, paths_differ: bool = True) -> StatisticalEvidence:
    """Every gate passes by a wide margin; each test then moves one slot."""

    return StatisticalEvidence(
        trial_id=trial_id,
        spec_sha256=D,
        attempt_id="attempt-1",
        basis_is_mark_to_market=True,
        evidence=EvidenceDigests(
            baseline=D, compounding=E, pbo_candidates=(D,), chain_head="h" * 64
        ),
        counters=TrialCounters(audit_attempts=1, selection_lotteries=1, effective_specifications=1),
        first_day=date(2026, 1, 1),
        last_day=date(2026, 3, 1),
        series_days=60,
        purge_days=1,
        embargo_days=1,
        cpcv_folds=6,
        wfa_folds=_m("wfa_folds", 3.0),
        psr=_m("psr", 0.99),
        sharpe_annualised=_m("sharpe_annualised", 1.0),
        dsr=DsrResult(
            dsr=_m("dsr", 0.99),
            selection_lotteries=1,
            effective_specifications=1,
            trials=1,
            expected_max_z=None,
            horizon_days=1,
            chain_head_sha256="h" * 64,
            standard_error=None,
            cross_section=None,
            cross_section_count=0,
            dispersion_used=None,
        ),
        pbo=PboResult(pbo=_m("pbo", 0.2), splits=(), candidates=(TRIAL,), excluded=()),
        bootstrap=BootstrapResult(
            seed=1,
            policy_version="8c-sb-1",
            block_length=2,
            replicates=100,
            mean=0.001,
            p5=0.0005,
            p95=0.002,
            bootstrap_lower_bound=_m("bootstrap_lower_bound", 0.0005),
        ),
        monte_carlo=McResult(
            seed=1,
            policy_version="8c-mc-1",
            block_length=2,
            replicates=100,
            horizon_days=60,
            halt_level=0.1,
            p_halt=_m("p_halt", 0.1),
            p_loss=_m("p_loss", 0.2),
        ),
        max_drawdown_baseline=_m("max_drawdown_baseline", 0.05),
        max_drawdown_cpcv_paths=(
            _m("max_drawdown_cpcv_path_0", 0.05),
            _m("max_drawdown_cpcv_path_1", 0.06),
        ),
        max_drawdown_compounding=_m("max_drawdown_compounding", 0.07),
        cpcv_p5=CpcvP5Result(
            p5=_m("cpcv_p5", 5.0),
            quantile=0.05,
            rank=1,
            paths_differ=paths_differ,
            closed_trades=40,
            all_trades_kept=not paths_differ,
            paths=(),
        ),
        coverage=CoverageResult(
            oos_trades=_m("oos_trades", 40.0),
            regimes_represented=_m("regimes_represented", 2.0),
            declared_labels=("london", "new_york"),
            oos_path_index=0,
            closed_trades=40,
            all_trades_kept=False,
        ),
        scenario_expectancy=(),
        capacity=CapacityDiagnostic(status=CapacityStatus.UNAVAILABLE, reason="no model"),
    )


def _facts(**change: Any) -> EvidenceFacts:
    base: dict[str, Any] = {
        "registration_state": RegistrationState.PROSPECTIVE,
        "dataset_sha256_present": True,
        "return_series_basis": ReturnSeriesBasis.MARK_TO_MARKET,
        "cost_status": CostAttributionStatus.COMPLETE,
        "baseline_net_expectancy": 12.5,
        "baseline_evidence_sha256": D,
    }
    return EvidenceFacts(**{**base, **change})


def _holdout(state: HoldoutState = HoldoutState.LOCKED) -> HoldoutStatus:
    return HoldoutStatus(state=state, reason="derived for the test", opened_bundle_count=0)


def _inputs(
    evidence: StatisticalEvidence | None = None,
    *,
    holdout: HoldoutStatus | None = None,
    h15: Measurement | None = None,
    h20: Measurement | None = None,
    spec: Any = SPEC,
    claimed: str | None = None,
) -> GateInputs:
    return GateInputs(
        trial_id=TRIAL,
        policy_sha256=claimed if claimed is not None else policy_sha256(spec),
        validation_spec=spec,
        evidence=evidence,
        holdout=holdout or _holdout(),
        holdout_expectancy_1_5=h15,
        holdout_expectancy_2_0=h20,
        facts=_facts(),
    )


def _gates(evidence: StatisticalEvidence | None, **kwargs: Any) -> dict[int, GateResult]:
    results = evaluate_gates(_inputs(evidence, **kwargs))
    assert [gate.gate for gate in results] == list(range(1, 10))
    return {gate.gate: gate for gate in results}


def test_there_are_nine_gates_named_and_ordered_by_the_spec() -> None:
    gates = _gates(_evidence())

    assert [gate.name for gate in gates.values()] == [
        "wfa_complete",
        "locked_oos_expectancy",
        "dsr_minimum",
        "pbo_maximum",
        "cpcv_p5_expectancy",
        "bootstrap_lower_bound",
        "max_drawdown",
        "oos_coverage",
        "capacity_model",
    ]
    assert list(GATE_NAMES) == list(range(1, 10))


def test_with_every_measurement_well_inside_its_limit_only_gates_two_and_nine_block() -> None:
    gates = _gates(_evidence())

    assert {n: g.status for n, g in gates.items()} == {
        1: GateStatus.PASS,
        2: GateStatus.UNAVAILABLE,  # no holdout is opened
        3: GateStatus.PASS,
        4: GateStatus.PASS,
        5: GateStatus.PASS,
        6: GateStatus.PASS,
        7: GateStatus.PASS,
        8: GateStatus.PASS,
        9: GateStatus.UNAVAILABLE,  # no capacity model can be declared
    }
    for number in (1, 3, 4, 5, 6, 7, 8):
        assert gates[number].evidence_sha256, "a PASS names its evidence"


# gate -> a function replacing that gate's one measurement by ``value`` (``None`` = undefined)


def _with_wfa(ev: StatisticalEvidence, v: float | None) -> StatisticalEvidence:
    return ev.model_copy(update={"wfa_folds": _m("wfa_folds", v)})


def _with_dsr(ev: StatisticalEvidence, v: float | None) -> StatisticalEvidence:
    return ev.model_copy(update={"dsr": ev.dsr.model_copy(update={"dsr": _m("dsr", v)})})


def _with_pbo(ev: StatisticalEvidence, v: float | None) -> StatisticalEvidence:
    return ev.model_copy(update={"pbo": ev.pbo.model_copy(update={"pbo": _m("pbo", v)})})


def _with_p5(ev: StatisticalEvidence, v: float | None) -> StatisticalEvidence:
    return ev.model_copy(update={"cpcv_p5": ev.cpcv_p5.model_copy(update={"p5": _m("cpcv_p5", v)})})


def _with_bootstrap(ev: StatisticalEvidence, v: float | None) -> StatisticalEvidence:
    return ev.model_copy(
        update={
            "bootstrap": ev.bootstrap.model_copy(
                update={"bootstrap_lower_bound": _m("bootstrap_lower_bound", v)}
            )
        }
    )


@pytest.mark.parametrize(
    ("number", "setter", "cases"),
    [
        (
            1,
            _with_wfa,
            [(1.0, "PASS"), (3.0, "PASS"), (0.0, "FAIL"), (None, "UNAVAILABLE")],
        ),
        (
            3,
            _with_dsr,
            [
                (0.95, "PASS"),
                (0.99, "PASS"),
                (0.9499999, "FAIL"),
                (0.0, "FAIL"),
                (None, "UNAVAILABLE"),
            ],
        ),
        (
            4,
            _with_pbo,
            [
                (0.5, "PASS"),
                (0.0, "PASS"),
                (0.5000001, "FAIL"),
                (1.0, "FAIL"),
                (None, "UNAVAILABLE"),
            ],
        ),
        (
            5,
            _with_p5,
            [(1e-9, "PASS"), (0.0, "FAIL"), (-3.0, "FAIL"), (None, "UNAVAILABLE")],
        ),
        (
            6,
            _with_bootstrap,
            [(1e-9, "PASS"), (0.0, "FAIL"), (-0.001, "FAIL"), (None, "UNAVAILABLE")],
        ),
    ],
)
def test_each_threshold_gate_passes_fails_and_is_unavailable_at_its_boundary(
    number: int, setter: Any, cases: list[tuple[float | None, str]]
) -> None:
    for value, status in cases:
        gate = _gates(setter(_evidence(), value))[number]

        assert gate.status is GateStatus(status), (number, value)
        if status == "UNAVAILABLE":
            assert gate.measured_value is None
        else:
            assert gate.measured_value == value


def test_each_gate_reports_the_threshold_it_compared_against() -> None:
    gates = _gates(_evidence())

    assert gates[1].threshold == 1.0
    assert gates[3].threshold == 0.95
    assert gates[4].threshold == 0.5
    assert gates[5].threshold == 0.0
    assert gates[6].threshold == 0.0
    assert gates[7].threshold == pytest.approx(0.1 - 1e-12, abs=1e-15)
    assert gates[8].threshold == 30.0


def test_the_thresholds_come_from_the_constants_in_force_not_from_the_gate() -> None:
    """Gate 3 at 0.90: below the signed 0.95, so it fails; the number is policy's, not an option."""

    gate = _gates(_with_dsr(_evidence(), 0.90))[3]

    assert gate.status is GateStatus.FAIL
    assert gate.threshold == promotion.DSR_MINIMUM


def test_gate_five_says_when_the_paths_were_identical_and_only_then() -> None:
    identical = _gates(_evidence(paths_differ=False))[5]
    different = _gates(_evidence(paths_differ=True))[5]

    assert "identical" in identical.reason
    assert "in-sample" in identical.reason
    assert "identical" not in different.reason
    assert identical.status is different.status is GateStatus.PASS


@pytest.mark.parametrize(
    ("trades", "regimes", "status", "measured", "threshold"),
    [
        (30.0, 2.0, GateStatus.PASS, 30.0, 30.0),
        (29.0, 2.0, GateStatus.FAIL, 29.0, 30.0),
        (30.0, 1.0, GateStatus.FAIL, 1.0, 2.0),
        (29.0, 1.0, GateStatus.FAIL, 29.0, 30.0),
        (None, 2.0, GateStatus.UNAVAILABLE, None, None),
        (30.0, None, GateStatus.UNAVAILABLE, None, None),
        (None, None, GateStatus.UNAVAILABLE, None, None),
        (None, 1.0, GateStatus.FAIL, 1.0, 2.0),  # a measured shortfall fails whatever is missing
        (29.0, None, GateStatus.FAIL, 29.0, 30.0),
    ],
)
def test_gate_eight_needs_thirty_trades_and_two_regimes(
    trades: float | None,
    regimes: float | None,
    status: GateStatus,
    measured: float | None,
    threshold: float | None,
) -> None:
    ev = _evidence()
    ev = ev.model_copy(
        update={
            "coverage": ev.coverage.model_copy(
                update={
                    "oos_trades": _m("oos_trades", trades),
                    "regimes_represented": _m("regimes_represented", regimes),
                }
            )
        }
    )

    gate = _gates(ev)[8]

    assert (gate.status, gate.measured_value, gate.threshold) == (status, measured, threshold)


def _drawdowns(
    ev: StatisticalEvidence,
    *,
    baseline: float | None = 0.05,
    paths: tuple[float | None, ...] = (0.05, 0.06),
    compounding: float | None = 0.07,
    p_halt: float | None = 0.1,
    p_loss: float | None = 0.2,
) -> StatisticalEvidence:
    return ev.model_copy(
        update={
            "max_drawdown_baseline": _m("max_drawdown_baseline", baseline),
            "max_drawdown_cpcv_paths": tuple(
                _m(f"max_drawdown_cpcv_path_{i}", v) for i, v in enumerate(paths)
            ),
            "max_drawdown_compounding": _m("max_drawdown_compounding", compounding),
            "monte_carlo": ev.monte_carlo.model_copy(
                update={"p_halt": _m("p_halt", p_halt), "p_loss": _m("p_loss", p_loss)}
            ),
        }
    )


@pytest.mark.parametrize(
    ("value", "status"),
    [
        (0.0, GateStatus.PASS),
        (0.0999, GateStatus.PASS),
        (0.1 - 2e-12, GateStatus.PASS),  # strictly inside the tolerance band
        (0.1 - 1e-12, GateStatus.FAIL),  # exactly MAX_DRAWDOWN less the tolerance: not below it
        (0.1 - 5e-13, GateStatus.FAIL),  # inside the band: counts as at the limit
        (0.09999999999999998, GateStatus.FAIL),  # an exact 10% fall from a flat start
        (0.1, GateStatus.FAIL),  # exactly the limit fails: strictly below is required
        (0.1000001, GateStatus.FAIL),
        (1.0, GateStatus.FAIL),
    ],
)
@pytest.mark.parametrize("slot", ["baseline", "path", "compounding"])
def test_gate_seven_passes_only_strictly_below_the_limit_less_the_tolerance_on_every_slot(
    slot: str, value: float, status: GateStatus
) -> None:
    kwargs: dict[str, Any] = {
        "baseline": {"baseline": value},
        "path": {"paths": (0.05, value)},
        "compounding": {"compounding": value},
    }[slot]

    gate = _gates(_drawdowns(_evidence(), **kwargs))[7]

    assert gate.status is status
    # the worst of the four, hand-derived from the defaults 0.05, 0.05/0.06 and 0.07
    others = {"baseline": 0.05, "path": 0.06, "compounding": 0.07}
    others.pop(slot)
    assert gate.measured_value == max(value, *others.values())


@pytest.mark.parametrize(
    "kwargs",
    [
        {"baseline": None},
        {"paths": (0.05, None)},
        {"paths": (None, None)},
        {"compounding": None},
        {"p_halt": None},
        {"p_loss": None},
        {"p_halt": None, "p_loss": None, "compounding": None},
    ],
)
def test_gate_seven_is_unavailable_when_any_drawdown_or_monte_carlo_measure_is_undefined(
    kwargs: dict[str, Any],
) -> None:
    gate = _gates(_drawdowns(_evidence(), **kwargs))[7]

    assert gate.status is GateStatus.UNAVAILABLE
    assert gate.measured_value is None
    assert "undefined" in gate.reason


def test_gate_seven_with_no_cpcv_path_drawdown_at_all_is_unavailable_not_vacuously_true() -> None:
    gate = _gates(_drawdowns(_evidence(), paths=()))[7]

    assert gate.status is GateStatus.UNAVAILABLE
    assert "no CPCV path drawdown" in gate.reason


def test_gate_seven_a_measured_breach_fails_even_when_another_measure_is_missing() -> None:
    """FAIL is a measured value on the wrong side and it wins over UNAVAILABLE; the reason
    says which it was."""

    gate = _gates(_drawdowns(_evidence(), baseline=0.2, compounding=None, p_halt=None))[7]

    assert gate.status is GateStatus.FAIL
    assert gate.measured_value == 0.2
    assert "at or beyond" in gate.reason
    assert "whatever else is missing" in gate.reason


def test_gate_seven_reports_the_worst_drawdown_across_baseline_paths_and_compounding() -> None:
    gate = _gates(_drawdowns(_evidence(), baseline=0.01, paths=(0.02, 0.08), compounding=0.03))[7]

    assert (gate.status, gate.measured_value) == (GateStatus.PASS, 0.08)


@pytest.mark.parametrize(
    "state", [HoldoutState.NOT_DEFINED, HoldoutState.LOCKED, HoldoutState.CONTAMINATED]
)
def test_gate_two_is_unavailable_unless_the_holdout_is_opened(state: HoldoutState) -> None:
    gate = _gates(_evidence(), holdout=_holdout(state), h15=_m("h15", 5.0), h20=_m("h20", 5.0))[2]

    assert gate.status is GateStatus.UNAVAILABLE
    assert state.value in gate.reason


@pytest.mark.parametrize("state", [HoldoutState.OPENED, HoldoutState.CONSUMED])
@pytest.mark.parametrize(
    ("h15", "h20", "status", "measured"),
    [
        (5.0, 3.0, GateStatus.PASS, 3.0),
        (1e-9, 4.0, GateStatus.PASS, 1e-9),
        (0.0, 4.0, GateStatus.FAIL, 0.0),
        (4.0, 0.0, GateStatus.FAIL, 0.0),
        (-1.0, 4.0, GateStatus.FAIL, -1.0),
        (4.0, -2.0, GateStatus.FAIL, -2.0),
        (None, 4.0, GateStatus.UNAVAILABLE, None),
        (4.0, None, GateStatus.UNAVAILABLE, None),
        (None, None, GateStatus.UNAVAILABLE, None),
        (None, -1.0, GateStatus.FAIL, -1.0),
        (-1.0, None, GateStatus.FAIL, -1.0),
    ],
)
def test_gate_two_needs_both_holdout_expectancies_above_zero(
    state: HoldoutState,
    h15: float | None,
    h20: float | None,
    status: GateStatus,
    measured: float | None,
) -> None:
    gate = _gates(_evidence(), holdout=_holdout(state), h15=_m("h15", h15), h20=_m("h20", h20))[2]

    assert (gate.status, gate.measured_value) == (status, measured)
    assert gate.threshold == 0.0


@pytest.mark.parametrize("missing", ["both", "first", "second"])
def test_gate_two_with_an_opened_holdout_but_an_absent_measurement_is_unavailable(
    missing: str,
) -> None:
    h15 = None if missing in {"both", "first"} else _m("h15", 5.0)
    h20 = None if missing in {"both", "second"} else _m("h20", 5.0)

    gate = _gates(_evidence(), holdout=_holdout(HoldoutState.OPENED), h15=h15, h20=h20)[2]

    assert gate.status is GateStatus.UNAVAILABLE
    assert "not both supplied" in gate.reason


def test_gate_nine_is_unavailable_for_every_candidate_and_the_capacity_enum_has_one_member() -> (
    None
):
    assert set(CapacityStatus) == {CapacityStatus.UNAVAILABLE}
    gate = _gates(_evidence())[9]

    assert gate.status is GateStatus.UNAVAILABLE
    assert gate.measured_value is None
    assert "no model" in gate.reason


def test_without_statistical_evidence_every_research_gate_is_unavailable_and_says_so() -> None:
    gates = _gates(None, holdout=_holdout(HoldoutState.CONTAMINATED))

    assert {n: g.status for n, g in gates.items()} == {
        n: GateStatus.UNAVAILABLE for n in range(1, 10)
    }
    assert [n for n, g in gates.items() if g.reason == "no statistical evidence"] == [
        1,
        3,
        4,
        5,
        6,
        7,
        8,
        9,
    ]
    assert "contaminated" in gates[2].reason


_SLOTS: list[tuple[str, int, Any]] = [
    ("wfa_folds", 1, lambda e: _with_wfa(e, None)),
    ("dsr", 3, lambda e: _with_dsr(e, None)),
    ("pbo", 4, lambda e: _with_pbo(e, None)),
    ("cpcv_p5", 5, lambda e: _with_p5(e, None)),
    ("bootstrap", 6, lambda e: _with_bootstrap(e, None)),
    ("dd_baseline", 7, lambda e: _drawdowns(e, baseline=None)),
    ("dd_path", 7, lambda e: _drawdowns(e, paths=(0.05, None))),
    ("dd_compounding", 7, lambda e: _drawdowns(e, compounding=None)),
    ("p_halt", 7, lambda e: _drawdowns(e, p_halt=None)),
    ("p_loss", 7, lambda e: _drawdowns(e, p_loss=None)),
    (
        "oos_trades",
        8,
        lambda e: e.model_copy(
            update={"coverage": e.coverage.model_copy(update={"oos_trades": _m("t", None)})}
        ),
    ),
    (
        "regimes",
        8,
        lambda e: e.model_copy(
            update={
                "coverage": e.coverage.model_copy(update={"regimes_represented": _m("r", None)})
            }
        ),
    ),
]


@pytest.mark.parametrize(("slot", "number", "undefine"), _SLOTS, ids=[s[0] for s in _SLOTS])
def test_a_measurement_replaced_by_undefined_never_reads_as_a_pass(
    slot: str, number: int, undefine: Any
) -> None:
    """Every research measurement in turn: the same inputs with the slot undefined turn that
    gate from PASS to UNAVAILABLE, and no decision can then be approval of any kind."""

    before = _gates(_evidence())[number]
    gates = _gates(undefine(_evidence()))

    assert before.status is GateStatus.PASS
    assert gates[number].status is GateStatus.UNAVAILABLE, slot
    results = tuple(gates.values())
    for state in HoldoutState:
        assert decide(results, _holdout(state), ()) is Decision.REJECTED


@pytest.mark.parametrize("slot", ["first", "second"])
def test_a_holdout_measurement_replaced_by_undefined_never_reads_as_a_pass(slot: str) -> None:
    good, bad = _m("h", 5.0), _m("h", None)
    h15, h20 = (bad, good) if slot == "first" else (good, bad)

    gate = _gates(_evidence(), holdout=_holdout(HoldoutState.OPENED), h15=h15, h20=h20)[2]

    assert gate.status is GateStatus.UNAVAILABLE


def test_a_changed_threshold_changes_the_digest_and_evaluation_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = _inputs(_evidence())
    assert evaluate_gates(inputs)  # the digest in the inputs is the one in force

    monkeypatch.setattr(promotion, "DSR_MINIMUM", 0.5)

    with pytest.raises(PromotionRefusedError) as refusal:
        evaluate_gates(inputs)
    assert "policy digest" in str(refusal.value.__cause__)
    assert str(refusal.value) == PromotionRefusedError.public_message


def test_inputs_claiming_a_digest_of_another_spec_are_refused() -> None:
    other = SPEC.model_copy(update={"cpcv_folds": 7})

    with pytest.raises(PromotionRefusedError):
        evaluate_gates(_inputs(_evidence(), claimed=policy_sha256(other)))


def test_evidence_of_another_trial_is_refused() -> None:
    with pytest.raises(PromotionRefusedError) as refusal:
        evaluate_gates(_inputs(_evidence(trial_id="trial-2")))
    assert "trial-2" in str(refusal.value.__cause__)


# --- GateResult itself --------------------------------------------------------------------


def _gate_kwargs(**change: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "gate": 3,
        "name": "dsr_minimum",
        "status": GateStatus.PASS,
        "measured_value": 0.97,
        "threshold": 0.95,
        "evidence_sha256": (D,),
        "reason": "because",
    }
    return {**base, **change}


@pytest.mark.parametrize(
    "change",
    [
        {"measured_value": None},
        {"evidence_sha256": ()},
        {"status": GateStatus.FAIL, "measured_value": None},
        {"measured_value": float("nan")},
        {"measured_value": float("inf")},
        {"gate": 0},
        {"gate": 10},
        {"reason": ""},
    ],
)
def test_a_gate_result_that_is_a_pass_or_a_fail_without_a_finite_measurement_is_refused(
    change: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError):
        GateResult(**_gate_kwargs(**change))


def test_an_unavailable_gate_may_carry_nothing_but_a_reason() -> None:
    gate = GateResult(
        **_gate_kwargs(
            status=GateStatus.UNAVAILABLE, measured_value=None, threshold=None, evidence_sha256=()
        )
    )

    assert gate.status is GateStatus.UNAVAILABLE


# --- the blocking reasons -------------------------------------------------------------------


def test_the_six_reasons_are_the_sections_8_4_words() -> None:
    assert REASON_NEGATIVE_EXPECTANCY == "negative expectancy at baseline costs"
    assert REASON_NO_LOCKED_HOLDOUT == "no locked unseen holdout"
    assert REASON_NOT_PREREGISTERED == "legacy evidence was not preregistered"
    assert REASON_NO_DATASET_HASH == "dataset-content hash is unavailable"
    assert REASON_NO_MARK_TO_MARKET == "mark-to-market returns are unavailable"
    assert REASON_COST_NOT_ATTRIBUTED == "spread and slippage cannot be separately attributed"


def test_clean_prospective_evidence_with_a_locked_holdout_has_no_reason() -> None:
    assert blocking_reasons(_facts(), _holdout(HoldoutState.LOCKED)) == ()


@pytest.mark.parametrize(
    ("facts", "holdout", "reason"),
    [
        ({"baseline_net_expectancy": -0.01}, HoldoutState.LOCKED, REASON_NEGATIVE_EXPECTANCY),
        ({}, HoldoutState.NOT_DEFINED, REASON_NO_LOCKED_HOLDOUT),
        ({}, HoldoutState.CONTAMINATED, REASON_NO_LOCKED_HOLDOUT),
        (
            {"registration_state": RegistrationState.LEGACY_UNPREGISTERED},
            HoldoutState.LOCKED,
            REASON_NOT_PREREGISTERED,
        ),
        ({"dataset_sha256_present": False}, HoldoutState.LOCKED, REASON_NO_DATASET_HASH),
        (
            {"return_series_basis": ReturnSeriesBasis.REALIZED_CLOSED_TRADES},
            HoldoutState.LOCKED,
            REASON_NO_MARK_TO_MARKET,
        ),
        (
            {"return_series_basis": ReturnSeriesBasis.MISSING},
            HoldoutState.LOCKED,
            REASON_NO_MARK_TO_MARKET,
        ),
        (
            {"cost_status": CostAttributionStatus.PARTIAL},
            HoldoutState.LOCKED,
            REASON_COST_NOT_ATTRIBUTED,
        ),
        (
            {"cost_status": CostAttributionStatus.UNAVAILABLE},
            HoldoutState.LOCKED,
            REASON_COST_NOT_ATTRIBUTED,
        ),
    ],
)
def test_each_reason_fires_alone(facts: dict[str, Any], holdout: HoldoutState, reason: str) -> None:
    assert blocking_reasons(_facts(**facts), _holdout(holdout)) == (reason,)


@pytest.mark.parametrize(
    "facts", [{"baseline_net_expectancy": 0.0}, {"baseline_net_expectancy": None}]
)
def test_an_expectancy_that_is_zero_or_unmeasured_is_not_a_negative_one(
    facts: dict[str, Any],
) -> None:
    assert blocking_reasons(_facts(**facts), _holdout()) == ()


@pytest.mark.parametrize("state", [HoldoutState.LOCKED, HoldoutState.OPENED, HoldoutState.CONSUMED])
def test_a_locked_or_opened_holdout_is_not_a_missing_one(state: HoldoutState) -> None:
    assert REASON_NO_LOCKED_HOLDOUT not in blocking_reasons(_facts(), _holdout(state))


def test_the_session_momentum_shape_gives_all_six_reasons_in_the_documented_order() -> None:
    facts = _facts(
        registration_state=RegistrationState.LEGACY_UNPREGISTERED,
        dataset_sha256_present=False,
        return_series_basis=ReturnSeriesBasis.REALIZED_CLOSED_TRADES,
        cost_status=CostAttributionStatus.PARTIAL,
        baseline_net_expectancy=-4.2,
    )

    assert blocking_reasons(facts, _holdout(HoldoutState.CONTAMINATED)) == (
        "negative expectancy at baseline costs",
        "no locked unseen holdout",
        "legacy evidence was not preregistered",
        "dataset-content hash is unavailable",
        "mark-to-market returns are unavailable",
        "spread and slippage cannot be separately attributed",
    )


# --- the decision -----------------------------------------------------------------------------


def _vector(statuses: dict[int, GateStatus]) -> tuple[GateResult, ...]:
    out = []
    for number in range(1, 10):
        status = statuses.get(number, GateStatus.PASS)
        out.append(
            GateResult(
                gate=number,
                name=GATE_NAMES[number],
                status=status,
                measured_value=None if status is GateStatus.UNAVAILABLE else 1.0,
                threshold=None,
                evidence_sha256=(D,),
                reason="r",
            )
        )
    return tuple(out)


U, F = GateStatus.UNAVAILABLE, GateStatus.FAIL
RESEARCH = (1, 3, 4, 5, 6, 7, 8, 9)


def test_all_nine_passing_with_an_opened_or_consumed_holdout_and_no_reason_is_paper_approved() -> (
    None
):
    results = _vector({})

    assert decide(results, _holdout(HoldoutState.OPENED), ()) is Decision.PAPER_APPROVED
    assert decide(results, _holdout(HoldoutState.CONSUMED), ()) is Decision.PAPER_APPROVED


def test_research_gates_passing_and_a_locked_unopened_holdout_is_research_passed() -> None:
    results = _vector({2: U})

    assert decide(results, _holdout(HoldoutState.LOCKED), ()) is Decision.RESEARCH_PASSED


@pytest.mark.parametrize(
    "state",
    [
        HoldoutState.NOT_DEFINED,
        HoldoutState.CONTAMINATED,
        HoldoutState.OPENED,
        HoldoutState.CONSUMED,
    ],
)
def test_research_gates_passing_without_a_locked_holdout_is_rejected(state: HoldoutState) -> None:
    assert decide(_vector({2: U}), _holdout(state), ()) is Decision.REJECTED


@pytest.mark.parametrize("gate", RESEARCH)
@pytest.mark.parametrize("status", [F, U])
@pytest.mark.parametrize("state", [HoldoutState.LOCKED, HoldoutState.OPENED])
def test_any_research_gate_that_is_not_a_pass_rejects(
    gate: int, status: GateStatus, state: HoldoutState
) -> None:
    results = _vector({2: U if state is HoldoutState.LOCKED else GateStatus.PASS, gate: status})

    assert decide(results, _holdout(state), ()) is Decision.REJECTED


@pytest.mark.parametrize("status", [F, U])
def test_a_failing_or_unavailable_holdout_gate_on_an_opened_holdout_rejects(
    status: GateStatus,
) -> None:
    assert decide(_vector({2: status}), _holdout(HoldoutState.OPENED), ()) is Decision.REJECTED


@pytest.mark.parametrize("state", [HoldoutState.LOCKED, HoldoutState.OPENED])
def test_one_blocking_reason_rejects_whatever_the_gates_say(state: HoldoutState) -> None:
    results = _vector({2: U} if state is HoldoutState.LOCKED else {})

    assert decide(results, _holdout(state), (REASON_NO_DATASET_HASH,)) is Decision.REJECTED


def test_a_passing_holdout_gate_on_a_holdout_that_is_not_opened_is_not_an_approval() -> None:
    """An inconsistent input (gate 2 passing with the state LOCKED) is never PAPER_APPROVED."""

    assert decide(_vector({}), _holdout(HoldoutState.LOCKED), ()) is not Decision.PAPER_APPROVED
    assert decide(_vector({}), _holdout(HoldoutState.CONTAMINATED), ()) is Decision.REJECTED


def test_a_decision_over_other_than_the_nine_gates_is_refused() -> None:
    results = _vector({})

    with pytest.raises(PromotionRefusedError):
        decide(results[:8], _holdout(), ())
    with pytest.raises(PromotionRefusedError):
        decide((*results[:8], results[0]), _holdout(), ())


def test_the_decision_vocabulary_is_the_three_values_the_ledger_payload_accepts() -> None:
    accepted = set(GateDecidedPayload.model_fields["decision"].annotation.__args__)  # type: ignore[union-attr]

    assert (
        {d.value for d in Decision}
        == accepted
        == {
            "REJECTED",
            "RESEARCH_PASSED",
            "PAPER_APPROVED",
        }
    )


# --- the report --------------------------------------------------------------------------------


def _report(**change: Any) -> ValidationReport:
    """A legacy-shaped report: no statistical evidence, every gate unavailable, REJECTED."""

    holdout = _holdout(HoldoutState.CONTAMINATED)
    facts = _facts()
    inputs = _inputs(None, holdout=holdout, spec=None)
    gates = evaluate_gates(inputs)
    reasons = blocking_reasons(facts, holdout)
    fields: dict[str, Any] = {
        "trial_id": TRIAL,
        "attempt_id": "attempt-1",
        "spec_sha256": D,
        "policy_sha256": policy_sha256(None),
        "policy": policy_digest_input(None),
        "chain_head": "h" * 64,
        "holdout": holdout,
        "facts": facts,
        "evidence": None,
        "gates": gates,
        "blocking_reasons": reasons,
        "decision": decide(gates, holdout, reasons),
    }
    return ValidationReport(**{**fields, **change})


def test_the_report_digest_is_derived_and_the_report_holds_no_field_for_it() -> None:
    report = _report()

    assert report.digest() == canonical_sha256(report)
    assert report.report_schema_version == 1
    assert not [
        name
        for name in ValidationReport.model_fields
        if "sha256" in name
        and name
        not in {
            "spec_sha256",
            "policy_sha256",
        }
    ]
    assert report.decision is Decision.REJECTED


def test_the_report_digest_binds_the_policy_the_data_and_the_results() -> None:
    """One change in each of the three, the digest moves; no two moves collide."""

    base = _report().digest()
    other_policy = policy_digest_input(SPEC)
    policy_changed = _report(policy=other_policy, policy_sha256=canonical_sha256(other_policy))
    data_changed = _report(chain_head="i" * 64)
    facts_changed = _report(facts=_facts(baseline_evidence_sha256=E))
    results = list(_report().gates)
    results[2] = results[2].model_copy(update={"reason": results[2].reason + "!"})
    results_changed = _report(gates=tuple(results))

    digests = {
        base,
        policy_changed.digest(),
        data_changed.digest(),
        facts_changed.digest(),
        results_changed.digest(),
    }
    assert len(digests) == 5


@pytest.mark.parametrize(
    "change",
    [
        {"policy_sha256": "0" * 64},
        {"decision": Decision.PAPER_APPROVED},
        {"decision": Decision.RESEARCH_PASSED},
        {"gates": ()},
    ],
)
def test_a_report_whose_policy_gates_or_decision_disagree_is_refused(
    change: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError):
        _report(**change)


def test_a_report_with_its_gates_out_of_order_is_refused() -> None:
    gates = _report().gates

    with pytest.raises(ValidationError):
        _report(gates=(gates[1], gates[0], *gates[2:]))
