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
    ScenarioTotals,
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
OTHER_PROPOSAL_ID = "proposal-1a7f3c"
"""A different trade's identity, for a run that otherwise traded the same
numbers. ``SimulatedTrade.proposal_id`` and ``TradeCostAttribution.proposal_id``
are both bare ``NonEmptyStr`` and nothing in any validator binds either to the
engine's own proposal stream, so renaming one is a model-accepted document. It is
the one edit that moves a candidate's trade *sequence* while leaving every
sealed total exactly where it was, which is what the identity check's sequence
clause has to catch."""
TAMPERED_WINDOW_END = datetime(2025, 6, 1, tzinfo=UTC)
"""An end the protocol cannot have declared -- it precedes the run's own start.
``BacktestResult`` holds no start-before-end rule of its own, so a document
carrying it is one the model accepts, which is the only kind worth handing the
report: a refusal that came from Pydantic would be proving the wrong layer."""


def _all_keys(value: object) -> set[str]:
    """Every mapping key anywhere in a dumped report, at any depth.

    A top-level sweep over ``ScenarioReport`` finds five keys and would miss a
    ``total_cost`` nested inside ``ScenarioTotals``, which is where one would
    actually be added.
    """

    if isinstance(value, dict):
        return {str(key) for key in value} | set().union(
            *(_all_keys(item) for item in value.values())
        )
    if isinstance(value, list):
        return set().union(*(_all_keys(item) for item in value))
    return set()


def _protocol(
    candidate_ids: tuple[str, ...] = ("trial-1",),
    *,
    baseline_multiplier: str = "1",
) -> TrialProtocol:
    """A preregistration whose ``costs`` and ``data`` are this file's own.

    The data window is the run's window, because check 5 compares the two and a
    protocol declaring a different one would make every other test here fail for
    a reason that is not the one it names. The baseline costs differ from every
    value the tampers below apply, so a tampered scenario is visibly tampered
    rather than coincidentally equal to something.

    ``slippage_points_per_side`` is non-zero rather than the conftest default,
    because ``stressed_overrides`` changes it and a scenario that changed from
    zero to zero would change nothing.

    ``baseline_multiplier`` exists for one test. ``CostSpec.stress_grid_is_exact``
    pins ``stress_multipliers`` to exactly ``{1.5, 2}`` and says nothing about
    the baseline's own multiplier, which ``CostModel`` requires only to be
    positive -- so a protocol declaring a baseline of ``1.5`` is a registration
    the models accept. The report must still compare the level-1 scenario to
    that baseline's *money terms* and refuse on the multiplier alone, rather
    than refusing the whole candidate because the two never match.
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
                stress_multiplier=Decimal(baseline_multiplier),
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


def _moving(field: str, value: object) -> Callable[[dict[str, Any]], None]:
    """One top-level field of a scenario changed to something else.

    The point of taking a field name rather than one more purpose-built tamper
    is coverage of the identity check's other five fields. ``trial_id`` is the
    only one a dedicated case moves, and a typo in ``_identity``'s key names --
    or a field that stopped being read at all -- would leave the suite green
    while the check silently compared nothing. This moves any of the six by name,
    so each is provably load-bearing.
    """

    def change(payload: dict[str, Any]) -> None:
        if field.startswith("result."):
            name = field.removeprefix("result.")
            payload["result"] = {**payload["result"], name: value}
            if name == "bars_seen" and "mark_to_market" in payload:
                # ``bars_seen`` is the count the equity series must match, and a
                # flat run's final realized total must equal the result's net --
                # so all three move together. The document that results claims to
                # have read one bar, carries one observation, and shows the same
                # net PnL the full run did. Nothing cross-checks a trade against
                # a bar count, so it is model-accepted, and it is exactly the
                # "these two runs did not read the same data" shape that the
                # identity check must refuse and the window check cannot see.
                observations = payload["mark_to_market"]["observations"][:value]
                net = payload["result"]["net_pnl"]
                payload["mark_to_market"] = {
                    **payload["mark_to_market"],
                    "observations": [
                        *observations[:-1],
                        {
                            **observations[-1],
                            "cumulative_realized_pnl": net,
                            "unrealized_pnl": "0",
                            "equity": str(
                                Decimal(payload["mark_to_market"]["firm_equity"]) + Decimal(net)
                            ),
                        },
                    ],
                }
            return
        if field == "trades":
            # The sequence, changed: the one trade's ``proposal_id`` becomes a
            # different string. Renaming rather than deleting, because every
            # cross-check in the document binds the totals to the trades and
            # none of them binds the *identity* of a trade to anything else --
            # so this moves the one thing the identity check exists to pin while
            # leaving a document the model accepts whole. A run that had traded
            # a different proposal is a real, valid, different experiment, which
            # is exactly the shape this refusal has to catch.
            payload["result"] = {
                **payload["result"],
                "trades": [
                    {**payload["result"]["trades"][0], "proposal_id": OTHER_PROPOSAL_ID},
                    *payload["result"]["trades"][1:],
                ],
            }
            payload["cost_attribution"] = {
                "trades": [
                    {**payload["cost_attribution"]["trades"][0], "proposal_id": OTHER_PROPOSAL_ID},
                    *payload["cost_attribution"]["trades"][1:],
                ],
            }
            return
        payload[field] = value

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
    moved_at: Decimal | None = None,
    moved_field: str | None = None,
    moved_value: object = None,
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
        if moved_field is not None and multiplier == moved_at:
            bundle = _rebuild(bundle, _moving(moved_field, moved_value))
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


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("spec_sha256", "f" * 64),
        ("result.strategy_id", "some-other-strategy"),
        ("result.strategy_version", "99"),
        ("result.bars_seen", 1),
    ],
)
def test_every_identity_field_the_grid_pins_is_load_bearing(field: str, value: object) -> None:
    """The fields ``trial_id`` is not, one at a time.

    ``_refuse_identity`` compares a six-field record per bundle, and only
    ``trial_id`` has a dedicated case. A field that stopped being read -- a
    renamed key, a deleted entry, a constant -- would leave every other test
    green while the check quietly compared less than it claims. Each is moved by
    name and must refuse.

    Dotted names address into ``result``, which is where four of the six live;
    a dotted path that does not exist raises ``KeyError`` from the tamper
    builder rather than passing, so a typo here cannot make a case vacuous.
    """

    protocol = _protocol()
    sealed = _sealed(protocol, moved_at=Decimal("1.5"), moved_field=field, moved_value=value)

    with pytest.raises(ScenarioEvidenceError):
        scenario_report(trial_id=_trial_id(protocol), protocol=protocol, sealed=sealed)


def test_a_grid_whose_trade_sequence_moved_is_refused() -> None:
    """The sequence, not the prices -- the property 8B2a's spread stress implies.

    Scaling the spread moves every price and must not move which trades happened.
    A grid whose 1.5x scenario traded something the 1.0x did not is a different
    experiment, and a report that accepted it would be comparing two strategies
    rather than two cost levels.

    Moving a whole trade out of the sequence is not a one-field edit, so the
    tamper empties the stressed scenario's ``result.trades`` and its
    ``cost_attribution`` together -- a model-accepted document that traded
    nothing, and one the identity check must catch on the sequence.
    """

    protocol = _protocol()
    sealed = _sealed(protocol, moved_at=Decimal("1.5"), moved_field="trades")

    with pytest.raises(ScenarioEvidenceError):
        scenario_report(trial_id=_trial_id(protocol), protocol=protocol, sealed=sealed)


def test_a_window_the_protocol_did_not_declare_is_refused() -> None:
    protocol = _protocol()
    sealed = _sealed(protocol, window_end=TAMPERED_WINDOW_END)

    with pytest.raises(ScenarioEvidenceError):
        scenario_report(trial_id=_trial_id(protocol), protocol=protocol, sealed=sealed)


def test_a_protocol_whose_baseline_is_itself_stressed_is_still_reportable() -> None:
    """The one input that used to be permanently unreportable.

    ``CostSpec`` pins the stressed levels to exactly ``{1.5, 2}`` and says
    nothing about the baseline's own multiplier, so ``baseline.stress_multiplier
    = 1.5`` with ``stress_multipliers = (1.5, 2)`` is a registration the models
    accept. Comparing the level-1 scenario to that declaration field for field
    would refuse a candidate whose grid *is* the grid that was preregistered,
    with a message blaming the sealed scenarios and no remedy -- a registration
    cannot be amended after the fact.

    The report reads the money terms and refuses on the multiplier alone, which
    is the one field that cannot be right: a stress grid is defined relative to
    unstressed costs.
    """

    protocol = _protocol(baseline_multiplier="1.5")
    report = scenario_report(
        trial_id=_trial_id(protocol), protocol=protocol, sealed=_sealed(protocol)
    )

    assert report.declared_multipliers == (Decimal("1"), Decimal("1.5"), Decimal("2"))
    assert [s.multiplier for s in report.scenarios] == [Decimal("1"), Decimal("1.5"), Decimal("2")]


def test_a_baseline_scenario_whose_money_terms_drift_is_still_refused() -> None:
    """The fix above must not have loosened the baseline check into a no-op.

    Under a stressed baseline the multiplier field can no longer carry the
    refusal, so the money terms are what is left to catch a drifted baseline --
    and they have to still catch it.
    """

    protocol = _protocol(baseline_multiplier="1.5")
    sealed = _sealed(protocol, baseline_overrides={"commission_per_lot_per_side": Decimal("3.50")})

    with pytest.raises(ScenarioEvidenceError):
        scenario_report(trial_id=_trial_id(protocol), protocol=protocol, sealed=sealed)


def test_a_report_about_no_trial_is_refused_rather_than_crashing_on_construction() -> None:
    """The caller's own inputs fail as this module's refusal, not as Pydantic's.

    ``trial_id`` and the evidence digests land in ``NonEmptyStr`` fields, and a
    ``strict`` model answers an empty string with a ``ValidationError`` -- which
    echoes the offending value and is not the class the CLI's catch-all maps to
    exit 19. A report that cannot name its subject is refused at the boundary.

    Asserting on the private cause, not merely on the raise, because both of
    these inputs would be caught by a *later* check if the boundary let them
    through: a blank ``trial_id`` disagrees with every bundle's own, and a blank
    digest on one of three trips the completeness check first. Naming the cause
    is what proves the refusal came from the boundary rather than from a check
    that happened to fire on the way past it.
    """

    protocol = _protocol()
    sealed = _sealed(protocol)

    with pytest.raises(ScenarioEvidenceError) as blank_trial:
        scenario_report(trial_id="   ", protocol=protocol, sealed=sealed)
    assert "must name the trial" in str(blank_trial.value.__cause__)

    unnamed = [(sealed[0][0], sealed[0][1]), ("", sealed[1][1]), (sealed[2][0], sealed[2][1])]
    with pytest.raises(ScenarioEvidenceError) as unnamed_digest:
        scenario_report(trial_id=_trial_id(protocol), protocol=protocol, sealed=unnamed)
    assert "no digest to name it by" in str(unnamed_digest.value.__cause__)


def test_the_report_names_the_grid_it_expected_before_what_it_found() -> None:
    protocol = _protocol()
    report = scenario_report(
        trial_id=_trial_id(protocol), protocol=protocol, sealed=_sealed(protocol)
    )

    assert report.declared_multipliers == (Decimal("1"), Decimal("1.5"), Decimal("2"))
    assert [s.multiplier for s in report.scenarios] == [Decimal("1"), Decimal("1.5"), Decimal("2")]
    assert not hasattr(report, "total_cost")
    # Recursively, not over the top-level keys: a ``total_cost`` added to
    # ``ScenarioTotals`` is nested two levels down and a top-level sweep would
    # miss it. The model field sets are pinned below as well, which is the
    # statement that cannot be satisfied by adding a field.
    assert not any("total" in key for key in _all_keys(report.model_dump(mode="json")))
    assert set(ScenarioTotals.model_fields) == {
        "multiplier",
        "attempt_id",
        "evidence_sha256",
        "source_result_sha256",
        "trades",
        "market_pnl",
        "spread_cost",
        "slippage_cost",
        "commission",
        "swap",
        "net_pnl",
    }


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


def test_the_reported_terms_reconstruct_the_result_they_came_from() -> None:
    """The sign convention the no-total-cost design rests on, asserted.

    ``ScenarioTotals``'s docstring states ``net = market - spread - slippage -
    commission + swap``, and ``swap`` is the odd one out: a charge is negative
    and enters with a ``+``, so it *reduces* ``net`` while looking like an
    addition. That is exactly the hazard a summed "total cost" would hide, and
    the docstring asserting an unasserted relation is how it goes stale.

    The relation is checked against the sealed bundles themselves rather than
    against a literal, so it holds for whatever the engine produced on this
    fixture: a reader must be able to re-add the report's numbers and land on
    the ``net_pnl`` the chain holds.
    """

    protocol = _protocol()
    report = scenario_report(
        trial_id=_trial_id(protocol), protocol=protocol, sealed=_sealed(protocol)
    )
    sealed = {bundle.result.cost_model.stress_multiplier: bundle for _, bundle in _sealed(protocol)}

    for scenario in report.scenarios:
        rebuilt = (
            scenario.market_pnl
            - scenario.spread_cost
            - scenario.slippage_cost
            - scenario.commission
            + scenario.swap
        )
        assert rebuilt == scenario.net_pnl
        assert scenario.net_pnl == sealed[scenario.multiplier].result.net_pnl
