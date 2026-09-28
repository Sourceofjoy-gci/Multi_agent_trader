"""The declared grid, the six checks a candidate's scenarios must pass, and the report.

Every case here is built from **real** bundles: a real ``TrialProtocol``, and for
each multiplier a real ``Backtester`` run over in-memory bars through the
production ``build_backtester`` factory, on the real signed constitution, with
the real ``RiskEngine`` and the real fill model -- sealed by the real
``mark_to_market_bundle`` builder. The bar reader is the only substitution,
because a real one is PostgreSQL and this file runs where Docker does not.

That is the point of the file. The report's whole value is that it reads the
same documents a reader would, and a test that handed it a hand-built bundle
would be testing that the report reads what it is given -- which is true of any
reader and proves nothing about the checks.

Every tamper below is rebuilt through ``EvidenceBundle.model_validate_json`` of
the document's own bytes with the change applied to the payload first, never
``model_copy(update=...)``. A tampered document built by bypassing validation
could be asserting on something the engine would refuse to seal, and the test
would prove nothing. So the refusals below are the report's, not Pydantic's:
each tampered document is one today's model accepts whole.

One helper builds them all, and every keyword is a field a test above actually
uses. The alternative is eight near-identical fixture functions, which is a
worse trade than one long builder.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import pytest

from tests.unit.research.backtest.conftest import (
    ATR_PERIOD,
    SPREAD_WINDOW,
    FakeBarReader,
    ToyStrategy,
    _contract,
    _loaded_constitution,
    _session_ramp,
)
from trading_house.core.errors import ScenarioEvidenceError
from trading_house.marketdata.models import Timeframe
from trading_house.ops.backtest import build_backtester, mark_to_market_bundle
from trading_house.ops.scenarios import (
    ScenarioDegradation,
    declared_grid,
    registered_protocol,
    scenario_report,
)
from trading_house.research.backtest.costs import CostModel
from trading_house.research.backtest.engine import BacktestRequest
from trading_house.research.backtest.result import BacktestResult
from trading_house.research.canonical import canonical_sha256
from trading_house.research.evidence import EvidenceBundle
from trading_house.research.trial_ledger import (
    CostAttributionStatus,
    CostSpec,
    DataSpec,
    ExecutionSpec,
    HoldoutSpec,
    HoldoutState,
    LedgerEvent,
    LedgerEventType,
    PreregisteredPayload,
    RegimeSpec,
    RegistrationState,
    ScopeKind,
    TrialProtocol,
    TrialSpec,
    ValidationSpec,
)

GRID = (Decimal("1"), Decimal("1.5"), Decimal("2"))
"""The three levels a ``CostSpec`` validator makes mandatory, written out so a
test that reads them is reading the shape rather than re-deriving it from the
protocol it is about to check."""

BAR_COUNT = 97
"""M15 bars spanning two UTC dates: ``_session_ramp``'s own Monday 00:00 origin
through Tuesday 00:00 inclusive.

Two dates is not decoration. ``swap_cost`` charges once per *rollover crossing*
-- one date strictly after the entry's, up to and including the exit's -- so a
run that opens and closes inside one day pays no swap at all and cannot show
the signed term 8B2b exists to report. The window has to cross midnight, and 97
bars is what does it at the smallest step that keeps the run cheap."""

BARS = _session_ramp(BAR_COUNT)
"""The one bar series every scenario here is run over. Built once at import so
the data window the protocol declares, the window the report checks, and the
window the engine actually replays cannot be three different series."""

WINDOW_START = BARS[0].event_time
WINDOW_END = BARS[-1].event_time

STRATEGY_HORIZON_SECONDS = 10_800
"""``horizon_is_simulatable`` refuses a strategy horizon under ten bars of the
run's timeframe, and M15's ten bars are 9000 s. ``ToyStrategy``'s own 7200 would
be refused before the run began."""

HOLDING_SECONDS = 67_500
"""The position's time stop, set so the deadline lands on Tuesday 00:00 and the
run's last bar is the one that closes it. The entry is Monday 05:15 (bar 21 --
bar 20 is the first with an ATR window behind it), so 67500 s is 18h45m and the
first bar at or after the deadline is the 97th. A later close would open a
second position the range then discards, and the run would end holding it."""

ATTEMPT_PREFIX = "att-scenarios"
SPEC_SHA256 = "e" * 64
AGENT_RUN_ID = "run-scenarios"
OCCURRED_AT = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
REGISTERED_AT = datetime(2026, 9, 20, 13, 0, tzinfo=UTC)

FOREIGN_TRIAL_ID = "trial-somebody-else"
TAMPERED_WINDOW_END = datetime(2025, 6, 1, tzinfo=UTC)
"""An end the protocol cannot have declared -- it precedes the run's own start.
``BacktestResult`` holds no start-before-end rule of its own, so a document
carrying it is one the model accepts, which is the only kind worth handing the
report: a refusal that came from Pydantic would be proving the wrong layer."""


def _protocol(candidate_ids: tuple[str, ...] = ("trial-1",)) -> TrialProtocol:
    """A preregistration whose ``costs`` and ``data`` are this file's own.

    The data window is the run's window, because check 5 compares the two and a
    protocol declaring a different one would make every other test here fail for
    a reason that is not the one it names. The baseline costs differ from every
    value the tampers below apply, so a tampered scenario is visibly tampered
    rather than coincidentally equal to something.

    ``slippage_points_per_side`` is non-zero rather than the conftest default,
    because ``stressed_overrides`` changes it and a scenario that changed from
    zero to zero would change nothing.
    """

    return TrialProtocol(
        protocol_id="protocol-scenarios",
        protocol_version="1",
        agent_run_id=AGENT_RUN_ID,
        strategy_id="toy",
        strategy_version="1",
        strategy_sha256="b" * 64,
        data=DataSpec(
            instrument_id="fx.eurusd",
            timeframe=Timeframe.M15,
            start=WINDOW_START,
            end=WINDOW_END,
            dataset_sha256="a" * 64,
            point_in_time_policy="availability_time",
        ),
        execution=ExecutionSpec(
            seed="fixed",
            warmup_bars=20,
            fill_policy="pessimistic-bar",
            sizing_policy="risk-engine",
        ),
        costs=CostSpec(
            baseline=CostModel(
                commission_per_lot_per_side=Decimal("2.50"),
                slippage_points_per_side=Decimal("0.5"),
                swap_long_points_per_day=Decimal("-0.80"),
                swap_short_points_per_day=Decimal("0.30"),
                triple_swap_weekday=2,
                stress_multiplier=Decimal("1"),
            ),
            stress_multipliers=GRID[1:],
        ),
        validation=ValidationSpec(
            primary_metric="net_expectancy",
            wfa_train_months=24,
            wfa_validation_months=6,
            wfa_test_months=6,
            purge_hours=16,
            embargo_hours=16,
            cpcv_folds=6,
            bootstrap_replicates=10000,
            bootstrap_c=Decimal("6.7"),
            trial_count_rule="conservative-selection-lotteries",
        ),
        regimes=RegimeSpec(labels=("london", "new_york"), provenance_sha256="c" * 64),
        holdout=HoldoutSpec(state=HoldoutState.NOT_DEFINED),
        candidates=tuple(
            TrialSpec(
                trial_id=trial_id,
                spec_id=f"spec-{trial_id}",
                rationale="declared before any result existed",
                parameter_space=(("window", "20"),),
            )
            for trial_id in candidate_ids
        ),
    )


def _trial_id(protocol: TrialProtocol) -> str:
    return protocol.candidates[0].trial_id


def _preregistered_event(protocol: TrialProtocol) -> LedgerEvent:
    """The event ``PostgresTrialLedger.register`` appends, built here.

    ``trial_id`` is left null and that is the point rather than an oversight: a
    protocol seals its whole candidate family inside one event, so the
    ``PREREGISTERED`` row's ``trial_id`` column is null and a
    ``WHERE trial_id = %s`` read cannot return it. Building the row the way the
    store does is what makes the recovery test below reach the recovery.
    """

    digest = canonical_sha256(protocol)
    return LedgerEvent(
        event_id=uuid5(NAMESPACE_URL, f"trading-house:trial-protocol:{digest}"),
        scope_kind=ScopeKind.PROTOCOL,
        scope_id=protocol.protocol_id,
        event_type=LedgerEventType.PREREGISTERED,
        spec_sha256=digest,
        occurred_at=protocol.data.end,
        payload=PreregisteredPayload(
            event_type=LedgerEventType.PREREGISTERED,
            protocol=protocol,
            registration_state=RegistrationState.PROSPECTIVE,
        ),
    )


def _bundle_at(
    protocol: TrialProtocol, bars: tuple[Any, ...], multiplier: Decimal, *, trial_id: str
) -> EvidenceBundle:
    """One scenario, from a real run at one multiplier, sealed the way the chain seals it.

    The cost model is the protocol's own baseline with the multiplier set --
    built through ``CostModel``'s constructor so it validates rather than
    bypassing the model, which is what the whole file is about. The bars, the
    contract, the constitution, the risk engine and the bundle builder are all
    production objects; ``FakeBarReader`` is the only substitution.
    """

    baseline = protocol.costs.baseline
    outcome = build_backtester(
        bars=FakeBarReader(bars), contract=_contract(), constitution=_loaded_constitution()
    ).run(
        BacktestRequest(
            strategy=ToyStrategy(
                horizon_seconds=STRATEGY_HORIZON_SECONDS,
                max_holding_seconds=HOLDING_SECONDS,
            ),
            instrument_id="fx.eurusd",
            timeframe=Timeframe.M15,
            start=bars[0].event_time,
            end=bars[-1].event_time,
            firm_equity=Decimal("100000"),
            cost_model=CostModel(**{**baseline.model_dump(), "stress_multiplier": multiplier}),
            atr_period=ATR_PERIOD,
            spread_window=SPREAD_WINDOW,
        )
    )
    return mark_to_market_bundle(
        outcome,
        trial_id=trial_id,
        attempt_id=f"{ATTEMPT_PREFIX}-{multiplier}",
        spec_sha256=SPEC_SHA256,
        agent_run_id=AGENT_RUN_ID,
        occurred_at=OCCURRED_AT,
        registered_at=REGISTERED_AT,
    )


def _rebuild(bundle: EvidenceBundle, change: Callable[[dict[str, Any]], None]) -> EvidenceBundle:
    """A tampered document the model accepts whole.

    The document's own bytes, decoded, changed, and validated through the JSON
    path every reader takes -- never ``model_copy(update=...)``, which skips
    validation and could produce a document the engine would never seal. The
    result's digest is re-derived afterwards because
    ``EvidenceBundle.source_digest_is_the_result_it_carries`` binds the two, so
    a tamper inside ``result`` has to move the digest with it: the document that
    reaches the report is internally consistent, and refusing it is the report's
    job rather than Pydantic's.
    """

    payload = json.loads(bundle.model_dump_json())
    change(payload)
    payload["source_result_sha256"] = BacktestResult.model_validate_json(
        json.dumps(payload["result"])
    ).digest()
    return EvidenceBundle.model_validate_json(json.dumps(payload))


def _carrying(
    status: CostAttributionStatus,
) -> Callable[[dict[str, Any]], None]:
    """A summary downgraded to ``status``, and the split that only ``COMPLETE`` may carry.

    Both halves move together because ``EvidenceBundle`` already couples them:
    a ``PARTIAL`` summary beside a per-trade split is refused at construction,
    so a tamper that changed one without the other would be a ``ValidationError``
    and would prove the wrong layer. This is the shape a Phase 7 document has,
    and it is exactly the one the report cannot read.
    """

    def change(payload: dict[str, Any]) -> None:
        payload["costs"] = {
            **payload["costs"],
            "status": status.value,
            "spread_cost": None,
            "slippage_cost": None,
        }
        payload.pop("cost_attribution", None)

    return change


def _costs(overrides: dict[str, Decimal]) -> Callable[[dict[str, Any]], None]:
    """A scenario declaring costs other than the ones it ran with.

    Applied as text because that is how the document carries them: a
    ``CanonicalModel`` is strict, so its own bytes are the only way back into
    it, and a raw ``Decimal`` spliced into the payload is not a value the wire
    ever held.
    """

    def change(payload: dict[str, Any]) -> None:
        payload["result"] = {
            **payload["result"],
            "cost_model": {
                **payload["result"]["cost_model"],
                **{field: str(value) for field, value in overrides.items()},
            },
        }

    return change


def _another_trial() -> Callable[[dict[str, Any]], None]:
    """A scenario belonging to a different candidate."""

    def change(payload: dict[str, Any]) -> None:
        payload["trial_id"] = FOREIGN_TRIAL_ID

    return change


def _ending_at(end: datetime) -> Callable[[dict[str, Any]], None]:
    """A scenario that ran a window the protocol never declared."""

    def change(payload: dict[str, Any]) -> None:
        payload["result"] = {**payload["result"], "end": end.isoformat()}

    return change


def _sealed(
    protocol: TrialProtocol,
    *,
    multipliers: tuple[Decimal, ...] = GRID,
    summary_status: CostAttributionStatus | None = None,
    baseline_overrides: dict[str, Decimal] | None = None,
    stressed_overrides: dict[str, Decimal] | None = None,
    foreign_trial_at: Decimal | None = None,
    window_end: datetime | None = None,
) -> tuple[tuple[str, EvidenceBundle], ...]:
    """One candidate's sealed grid, as the chain would hold it.

    Every multiplier gets a real run and a real bundle; the keyword named then
    tampers with whichever bundle it identifies, and the pair's digest is the
    tampered document's own -- which is what the chain would hold for the bytes
    it sealed, and what the report reports as the file a row came from.

    ``multipliers`` selects which of the three are returned at all, so the
    completeness case is a grid that was never finished rather than a grid with
    something removed from it afterwards.
    """

    bars = BARS
    trial_id = _trial_id(protocol)
    sealed: dict[Decimal, tuple[str, EvidenceBundle]] = {}
    for multiplier in GRID:
        bundle = _bundle_at(protocol, bars, multiplier, trial_id=trial_id)
        if summary_status is not None:
            bundle = _rebuild(bundle, _carrying(summary_status))
        if baseline_overrides is not None and multiplier == Decimal("1"):
            bundle = _rebuild(bundle, _costs(baseline_overrides))
        if stressed_overrides is not None and multiplier == Decimal("1.5"):
            bundle = _rebuild(bundle, _costs(stressed_overrides))
        if foreign_trial_at is not None and multiplier == foreign_trial_at:
            bundle = _rebuild(bundle, _another_trial())
        if window_end is not None and multiplier == Decimal("1"):
            bundle = _rebuild(bundle, _ending_at(window_end))
        sealed[multiplier] = (canonical_sha256(bundle), bundle)
    return tuple(sealed[multiplier] for multiplier in multipliers)


def test_a_grid_is_read_from_the_protocol_and_never_hard_coded() -> None:
    protocol = _protocol()
    assert declared_grid(protocol) == (Decimal("1"), Decimal("1.5"), Decimal("2"))


def test_a_registration_that_names_no_such_trial_is_refused() -> None:
    # The grid's authority is the sealed registration, so a trial the chain does
    # not name has no grid -- which is what makes an unregistered candidate
    # unreportable rather than merely empty.
    with pytest.raises(ScenarioEvidenceError):
        registered_protocol([_preregistered_event(_protocol())], "trial-nobody-declared")


def test_a_grid_is_recovered_from_a_registration_several_candidates_share() -> None:
    # A registration seals the whole family in one event whose trial_id column is
    # null, so this is the case events_for(trial_id) cannot serve -- and getting
    # it wrong reports a registered candidate as unregistered.
    protocol = _protocol(candidate_ids=("trial-1", "trial-2"))
    recovered = registered_protocol([_preregistered_event(protocol)], "trial-2")

    assert recovered.protocol_id == protocol.protocol_id
    assert [c.trial_id for c in recovered.candidates] == ["trial-1", "trial-2"]


def test_an_incomplete_grid_is_refused() -> None:
    protocol = _protocol()
    sealed = _sealed(protocol, multipliers=(Decimal("1"), Decimal("1.5")))

    with pytest.raises(ScenarioEvidenceError):
        scenario_report(trial_id=_trial_id(protocol), protocol=protocol, sealed=sealed)


def test_a_summary_that_is_not_complete_is_refused() -> None:
    # PARTIAL carries None for the two terms 8B2a added, so the report cannot
    # name four separable costs and must refuse rather than read them as zero.
    protocol = _protocol()
    sealed = _sealed(protocol, summary_status=CostAttributionStatus.PARTIAL)

    with pytest.raises(ScenarioEvidenceError):
        scenario_report(trial_id=_trial_id(protocol), protocol=protocol, sealed=sealed)


def test_a_baseline_that_is_not_the_declared_one_is_refused() -> None:
    protocol = _protocol()
    sealed = _sealed(protocol, baseline_overrides={"commission_per_lot_per_side": Decimal("3.50")})

    with pytest.raises(ScenarioEvidenceError):
        scenario_report(trial_id=_trial_id(protocol), protocol=protocol, sealed=sealed)


def test_a_stressed_scenario_that_changed_anything_but_the_multiplier_is_refused() -> None:
    protocol = _protocol()
    sealed = _sealed(protocol, stressed_overrides={"slippage_points_per_side": Decimal("0.9")})

    with pytest.raises(ScenarioEvidenceError):
        scenario_report(trial_id=_trial_id(protocol), protocol=protocol, sealed=sealed)


def test_a_scenario_from_another_trial_is_refused() -> None:
    protocol = _protocol()
    sealed = _sealed(protocol, foreign_trial_at=Decimal("1"))

    with pytest.raises(ScenarioEvidenceError):
        scenario_report(trial_id=_trial_id(protocol), protocol=protocol, sealed=sealed)


def test_a_window_the_protocol_did_not_declare_is_refused() -> None:
    protocol = _protocol()
    sealed = _sealed(protocol, window_end=TAMPERED_WINDOW_END)

    with pytest.raises(ScenarioEvidenceError):
        scenario_report(trial_id=_trial_id(protocol), protocol=protocol, sealed=sealed)


def test_the_report_names_the_grid_it_expected_before_what_it_found() -> None:
    protocol = _protocol()
    report = scenario_report(
        trial_id=_trial_id(protocol), protocol=protocol, sealed=_sealed(protocol)
    )

    assert report.declared_multipliers == (Decimal("1"), Decimal("1.5"), Decimal("2"))
    assert [s.multiplier for s in report.scenarios] == [Decimal("1"), Decimal("1.5"), Decimal("2")]
    assert not hasattr(report, "total_cost")
    assert not any("total" in key for key in report.model_dump(mode="json"))


def test_swap_is_reported_signed_and_the_degradation_is_a_difference_in_net_pnl() -> None:
    protocol = _protocol()
    report = scenario_report(
        trial_id=_trial_id(protocol), protocol=protocol, sealed=_sealed(protocol)
    )
    baseline, stressed = report.scenarios[0], report.scenarios[1]

    assert baseline.net_pnl - stressed.net_pnl == report.degradations[0].net_pnl_delta
    # Signed: a charge is negative, a credit positive. Nothing sums the four
    # terms into one figure, which would bury a credit inside a plausible-looking
    # total. The field set is pinned rather than any single value, so a later
    # `total_delta` fails this test rather than laundering the four into one.
    assert stressed.swap < 0
    assert set(ScenarioDegradation.model_fields) == {
        "multiplier",
        "net_pnl_delta",
        "market_pnl_delta",
        "spread_cost_delta",
        "slippage_cost_delta",
        "commission_delta",
        "swap_delta",
    }
