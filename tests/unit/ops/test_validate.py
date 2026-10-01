"""Phase 8C3: the assembly of every measurement into one ``StatisticalEvidence``.

``statistical_evidence`` is a pure function over inputs that have already been read, so
these cases hand it small bundles built here: a 60-day series, twelve intraday trades whose
P&L is written out so every expected expectancy is arithmetic, and a protocol whose six
CPCV folds are ten days each. The reads against a real chain and store are the acceptance's.

Series: 2024-01-01 .. 2024-02-29. Folds of ten days: f0 Jan 1-10, f1 Jan 11-20, f2 Jan 21-30,
f3 Jan 31-Feb 9, f4 Feb 10-19, f5 Feb 20-29.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from tests.unit.ops.test_scenarios import _protocol
from tests.unit.research.test_evidence import _bundle
from tests.unit.research.validation.test_series import _trade
from trading_house.core.errors import ScenarioEvidenceError, StatisticalInputError
from trading_house.ops.scenarios import baseline_at
from trading_house.ops.validate import (
    ValidationInputs,
    _optional,
    horizon_of,
    statistical_evidence,
)
from trading_house.research.backtest.mark import EquityObservation, EquitySeries
from trading_house.research.backtest.sizing import SizingMode
from trading_house.research.canonical import canonical_sha256
from trading_house.research.evidence import DailyReturnPoint, EvidenceBundle
from trading_house.research.trial_ledger import (
    HoldoutState,
    ReturnSeriesBasis,
    TrialCounters,
    TrialProtocol,
)
from trading_house.research.validation.drawdown import NOT_MARKED
from trading_house.research.validation.montecarlo import mc_seed
from trading_house.strategies.impl.session_momentum import SESSION_MOMENTUM_ID

FIRST = date(2024, 1, 1)
BASELINE_DIGEST = "b" * 64
HEAD = "h" * 64
COUNTERS = TrialCounters(audit_attempts=3, selection_lotteries=1, effective_specifications=1)

PNL = [4.0, -2.0, 6.0, -8.0, 10.0, -4.0, 2.0, -6.0, 8.0, 0.0, 12.0, -10.0]


def _trades(pnl: list[float] = PNL, *, crosser: bool = False) -> tuple[Any, ...]:
    """Intraday trades, one hour long, entering every fifth day from Jan 1 (days 1, 6, 11 ...),
    so each ten-day fold exits exactly two and none straddles a fold start. The entry hours
    cycle 9, 17, 9, 3: london, new_york, london, asian.

    With ``crosser`` the first is replaced by one entering Jan 10 23:00 and exiting Jan 11
    01:00, which crosses fold 1's start.
    """

    hours = [9, 17, 9, 3] * 3
    out = []
    for i, value in enumerate(pnl):
        day = FIRST + timedelta(days=5 * i)
        entry = datetime(day.year, day.month, day.day, hours[i], tzinfo=UTC)
        out.append(_trade(entry, entry + timedelta(hours=1), repr(value), f"p{i}"))
    if crosser:
        out[0] = _trade(
            datetime(2024, 1, 10, 23, tzinfo=UTC),
            datetime(2024, 1, 11, 1, tzinfo=UTC),
            repr(pnl[0]),
            "crosser",
        )
    return tuple(out)


def _wave(n: int = 60) -> list[Decimal]:
    return [Decimal(str(round(0.002 * math.sin(1.3 * i) + 0.0004, 6))) for i in range(n)]


def _equity(*equities: str) -> EquitySeries:
    """Flat observations: equity above 100000 is realized profit, below is realized loss."""

    return EquitySeries(
        firm_equity=Decimal(100000),
        observations=tuple(
            EquityObservation(
                marked_at=datetime(2024, 1, 1, 9, tzinfo=UTC) + timedelta(hours=i),
                equity=Decimal(value),
                cumulative_realized_pnl=Decimal(value) - Decimal(100000),
                unrealized_pnl=Decimal(0),
                open_positions=0,
            )
            for i, value in enumerate(equities)
        ),
    )


def _spec(trial: str) -> str:
    """The digest a run of ``trial`` must declare: ``canonical_sha256`` of its candidate."""

    protocol = _protocol((trial,))
    return canonical_sha256(protocol.candidates[0])


def _make(
    *,
    level: str = "1",
    tamper: dict[str, Any] | None = None,
    holdout: HoldoutState | None = None,
    trades: tuple[Any, ...] | None = None,
    days: int = 60,
    basis: ReturnSeriesBasis = ReturnSeriesBasis.MARK_TO_MARKET,
    trial: str = "trial-1",
    attempt: str = "attempt-1",
    equities: tuple[str, ...] | None = ("100000", "99000", "100500"),
    sizing: SizingMode = SizingMode.CONSTANT_NOTIONAL,
    values: list[Decimal] | None = None,
) -> EvidenceBundle:
    bundle = _bundle()
    chosen = _trades() if trades is None else trades
    protocol = _protocol((trial,))
    result = bundle.result.model_copy(
        update={
            "trades": chosen,
            "strategy_id": protocol.strategy_id,
            "strategy_version": protocol.strategy_version,
            "start": protocol.data.start,
            "end": protocol.data.end,
            "cost_model": baseline_at(protocol, Decimal(level)),
            **(tamper or {}),
        }
    )
    provenance = bundle.provenance
    if holdout is not None:
        provenance = provenance.model_copy(update={"holdout_state": holdout})
    returns = values if values is not None else _wave(days)
    return bundle.model_copy(
        update={
            "result": result,
            "provenance": provenance,
            "spec_sha256": _spec(trial),
            "trial_id": trial,
            "attempt_id": attempt,
            "daily_returns": tuple(
                DailyReturnPoint(day=FIRST + timedelta(days=i), value=v)
                for i, v in enumerate(returns)
            ),
            "return_series_basis": basis,
            "mark_to_market": None if equities is None else _equity(*equities),
            "sizing": sizing,
        }
    )


def _protocol_for(*candidates: str, replicates: int = 40) -> TrialProtocol:
    protocol = _protocol(candidates or ("trial-1",))
    policy = protocol.validation.model_copy(update={"bootstrap_replicates": replicates})
    return protocol.model_copy(update={"validation": policy})


def _inputs(**changes: Any) -> ValidationInputs:
    baseline = changes.pop("baseline", (BASELINE_DIGEST, _make()))
    fields: dict[str, Any] = {
        "trial_id": "trial-1",
        "protocol": _protocol_for(),
        "baseline": baseline,
        "compounding": None,
        "stressed": {},
        "candidates": {"trial-1": baseline},
        "excluded": (),
        "counters": COUNTERS,
        "chain_head_sha256": HEAD,
        "horizon_seconds": 32_400,
    }
    return ValidationInputs(**{**fields, **changes})


def _measurements(value: object) -> list[dict[str, Any]]:
    """Every dumped Measurement anywhere in a document: a mapping with a value, a reason
    and the evidence digests."""

    found: list[dict[str, Any]] = []
    if isinstance(value, dict):
        if {"value", "undefined_reason", "evidence_sha256"} <= set(value):
            found.append(value)
        for item in value.values():
            found.extend(_measurements(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_measurements(item))
    return found


# --- the whole document ------------------------------------------------------------


def test_every_measurement_is_finite_or_has_a_reason_and_names_its_evidence() -> None:
    evidence = statistical_evidence(_inputs())
    document = json.loads(evidence.model_dump_json())

    found = _measurements(document)

    assert len(found) >= 20, "the walk must reach every measurement or it proves nothing"
    for item in found:
        assert (item["value"] is None) != (item["undefined_reason"] is None)
        if item["value"] is not None:
            assert math.isfinite(item["value"])
        assert item["evidence_sha256"], item
        assert all(isinstance(digest, str) and digest for digest in item["evidence_sha256"])
    assert {m["name"] for m in found} >= {
        "wfa_folds",
        "psr",
        "sharpe_annualised",
        "dsr",
        "pbo",
        "bootstrap_lower_bound",
        "p_halt",
        "p_loss",
        "max_drawdown_baseline",
        "max_drawdown_cpcv_path_0",
        "max_drawdown_cpcv_path_4",
        "max_drawdown_compounding",
        "cpcv_p5",
        "oos_trades",
        "regimes_represented",
        "scenario_expectancy_1.5",
        "scenario_expectancy_2.0",
    }


def test_the_identity_basis_series_and_chain_facts_are_the_inputs() -> None:
    evidence = statistical_evidence(_inputs())

    assert evidence.trial_id == "trial-1"
    assert evidence.spec_sha256 == _spec("trial-1")
    assert evidence.attempt_id == "attempt-1"
    assert evidence.basis_is_mark_to_market is True
    assert evidence.counters == COUNTERS
    assert evidence.evidence.baseline == BASELINE_DIGEST
    assert evidence.evidence.chain_head == HEAD
    assert evidence.dsr.chain_head_sha256 == HEAD
    assert (evidence.first_day, evidence.last_day) == (FIRST, date(2024, 2, 29))
    assert evidence.series_days == 60
    assert (evidence.purge_days, evidence.embargo_days) == (1, 1)
    assert evidence.cpcv_folds == 6
    assert evidence.capacity.status.value == "unavailable"


def test_a_realized_trades_basis_is_flagged_on_every_measurement_not_hidden() -> None:
    realized = _make(basis=ReturnSeriesBasis.REALIZED_CLOSED_TRADES, equities=None)

    evidence = statistical_evidence(_inputs(baseline=(BASELINE_DIGEST, realized)))

    assert evidence.basis_is_mark_to_market is False
    for item in _measurements(json.loads(evidence.model_dump_json())):
        if item["name"] not in ("max_drawdown_compounding",):
            assert item["basis_is_mark_to_market"] is False, item["name"]


def test_walk_forward_folds_are_counted_when_they_fit_and_explained_when_they_do_not() -> None:
    short = statistical_evidence(_inputs()).wfa_folds
    long_bundle = _make(days=1827, trades=(), values=[Decimal(0)] * 1827)
    five_years = statistical_evidence(_inputs(baseline=(BASELINE_DIGEST, long_bundle))).wfa_folds

    assert short.value is None
    assert "no fold of 24 training, 6 validation and 6 test months" in (
        short.undefined_reason or ""
    )
    assert short.evidence_sha256 == (BASELINE_DIGEST,)
    assert five_years.value == 5.0
    assert five_years.undefined_reason is None


# --- seeds -------------------------------------------------------------------------


def test_the_bootstrap_and_the_monte_carlo_are_seeded_from_the_baseline_attempt() -> None:
    evidence = statistical_evidence(_inputs())

    # sha256(b"<d*64>|attempt-1|8c-sb-1")[:8] big-endian, computed outside with hashlib
    expected = hashlib.sha256((_spec("trial-1") + "|attempt-1|8c-sb-1").encode()).digest()[:8]
    assert evidence.bootstrap.seed == int.from_bytes(expected, "big")
    expected_mc = hashlib.sha256((_spec("trial-1") + "|attempt-1|8c-mc-1").encode()).digest()[:8]
    assert evidence.monte_carlo.seed == int.from_bytes(expected_mc, "big")
    assert evidence.monte_carlo.seed == mc_seed(_spec("trial-1"), "attempt-1")
    assert evidence.bootstrap.policy_version == "8c-sb-1"
    assert evidence.monte_carlo.policy_version == "8c-mc-1"
    assert evidence.monte_carlo.seed != evidence.bootstrap.seed
    other = statistical_evidence(_inputs(baseline=(BASELINE_DIGEST, _make(attempt="attempt-2"))))
    assert other.monte_carlo.seed == mc_seed(_spec("trial-1"), "attempt-2")
    assert other.monte_carlo.seed != evidence.monte_carlo.seed
    assert other.bootstrap.seed != evidence.bootstrap.seed


def test_both_simulations_use_the_protocols_replicates_and_the_series_length_as_horizon() -> None:
    evidence = statistical_evidence(_inputs(protocol=_protocol_for(replicates=25)))

    assert evidence.bootstrap.replicates == 25
    assert evidence.monte_carlo.replicates == 25
    assert evidence.monte_carlo.horizon_days == 60
    # 6.7 * (60 / 3) ** (1 / 3) = 6.7 * 2.714 = 18.19 -> 18, the same block for both
    assert evidence.bootstrap.block_length == evidence.monte_carlo.block_length == 18


# --- the holding horizon -----------------------------------------------------------


@pytest.mark.parametrize(
    ("seconds", "days", "defined"),
    [
        (32_400, 1, True),  # the registered session momentum horizon: 9 hours
        (86_400, 1, True),  # exactly one day
        (86_401, 2, False),  # one second over rounds UP to two days
        (172_800, 2, False),
        (None, None, False),
    ],
)
def test_the_horizon_is_the_registered_seconds_rounded_up_to_whole_days(
    seconds: int | None, days: int | None, defined: bool
) -> None:
    evidence = statistical_evidence(_inputs(horizon_seconds=seconds))

    assert evidence.dsr.horizon_days == days
    assert (evidence.dsr.dsr.value is not None) is defined
    if days is None:
        assert evidence.dsr.dsr.undefined_reason == (
            "the declared holding horizon is not known, so DSR cannot be formed"
        )
    elif days != 1:
        assert f"horizon {days} days" in (evidence.dsr.dsr.undefined_reason or "")


def test_the_registered_strategys_horizon_is_read_without_a_run() -> None:
    protocol = _protocol_for().model_copy(
        update={"strategy_id": SESSION_MOMENTUM_ID, "strategy_version": "1"}
    )

    assert horizon_of(protocol) == (32_400, None)
    assert horizon_of(protocol.model_copy(update={"strategy_version": "2"})) == (
        None,
        "registered strategy session_momentum_eurusd is version 1, not the protocol's 2",
    )
    assert horizon_of(_protocol_for()) == (None, "strategy toy is not registered")


# --- the optional runs -------------------------------------------------------------


def test_missing_compounding_and_stressed_runs_are_undefined_never_zero() -> None:
    evidence = statistical_evidence(_inputs())

    assert evidence.max_drawdown_compounding.value is None
    assert evidence.max_drawdown_compounding.undefined_reason == "no compounding run is sealed"
    assert evidence.max_drawdown_compounding.evidence_sha256 == (BASELINE_DIGEST,)
    assert evidence.evidence.compounding is None
    assert [s.multiplier for s in evidence.scenario_expectancy] == [Decimal("1.5"), Decimal(2)]
    for scenario in evidence.scenario_expectancy:
        assert scenario.evidence_sha256 is None
        assert scenario.expectancy.value is None
        assert "no constant-notional run is sealed at" in (
            scenario.expectancy.undefined_reason or ""
        )
        assert scenario.expectancy.evidence_sha256 == (BASELINE_DIGEST,)


def test_a_sealed_compounding_run_gives_its_own_drawdown_and_digest() -> None:
    # firm 100000, then 100000, 99000 (a 1% fall), 100500: peak 100000, trough 99000.
    comp = _make(equities=("100000", "99000", "100500"), sizing=SizingMode.COMPOUNDING)
    flat = _make(equities=("100000", "100000"))

    with_comp = statistical_evidence(_inputs(compounding=("c" * 64, comp)))
    flat_comp = statistical_evidence(
        _inputs(compounding=("c" * 64, _make(equities=("100000", "100000"))))
    )

    assert with_comp.max_drawdown_compounding.value == pytest.approx(0.01)
    assert with_comp.max_drawdown_compounding.evidence_sha256 == ("c" * 64,)
    assert with_comp.max_drawdown_compounding.name == "max_drawdown_compounding"
    assert with_comp.evidence.compounding == "c" * 64
    assert flat_comp.max_drawdown_compounding.value == 0.0
    # the baseline's own drawdown is from the baseline series, not the compounding one
    assert with_comp.max_drawdown_baseline.value == pytest.approx(0.01)
    assert flat.mark_to_market is not None


def test_the_baseline_and_compounding_drawdowns_come_from_their_own_series() -> None:
    base = _make(equities=("100000", "95000", "100000"))  # a 5% fall
    comp = _make(equities=("100000", "90000", "100000"), sizing=SizingMode.COMPOUNDING)  # 10%

    evidence = statistical_evidence(
        _inputs(baseline=(BASELINE_DIGEST, base), compounding=("c" * 64, comp))
    )

    assert evidence.max_drawdown_baseline.value == pytest.approx(0.05)
    assert evidence.max_drawdown_baseline.evidence_sha256 == (BASELINE_DIGEST,)
    assert evidence.max_drawdown_compounding.value == pytest.approx(0.10)


def test_a_marked_baseline_without_an_equity_series_has_an_undefined_baseline_drawdown() -> None:
    bare = _make(equities=None)  # the basis says marked; the series is absent

    evidence = statistical_evidence(_inputs(baseline=(BASELINE_DIGEST, bare)))

    assert evidence.max_drawdown_baseline.value is None
    assert "no mark-to-market equity series" in (
        evidence.max_drawdown_baseline.undefined_reason or ""
    )


def test_a_realized_trades_bundle_has_no_drawdown_even_when_an_equity_series_is_present() -> None:
    """The series is there but the basis says closed trades: nothing may be taken from it.
    The baseline drawdown and all five CPCV path drawdowns are UNDEFINED, with the reason
    (not merely flagged non-marked)."""

    realized = _make(basis=ReturnSeriesBasis.REALIZED_CLOSED_TRADES, equities=("100000", "90000"))

    evidence = statistical_evidence(_inputs(baseline=(BASELINE_DIGEST, realized)))

    undefined = [evidence.max_drawdown_baseline, *evidence.max_drawdown_cpcv_paths]
    assert len(undefined) == 6
    for item in undefined:
        assert item.value is None, item.name
        assert item.undefined_reason == NOT_MARKED
        assert item.basis_is_mark_to_market is False
        assert item.evidence_sha256 == (BASELINE_DIGEST,)


def test_a_compounding_run_on_another_basis_carries_its_own_basis_flag() -> None:
    realized = _make(
        sizing=SizingMode.COMPOUNDING,
        basis=ReturnSeriesBasis.REALIZED_CLOSED_TRADES,
        equities=("100000", "90000"),
    )

    evidence = statistical_evidence(_inputs(compounding=("c" * 64, realized)))

    assert evidence.max_drawdown_baseline.basis_is_mark_to_market is True
    assert evidence.max_drawdown_compounding.basis_is_mark_to_market is False
    assert evidence.max_drawdown_compounding.value is None


def test_stressed_runs_are_measured_over_their_own_full_sealed_samples() -> None:
    # 1.5x: net P&L [2, 4] -> 3.0 ; 2.0x: [-6, 2] -> -2.0 ; the baseline's own mean is 1.0.
    one_five = _make(trades=_trades([2.0, 4.0]), level="1.5")
    two = _make(
        trades=_trades([-6.0, 2.0]), basis=ReturnSeriesBasis.REALIZED_CLOSED_TRADES, level="2"
    )

    evidence = statistical_evidence(
        _inputs(stressed={Decimal("1.5"): ("e" * 64, one_five), Decimal(2): ("f" * 64, two)})
    )

    first, second = evidence.scenario_expectancy
    assert (first.multiplier, first.evidence_sha256) == (Decimal("1.5"), "e" * 64)
    assert first.expectancy.name == "scenario_expectancy_1.5"
    assert first.expectancy.value == 3.0
    assert first.expectancy.evidence_sha256 == ("e" * 64,)
    assert first.expectancy.basis_is_mark_to_market is True
    assert second.expectancy.value == -2.0
    assert second.expectancy.basis_is_mark_to_market is False  # its own basis, not the baseline's
    assert second.expectancy.evidence_sha256 == ("f" * 64,)


def test_a_stressed_run_of_another_trial_is_refused() -> None:
    foreign = _make(trial="trial-9")

    with pytest.raises(ScenarioEvidenceError):
        statistical_evidence(_inputs(stressed={Decimal("1.5"): ("e" * 64, foreign)}))
    with pytest.raises(ScenarioEvidenceError):
        statistical_evidence(_inputs(compounding=("c" * 64, foreign)))


def test_an_optional_run_sealed_twice_is_refused_and_one_or_none_is_read() -> None:
    one = (BASELINE_DIGEST, _make(sizing=SizingMode.COMPOUNDING))
    two = ("z" * 64, _make(sizing=SizingMode.COMPOUNDING))

    assert _optional([], "trial-1", SizingMode.COMPOUNDING, None) is None
    assert _optional([one], "trial-1", SizingMode.COMPOUNDING, None) == one
    with pytest.raises(ScenarioEvidenceError) as error:
        _optional([one, two], "trial-1", SizingMode.COMPOUNDING, None)
    assert "2 compounding bundles" in str(error.value.__cause__)
    assert _optional([one], "trial-1", SizingMode.CONSTANT_NOTIONAL, None) is None


def _at_level(level: str) -> EvidenceBundle:
    return _make(level=level)


def test_an_optional_constant_run_is_found_by_its_stress_level_and_no_other() -> None:
    one, one_five, two = (
        ("1" * 64, _at_level("1")),
        ("5" * 64, _at_level("1.5")),
        ("2" * 64, _at_level("2")),
    )
    sealed = [one, one_five, two]

    assert _optional(sealed, "trial-1", SizingMode.CONSTANT_NOTIONAL, Decimal("1.5")) == one_five
    assert _optional(sealed, "trial-1", SizingMode.CONSTANT_NOTIONAL, Decimal(2)) == two
    assert _optional(sealed, "trial-1", SizingMode.CONSTANT_NOTIONAL, Decimal("3")) is None
    assert _optional([one], "trial-1", SizingMode.CONSTANT_NOTIONAL, Decimal("1.5")) is None


def test_the_deflation_denominator_is_the_chains_larger_counter() -> None:
    counters = TrialCounters(audit_attempts=9, selection_lotteries=5, effective_specifications=7)

    evidence = statistical_evidence(_inputs(counters=counters))

    assert evidence.counters == counters
    assert (evidence.dsr.selection_lotteries, evidence.dsr.effective_specifications) == (5, 7)
    assert evidence.dsr.trials == 7
    assert evidence.dsr.expected_max_z is not None
    assert evidence.dsr.expected_max_z > 0.0


def test_each_cpcv_paths_drawdown_is_measured_on_that_paths_own_series(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A path's series is the whole series today (returns are not purged), so a stand-in that
    depends on the ``path`` and ``folds`` it is handed proves the drawdowns are read from what
    ``path_return_series`` returns for each path: path ``i`` holds -(i + 1) / 1000 every day,
    whose drawdown over 60 days is 1 - (1 - (i + 1) / 1000) ** 60. It raises unless it is
    handed the six ten-day folds of the series."""

    import numpy as np

    import trading_house.ops.validate as module
    from trading_house.research.validation.series import ReturnSeries

    def stand_in(series: ReturnSeries, folds: Any, path: Any) -> ReturnSeries:
        if [fold.days for fold in folds] != [10] * 6 or folds[0].start != series.days[0]:
            raise AssertionError("the stand-in was not handed the series' own folds")
        return ReturnSeries(
            days=series.days,
            values=np.full(len(series.days), -(path.index + 1) / 1000),
            basis=series.basis,
            evidence_sha256=series.evidence_sha256,
        )

    monkeypatch.setattr(module, "path_return_series", stand_in)

    evidence = statistical_evidence(_inputs())

    assert [m.value for m in evidence.max_drawdown_cpcv_paths] == [
        pytest.approx(1 - (1 - (i + 1) / 1000) ** 60) for i in range(5)
    ]


# --- candidates, PBO and the DSR cross-section --------------------------------------


def test_one_candidate_leaves_pbo_undefined_and_lists_the_excluded() -> None:
    evidence = statistical_evidence(_inputs(excluded=("trial-z", "trial-a")))

    assert evidence.pbo.pbo.value is None
    assert evidence.pbo.candidates == ("trial-1",)
    assert evidence.pbo.excluded == ("trial-a", "trial-z")
    assert evidence.evidence.pbo_candidates == (BASELINE_DIGEST,)
    assert evidence.dsr.cross_section is None  # one Sharpe has no cross-section


def test_two_candidates_are_ranked_in_id_order_and_feed_the_dsr_cross_section() -> None:
    base = (BASELINE_DIGEST, _make())
    other_values = [Decimal(str(round(0.003 * math.cos(0.7 * i), 6))) for i in range(60)]
    other = ("o" * 64, _make(trial="trial-0", values=other_values, trades=_trades(PNL[::-1])))
    protocol = _protocol_for("trial-1", "trial-0")

    evidence = statistical_evidence(
        _inputs(protocol=protocol, baseline=base, candidates={"trial-1": base, "trial-0": other})
    )

    assert evidence.pbo.candidates == ("trial-0", "trial-1")
    assert evidence.evidence.pbo_candidates == ("o" * 64, BASELINE_DIGEST)  # by trial id
    assert evidence.pbo.pbo.evidence_sha256 == ("o" * 64, BASELINE_DIGEST)
    assert evidence.dsr.cross_section is not None
    assert evidence.dsr.cross_section > 0.0


def test_a_candidate_with_a_constant_series_has_no_sharpe_and_adds_none() -> None:
    base = (BASELINE_DIGEST, _make())
    flat = ("o" * 64, _make(trial="trial-0", values=[Decimal(0)] * 60))

    evidence = statistical_evidence(
        _inputs(
            protocol=_protocol_for("trial-1", "trial-0"),
            baseline=base,
            candidates={"trial-1": base, "trial-0": flat},
        )
    )

    assert evidence.dsr.cross_section is None  # only one candidate Sharpe exists


def test_the_pbo_basis_flag_is_true_only_when_every_ranked_candidate_is_marked() -> None:
    base = (BASELINE_DIGEST, _make())
    realized = (
        "o" * 64,
        _make(trial="trial-0", basis=ReturnSeriesBasis.REALIZED_CLOSED_TRADES, equities=None),
    )

    evidence = statistical_evidence(
        _inputs(
            protocol=_protocol_for("trial-1", "trial-0"),
            baseline=base,
            candidates={"trial-1": base, "trial-0": realized},
        )
    )

    assert evidence.pbo.pbo.basis_is_mark_to_market is False
    assert evidence.psr.basis_is_mark_to_market is True


def test_the_trial_must_be_among_its_own_ranked_candidates() -> None:
    other = (BASELINE_DIGEST, _make(trial="trial-0"))

    with pytest.raises(StatisticalInputError) as error:
        statistical_evidence(_inputs(candidates={"trial-0": other}))

    assert "own baseline is not among the ranked candidates" in str(error.value.__cause__)


def test_a_candidate_sealed_over_other_days_is_refused() -> None:
    base = (BASELINE_DIGEST, _make())
    shifted = _make(trial="trial-0", days=61)

    with pytest.raises(StatisticalInputError) as error:
        statistical_evidence(
            _inputs(
                protocol=_protocol_for("trial-1", "trial-0"),
                baseline=base,
                candidates={"trial-1": base, "trial-0": ("o" * 64, shifted)},
            )
        )

    assert "candidate trial-0 was sealed over different days" in str(error.value.__cause__)


def test_a_candidate_that_is_another_trials_bundle_is_refused() -> None:
    base = (BASELINE_DIGEST, _make())
    mislabelled = ("o" * 64, _make(trial="trial-7"))

    with pytest.raises(ScenarioEvidenceError):
        statistical_evidence(
            _inputs(
                protocol=_protocol_for("trial-1", "trial-0"),
                baseline=base,
                candidates={"trial-1": base, "trial-0": mislabelled},
            )
        )


# --- CPCV, coverage -------------------------------------------------------------------


def test_with_no_trade_straddling_a_fold_start_the_paths_are_one_and_p5_is_the_expectancy() -> None:
    """Twelve intraday trades, net P&L 4 -2 6 -8 10 -4 2 -6 8 0 12 -10, sum 12, mean 1.0.
    None straddles a fold start, so every path keeps all twelve and each path's expectancy
    is 1.0: p5 is 1.0 (the aggregate expectancy), and paths_differ says the paths are one."""

    evidence = statistical_evidence(_inputs())

    assert evidence.cpcv_p5.paths_differ is False
    assert evidence.cpcv_p5.p5.value == 1.0
    assert evidence.cpcv_p5.rank == 1
    assert [p.trades_kept for p in evidence.cpcv_p5.paths] == [12] * 5
    assert evidence.cpcv_p5.p5.evidence_sha256 == (BASELINE_DIGEST,)
    assert len(evidence.max_drawdown_cpcv_paths) == 5
    assert [m.name for m in evidence.max_drawdown_cpcv_paths] == [
        f"max_drawdown_cpcv_path_{i}" for i in range(5)
    ]


def test_a_trade_straddling_a_fold_start_makes_the_paths_differ_and_is_carried() -> None:
    evidence = statistical_evidence(
        _inputs(baseline=(BASELINE_DIGEST, _make(trades=_trades(crosser=True))))
    )

    assert evidence.cpcv_p5.paths_differ is True
    kept = [p.trades_kept for p in evidence.cpcv_p5.paths]
    assert sorted(kept) == [11, 11, 11, 11, 12]  # the crosser survives on one path only


def test_coverage_counts_the_fewest_path_and_the_declared_regimes_present() -> None:
    """All paths keep twelve trades. The protocol declares london and new_york; the trades'
    entry hours cycle 9, 17, 9, 3 = london, new_york, london, asian, so both declared labels
    have trades and the asian ones are not declared."""

    evidence = statistical_evidence(_inputs())

    assert evidence.coverage.oos_trades.value == 12.0
    assert evidence.coverage.regimes_represented.value == 2.0
    assert evidence.coverage.declared_labels == ("london", "new_york")
    assert evidence.coverage.oos_path_index == 0


def test_a_candidate_that_never_traded_has_undefined_p5_and_regimes_but_a_zero_count() -> None:
    evidence = statistical_evidence(_inputs(baseline=(BASELINE_DIGEST, _make(trades=()))))

    assert evidence.cpcv_p5.p5.value is None
    assert "path 0 kept no closed trade" in (evidence.cpcv_p5.p5.undefined_reason or "")
    assert evidence.coverage.oos_trades.value == 0.0
    assert evidence.coverage.regimes_represented.value is None


def test_the_document_carries_no_gate_or_decision_key() -> None:
    document = json.loads(statistical_evidence(_inputs()).model_dump_json())

    def keys(value: object) -> set[str]:
        if isinstance(value, dict):
            return {str(k) for k in value} | set().union(*(keys(v) for v in value.values()))
        if isinstance(value, list):
            return set().union(*(keys(v) for v in value))
        return set()

    found = keys(document)
    assert "basis_is_mark_to_market" in found
    assert "promotion_grade" not in found
    assert not [k for k in found if any(w in k for w in ("threshold", "verdict", "pass", "fail"))]


# --- the DSR cross-section and the horizon reason ----------------------------------------


def _two_point(first: str, second: str) -> list[Decimal]:
    """Thirty repetitions of a pair of daily returns: 60 days."""

    return [Decimal(first), Decimal(second)] * 30


def test_the_cross_section_is_the_sample_std_of_the_candidates_per_day_sharpes() -> None:
    """Thirty repeats of the pair (a, b): the mean is (a + b) / 2 and the deviations are
    +/-(a - b) / 2, so the ddof=1 std is (|a - b| / 2) * sqrt(60 / 59) and the per-day Sharpe
    is ((a + b) / |a - b|) * sqrt(59 / 60). With k = sqrt(59 / 60):
    candidate 1 is (0.003, 0.001): 0.004 / 0.002 = 2, Sharpe 2k;
    candidate 0 is (0.004, 0.002): 0.006 / 0.002 = 3, Sharpe 3k.
    The sample (ddof=1) std of two numbers is their difference over sqrt(2): k / sqrt(2) =
    sqrt(59 / 120) = 0.70119... (no annualisation: nothing is multiplied by sqrt(365))."""

    base = (BASELINE_DIGEST, _make(values=_two_point("0.003", "0.001")))
    other = ("o" * 64, _make(trial="trial-0", values=_two_point("0.004", "0.002")))

    evidence = statistical_evidence(
        _inputs(
            protocol=_protocol_for("trial-1", "trial-0"),
            baseline=base,
            candidates={"trial-1": base, "trial-0": other},
        )
    )

    assert evidence.dsr.cross_section == pytest.approx(0.7011893, abs=1e-6)
    assert evidence.dsr.cross_section_count == 2


def test_a_constant_series_candidate_is_not_counted_in_the_cross_section_but_is_ranked() -> None:
    base = (BASELINE_DIGEST, _make(values=_two_point("0.003", "0.001")))
    other = ("o" * 64, _make(trial="trial-0", values=_two_point("0.004", "0.002")))
    flat = ("f" * 64, _make(trial="trial-2", values=[Decimal(0)] * 60))

    evidence = statistical_evidence(
        _inputs(
            protocol=_protocol_for("trial-1", "trial-0", "trial-2"),
            baseline=base,
            candidates={"trial-1": base, "trial-0": other, "trial-2": flat},
        )
    )

    assert evidence.dsr.cross_section_count == 2
    assert evidence.dsr.cross_section == pytest.approx(0.7011893, abs=1e-6)
    assert evidence.pbo.candidates == ("trial-0", "trial-1", "trial-2")
    assert evidence.evidence.pbo_candidates == ("o" * 64, BASELINE_DIGEST, "f" * 64)


def test_one_candidate_enters_no_cross_section() -> None:
    evidence = statistical_evidence(_inputs())

    assert evidence.dsr.cross_section is None
    assert evidence.dsr.cross_section_count == 1


def test_an_unreadable_horizon_says_why_in_the_dsr_reason() -> None:
    unregistered = statistical_evidence(
        _inputs(horizon_seconds=None, horizon_reason="strategy toy is not registered")
    )
    wrong_version = statistical_evidence(
        _inputs(
            horizon_seconds=None,
            horizon_reason="registered strategy x is version 1, not the protocol's 2",
        )
    )

    assert unregistered.dsr.dsr.undefined_reason == (
        "the declared holding horizon is not known: strategy toy is not registered"
    )
    assert "is version 1, not the protocol's 2" in (wrong_version.dsr.dsr.undefined_reason or "")


# --- the trade and basis facts of the CPCV measurements ----------------------------------


def test_the_cpcv_p5_and_the_coverage_say_how_much_of_the_run_they_hold() -> None:
    whole = statistical_evidence(_inputs())
    crossing = statistical_evidence(
        _inputs(baseline=(BASELINE_DIGEST, _make(trades=_trades(crosser=True))))
    )

    assert whole.cpcv_p5.closed_trades == 12
    assert whole.cpcv_p5.all_trades_kept is True
    assert whole.coverage.closed_trades == 12
    assert whole.coverage.all_trades_kept is True
    assert whole.coverage.oos_trades.value == 12.0
    assert crossing.cpcv_p5.closed_trades == 12
    assert crossing.cpcv_p5.all_trades_kept is False  # four paths drop the crosser
    assert crossing.coverage.closed_trades == 12
    assert crossing.coverage.all_trades_kept is False
    assert crossing.coverage.oos_trades.value == 11.0


# --- fidelity: the same checks the sibling reports run -------------------------------------

_TAMPERS = {
    "strategy id": ({"strategy_id": "another"}, "strategy"),
    "strategy version": ({"strategy_version": "9"}, "strategy"),
    "window start": ({"start": datetime(2020, 1, 1, tzinfo=UTC)}, "ran ["),
    "window end": ({"end": datetime(2020, 1, 2, tzinfo=UTC)}, "ran ["),
}


def _cost_tamper(multiplier: str) -> dict[str, Any]:
    protocol = _protocol(("trial-1",))
    other = baseline_at(protocol, Decimal(multiplier)).model_copy(
        update={"commission_per_lot_per_side": Decimal("99")}
    )
    return {"cost_model": other}


@pytest.mark.parametrize("name", sorted(_TAMPERS))
def test_a_baseline_that_is_not_the_protocols_run_is_refused_per_clause(name: str) -> None:
    tamper, fragment = _TAMPERS[name]
    bad = (BASELINE_DIGEST, _make(tamper=tamper))

    with pytest.raises(ScenarioEvidenceError) as error:
        statistical_evidence(_inputs(baseline=bad))

    assert fragment in str(error.value.__cause__)


def test_a_baseline_whose_specification_digest_is_not_the_candidates_is_refused() -> None:
    bundle = _make().model_copy(update={"spec_sha256": "e" * 64})

    with pytest.raises(ScenarioEvidenceError) as error:
        statistical_evidence(_inputs(baseline=(BASELINE_DIGEST, bundle)))

    assert "declares spec" in str(error.value.__cause__)


def test_a_baseline_with_other_costs_is_refused() -> None:
    bad = (BASELINE_DIGEST, _make(tamper=_cost_tamper("1")))

    with pytest.raises(ScenarioEvidenceError) as error:
        statistical_evidence(_inputs(baseline=bad))

    assert "the protocol's baseline at level 1" in str(error.value.__cause__)


def test_a_ranked_candidate_that_is_not_its_protocols_run_is_refused() -> None:
    base = (BASELINE_DIGEST, _make())
    bad = ("o" * 64, _make(trial="trial-0", tamper=_cost_tamper("1")))

    with pytest.raises(ScenarioEvidenceError) as error:
        statistical_evidence(
            _inputs(
                protocol=_protocol_for("trial-1", "trial-0"),
                baseline=base,
                candidates={"trial-1": base, "trial-0": bad},
            )
        )

    assert "the protocol's baseline at level 1" in str(error.value.__cause__)


def test_a_candidate_whose_digest_is_another_candidates_is_refused() -> None:
    base = (BASELINE_DIGEST, _make())
    swapped = (
        "o" * 64,
        _make(trial="trial-0").model_copy(update={"spec_sha256": _spec("trial-1")}),
    )

    with pytest.raises(ScenarioEvidenceError) as error:
        statistical_evidence(
            _inputs(
                protocol=_protocol_for("trial-1", "trial-0"),
                baseline=base,
                candidates={"trial-1": base, "trial-0": swapped},
            )
        )

    assert "declares spec" in str(error.value.__cause__)


def test_a_compounding_run_with_other_costs_is_refused() -> None:
    bad = _make(sizing=SizingMode.COMPOUNDING, tamper=_cost_tamper("1"))

    with pytest.raises(ScenarioEvidenceError) as error:
        statistical_evidence(_inputs(compounding=("c" * 64, bad)))

    assert "the protocol's baseline at level 1" in str(error.value.__cause__)


def test_a_stressed_run_is_checked_against_the_baseline_at_its_own_level() -> None:
    """A 1.5x run must carry the baseline costs at 1.5 (so it is not refused for not being
    level 1), and one carrying other costs, or the level-1 costs, is refused."""

    good = _make(level="1.5")
    at_level_one = _make(level="1")
    other_costs = _make(level="1.5", tamper=_cost_tamper("1.5"))

    assert statistical_evidence(_inputs(stressed={Decimal("1.5"): ("e" * 64, good)}))
    for bad in (at_level_one, other_costs):
        with pytest.raises(ScenarioEvidenceError) as error:
            statistical_evidence(_inputs(stressed={Decimal("1.5"): ("e" * 64, bad)}))
        assert "the protocol's baseline at level 1.5" in str(error.value.__cause__)


# --- runs on the locked data are a different experiment -------------------------------------


def test_a_bundle_sealed_after_the_holdout_was_opened_is_not_a_research_run() -> None:
    from trading_house.ops.compounding import sealed_baseline, sealed_rerun

    base = (BASELINE_DIGEST, _make())
    comp = ("c" * 64, _make(sizing=SizingMode.COMPOUNDING))
    for state in (HoldoutState.OPENED, HoldoutState.CONSUMED):
        opened_base = ("x" * 64, _make(holdout=state, attempt="opened"))
        opened_comp = ("y" * 64, _make(holdout=state, sizing=SizingMode.COMPOUNDING))
        opened_stress = ("z" * 64, _make(holdout=state, level="1.5"))
        sealed = [base, opened_base, comp, opened_comp, opened_stress]

        assert sealed_baseline(sealed, "trial-1") == base
        assert sealed_rerun(sealed, "trial-1") == comp
        assert _optional(sealed, "trial-1", SizingMode.COMPOUNDING, None) == comp
        assert _optional(sealed, "trial-1", SizingMode.CONSTANT_NOTIONAL, Decimal("1.5")) is None
    # a locked (not opened) holdout is still a research run: two baselines are then a refusal
    locked = ("x" * 64, _make(holdout=HoldoutState.LOCKED, attempt="locked"))
    with pytest.raises(ScenarioEvidenceError):
        sealed_baseline([base, locked], "trial-1")
