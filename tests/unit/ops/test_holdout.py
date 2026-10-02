"""What the holdout opening seals and what gate 2 reads back from it (Phase 8D2).

The bundles are real runs. The one real strategy trade of ``test_scenarios``'s bar ramp happens
only over the full 97 bars, so the PROTOCOL's research window is declared as the first 80 of them
(a run on which closes nothing) and its locked holdout as the whole ramp. An opened bundle is then
a full run carrying one real closed trade, so gate 2's expectancy is that trade's net P&L, derived
here from the trade and not from the code under test.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from tests.unit.ops.test_scenarios import (
    BARS,
    WINDOW_END,
    WINDOW_START,
    _bundle_at,
    _protocol,
    _trial_id,
)
from trading_house.core.errors import PromotionRefusedError, ScenarioEvidenceError
from trading_house.marketdata.models import Coverage, Timeframe
from trading_house.ops.compounding import refuse_unfaithful
from trading_house.ops.holdout import (
    HOLDOUT_LEVELS,
    holdout_expectancies,
    refuse_outside_coverage,
)
from trading_house.ops.scenarios import declared_window
from trading_house.research.backtest.sizing import SizingMode
from trading_house.research.canonical import canonical_sha256
from trading_house.research.evidence import EvidenceBundle
from trading_house.research.trial_ledger import HoldoutSpec, HoldoutState, TrialProtocol

HOLDOUT_HASH = "9" * 64
RESEARCH_END = BARS[79].event_time
ONE, ONE_AND_A_HALF, TWO = Decimal(1), Decimal("1.5"), Decimal(2)


def _locked() -> TrialProtocol:
    base = _protocol()
    return base.model_copy(
        update={
            "data": base.data.model_copy(update={"end": RESEARCH_END}),
            "holdout": HoldoutSpec(
                state=HoldoutState.LOCKED,
                start=WINDOW_START,
                end=WINDOW_END,
                dataset_sha256=HOLDOUT_HASH,
            ),
        }
    )


PROTOCOL = _locked()
TRIAL = _trial_id(PROTOCOL)


def _bundle(level: Decimal, *, opened: bool, **provenance: object) -> EvidenceBundle:
    """A real run at ``level``: over the whole ramp, sealed as opened, or over the research
    window sealed as research. ``provenance`` overrides what the opened run records."""

    if not opened:
        return _bundle_at(PROTOCOL, BARS[:80], level, trial_id=TRIAL)
    return _bundle_at(
        PROTOCOL,
        BARS,
        level,
        trial_id=TRIAL,
        **{"holdout_state": HoldoutState.OPENED, "dataset_sha256": HOLDOUT_HASH, **provenance},
    )


def _sealed(*bundles: EvidenceBundle) -> list[tuple[str, EvidenceBundle]]:
    return [(canonical_sha256(bundle), bundle) for bundle in bundles]


def _full_opening() -> list[tuple[str, EvidenceBundle]]:
    return _sealed(*(_bundle(level, opened=True) for level in (ONE, ONE_AND_A_HALF, TWO)))


def _cause(call: object) -> str:
    assert callable(call)
    with pytest.raises(PromotionRefusedError) as excinfo:
        call()
    assert str(excinfo.value) == "promotion step refused"
    return str(excinfo.value.__cause__)


# --- the window an opened run is held to ---------------------------------------------------


def test_the_declared_window_is_the_research_window_or_the_holdouts_never_the_other() -> None:
    assert declared_window(PROTOCOL) == (WINDOW_START, RESEARCH_END)
    assert declared_window(PROTOCOL, opened=True) == (WINDOW_START, WINDOW_END)
    assert declared_window(PROTOCOL) != declared_window(PROTOCOL, opened=True)


def test_a_protocol_with_no_holdout_window_has_none_to_give() -> None:
    with pytest.raises(ScenarioEvidenceError):
        declared_window(_protocol(), opened=True)


def test_an_opened_bundle_is_faithful_to_the_holdout_window_and_refused_on_the_research_one() -> (
    None
):
    opened = _bundle(ONE, opened=True)

    refuse_unfaithful(TRIAL, PROTOCOL, opened, ONE, opened=True)
    with pytest.raises(ScenarioEvidenceError):
        refuse_unfaithful(TRIAL, PROTOCOL, opened)


def test_a_research_bundle_is_faithful_to_the_research_window_and_refused_on_the_holdouts() -> None:
    research = _bundle(ONE, opened=False)

    refuse_unfaithful(TRIAL, PROTOCOL, research)
    with pytest.raises(ScenarioEvidenceError):
        refuse_unfaithful(TRIAL, PROTOCOL, research, ONE, opened=True)


def test_the_window_refusal_names_the_window_the_protocol_declares() -> None:
    with pytest.raises(ScenarioEvidenceError) as excinfo:
        refuse_unfaithful(TRIAL, PROTOCOL, _bundle(ONE, opened=True))

    assert str(WINDOW_END) in str(excinfo.value.__cause__)
    assert str(RESEARCH_END) in str(excinfo.value.__cause__)


def test_a_bundle_keeps_its_default_provenance_and_records_what_it_is_given() -> None:
    default = _bundle(ONE, opened=False).provenance
    opened = _bundle(ONE, opened=True).provenance

    assert (default.holdout_state, default.dataset_sha256) == (HoldoutState.NOT_DEFINED, None)
    assert (opened.holdout_state, opened.dataset_sha256) == (HoldoutState.OPENED, HOLDOUT_HASH)
    assert _bundle(ONE, opened=False) == _bundle_at(PROTOCOL, BARS[:80], ONE, trial_id=TRIAL)


# --- gate 2's inputs -----------------------------------------------------------------------


def test_the_two_expectancies_are_the_opened_bundles_net_pnl_per_closed_trade() -> None:
    sealed = _full_opening()
    by_level = {bundle.result.cost_model.stress_multiplier: (d, bundle) for d, bundle in sealed}

    at_1_5, at_2_0 = holdout_expectancies(TRIAL, PROTOCOL, sealed)

    for measurement, level, name in (
        (at_1_5, ONE_AND_A_HALF, "scenario_expectancy_1.5"),
        (at_2_0, TWO, "scenario_expectancy_2.0"),
    ):
        digest, bundle = by_level[level]
        trades = bundle.result.trades
        assert len(trades) == 1
        assert measurement.name == name
        assert measurement.value == float(sum(t.net_pnl for t in trades) / len(trades))
        assert measurement.evidence_sha256 == (digest,)
        assert measurement.basis_is_mark_to_market is True
    assert at_1_5.value != at_2_0.value  # the stress moved the figure


def test_the_levels_gate_two_reads_are_the_two_stressed_ones() -> None:
    assert HOLDOUT_LEVELS == (ONE_AND_A_HALF, TWO)


def test_research_bundles_in_the_sealed_set_are_not_opened_ones() -> None:
    research = _sealed(*(_bundle(level, opened=False) for level in (ONE, ONE_AND_A_HALF, TWO)))

    assert "0 opened bundles at 1.5x" in _cause(
        lambda: holdout_expectancies(TRIAL, PROTOCOL, research)
    )
    both = holdout_expectancies(TRIAL, PROTOCOL, [*research, *_full_opening()])
    assert [m.value is not None for m in both] == [True, True]


@pytest.mark.parametrize("missing", [ONE_AND_A_HALF, TWO])
def test_an_opening_without_either_stressed_bundle_is_refused(missing: Decimal) -> None:
    kept = [(d, b) for d, b in _full_opening() if b.result.cost_model.stress_multiplier != missing]

    assert f"0 opened bundles at {missing}x" in _cause(
        lambda: holdout_expectancies(TRIAL, PROTOCOL, kept)
    )


def test_an_opening_that_sealed_only_the_baseline_level_is_refused() -> None:
    only_baseline = _sealed(_bundle(ONE, opened=True))

    assert "opened bundles at 1.5x" in _cause(
        lambda: holdout_expectancies(TRIAL, PROTOCOL, only_baseline)
    )


def test_a_stressed_level_sealed_twice_is_refused_not_chosen_between() -> None:
    twice = _full_opening() + _sealed(
        _bundle(TWO, opened=True, holdout_state=HoldoutState.CONSUMED)
    )

    assert "2 opened bundles at 2x" in _cause(lambda: holdout_expectancies(TRIAL, PROTOCOL, twice))


def test_a_compounding_opened_bundle_does_not_stand_in_for_a_constant_one() -> None:
    sealed = [
        (d, b)
        for d, b in _full_opening()
        if b.result.cost_model.stress_multiplier != ONE_AND_A_HALF
    ]
    compounding = _bundle_at(
        PROTOCOL,
        BARS,
        ONE_AND_A_HALF,
        trial_id=TRIAL,
        sizing=SizingMode.COMPOUNDING,
        holdout_state=HoldoutState.OPENED,
        dataset_sha256=HOLDOUT_HASH,
    )

    assert "opened bundles at 1.5x" in _cause(
        lambda: holdout_expectancies(TRIAL, PROTOCOL, [*sealed, *_sealed(compounding)])
    )


def test_a_bundle_sealed_as_consumed_is_not_one_the_opening_made() -> None:
    sealed = _sealed(
        _bundle(ONE, opened=True),
        _bundle(ONE_AND_A_HALF, opened=True, holdout_state=HoldoutState.CONSUMED),
        _bundle(TWO, opened=True),
    )

    assert "says its holdout was consumed" in _cause(
        lambda: holdout_expectancies(TRIAL, PROTOCOL, sealed)
    )


def test_an_opened_bundle_must_carry_the_declared_holdout_dataset_hash() -> None:
    for wrong in (None, "8" * 64):
        sealed = _sealed(
            _bundle(ONE, opened=True),
            _bundle(ONE_AND_A_HALF, opened=True, dataset_sha256=wrong),
            _bundle(TWO, opened=True),
        )

        assert "declared holdout dataset hash" in _cause(
            lambda sealed=sealed: holdout_expectancies(TRIAL, PROTOCOL, sealed)
        )


def test_an_opened_bundle_run_on_the_research_window_is_refused_as_unfaithful() -> None:
    on_research = _bundle_at(
        PROTOCOL,
        BARS[:80],
        ONE_AND_A_HALF,
        trial_id=TRIAL,
        holdout_state=HoldoutState.OPENED,
        dataset_sha256=HOLDOUT_HASH,
    )
    sealed = [
        (d, b)
        for d, b in _full_opening()
        if b.result.cost_model.stress_multiplier != ONE_AND_A_HALF
    ] + _sealed(on_research)

    with pytest.raises(ScenarioEvidenceError):
        holdout_expectancies(TRIAL, PROTOCOL, sealed)


def test_a_missing_level_is_refused_before_the_other_levels_faithfulness_is_read() -> None:
    """A half-made opening is always the one refusal, however unfaithful the half that exists."""

    unfaithful = _bundle_at(
        PROTOCOL,
        BARS[:80],
        ONE_AND_A_HALF,
        trial_id=TRIAL,
        holdout_state=HoldoutState.OPENED,
        dataset_sha256="8" * 64,
    )

    assert "opened bundles at 2x" in _cause(
        lambda: holdout_expectancies(TRIAL, PROTOCOL, _sealed(unfaithful))
    )


def test_an_opened_bundle_of_another_candidate_is_refused() -> None:
    other = _bundle(ONE_AND_A_HALF, opened=True).model_copy(update={"trial_id": "someone-else"})
    sealed = [
        (d, b)
        for d, b in _full_opening()
        if b.result.cost_model.stress_multiplier != ONE_AND_A_HALF
    ] + _sealed(other)

    with pytest.raises(ScenarioEvidenceError):
        holdout_expectancies(TRIAL, PROTOCOL, sealed)


def test_an_opened_bundle_with_other_costs_than_the_declared_baseline_is_refused() -> None:
    sealed = _full_opening()
    forged = []
    for digest, bundle in sealed:
        if bundle.result.cost_model.stress_multiplier == TWO:
            result = bundle.result.model_copy(
                update={
                    "cost_model": bundle.result.cost_model.model_copy(
                        update={"commission_per_lot_per_side": Decimal("9")}
                    )
                }
            )
            bundle = bundle.model_copy(update={"result": result})
            digest = canonical_sha256(bundle)
        forged.append((digest, bundle))

    with pytest.raises(ScenarioEvidenceError):
        holdout_expectancies(TRIAL, PROTOCOL, forged)


# --- the coverage pre-flight ---------------------------------------------------------------

EARLIEST = datetime(2026, 9, 21, tzinfo=UTC)
LATEST = datetime(2026, 9, 23, tzinfo=UTC)


def _coverage(earliest: datetime | None, latest: datetime | None) -> Coverage:
    return Coverage(
        instrument_id="fx.eurusd",
        timeframe=Timeframe.M15,
        earliest_event_time=earliest,
        latest_event_time=latest,
        latest_availability_time=latest,
        clean_bars=0 if earliest is None else 100,
        defective_bars=0,
    )


def test_a_window_inside_the_stored_bars_passes_with_its_edges_included() -> None:
    refuse_outside_coverage(EARLIEST, LATEST, _coverage(EARLIEST, LATEST))
    refuse_outside_coverage(
        datetime(2026, 9, 22, tzinfo=UTC),
        datetime(2026, 9, 22, 6, tzinfo=UTC),
        _coverage(EARLIEST, LATEST),
    )


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (datetime(2026, 9, 20, 23, 45, tzinfo=UTC), LATEST),  # starts before the first bar
        (EARLIEST, datetime(2026, 9, 23, 0, 15, tzinfo=UTC)),  # ends after the last bar
    ],
)
def test_a_window_outside_the_stored_bars_is_refused(start: datetime, end: datetime) -> None:
    with pytest.raises(ScenarioEvidenceError):
        refuse_outside_coverage(start, end, _coverage(EARLIEST, LATEST))


def test_a_store_with_no_bars_covers_nothing() -> None:
    with pytest.raises(ScenarioEvidenceError):
        refuse_outside_coverage(EARLIEST, LATEST, _coverage(None, None))
