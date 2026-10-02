"""Every statistical measurement of one sealed candidate, assembled.

Phase 8C3. A read over the chain and the evidence store, kept apart from the logic:
``read_validation_inputs`` is the only function here that touches either, and
``statistical_evidence`` is a pure function over what it returned. It computes
measurements and states no decision about the candidate.

The baseline is the trial's one sealed constant-notional 1.0x run, and is refused when
there is none or more than one (exactly as ``splits`` refuses). The compounding run and
the 1.5x and 2.0x runs are optional: a level with no sealed run is an undefined
measurement, never a default. A level sealed twice is refused, since choosing between
the two would be a silent selection.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

from trading_house.core.errors import ConfigurationError, ScenarioEvidenceError
from trading_house.ops.compounding import refuse_unfaithful, sealed_baseline
from trading_house.ops.scenarios import (
    declared_grid,
    is_research_run,
    refuse_reportable,
    registered_protocol,
    sealed_bundles,
)
from trading_house.ops.splits import splits_report
from trading_house.research.backtest.sizing import SizingMode
from trading_house.research.evidence import EvidenceBundle, EvidenceStore
from trading_house.research.ledger_store import PostgresTrialLedger
from trading_house.research.trial_ledger import (
    LedgerEventType,
    ReturnSeriesBasis,
    TrialCounters,
    TrialProtocol,
    trial_counters,
)
from trading_house.research.validation.bootstrap import block_length, bootstrap_mean
from trading_house.research.validation.capacity import capacity_diagnostic
from trading_house.research.validation.coverage import (
    cpcv_p5,
    oos_coverage,
    scenario_expectancy,
    trade_subset,
)
from trading_house.research.validation.drawdown import equity_drawdown, path_drawdown
from trading_house.research.validation.evidence import (
    EvidenceDigests,
    ScenarioMeasurement,
    StatisticalEvidence,
)
from trading_house.research.validation.measurement import Measurement
from trading_house.research.validation.moments import Moments, moments
from trading_house.research.validation.montecarlo import drawdown_loss_probabilities, mc_seed
from trading_house.research.validation.pbo import pbo
from trading_house.research.validation.psr import dsr, psr, sharpe_annualised
from trading_house.research.validation.sampling import path_return_series, path_trade_sample
from trading_house.research.validation.series import ReturnSeries, TradeSample, refusal
from trading_house.research.validation.splits import cpcv_folds, cpcv_paths, cpcv_splits
from trading_house.strategies.registry import registered

Sealed = tuple[str, EvidenceBundle]

SECONDS_PER_DAY = 86_400


@dataclass(frozen=True)
class ValidationInputs:
    """Everything ``statistical_evidence`` needs, already read."""

    trial_id: str
    protocol: TrialProtocol
    baseline: Sealed
    compounding: Sealed | None
    stressed: Mapping[Decimal, Sealed]
    """The sealed constant-notional runs at the declared stress levels that have one."""
    candidates: Mapping[str, Sealed]
    """Every protocol candidate with exactly one sealed 1.0x baseline, this trial included."""
    excluded: tuple[str, ...]
    """The protocol candidates with no single sealed baseline."""
    counters: TrialCounters
    chain_head_sha256: str
    horizon_seconds: int | None
    """The registered strategy's declared holding horizon; ``None`` if it cannot be read."""
    horizon_reason: str | None = None
    """Why the horizon could not be read: the strategy is unregistered or its version differs."""


def horizon_of(protocol: TrialProtocol) -> tuple[int | None, str | None]:
    """``(horizon_seconds, None)`` of the strategy the protocol names, or ``(None, reason)``.

    Read from the strategy registry's own object (no run is constructed). Unknown when
    the strategy is not registered or its version is not the one the protocol declares:
    the code then is not what ran, so its horizon is not evidence of the run's. The reason
    says which.
    """

    try:
        strategy = registered(protocol.strategy_id)
    except ConfigurationError:
        return None, f"strategy {protocol.strategy_id} is not registered"
    if strategy.version != protocol.strategy_version:
        return None, (
            f"registered strategy {protocol.strategy_id} is version {strategy.version}, "
            f"not the protocol's {protocol.strategy_version}"
        )
    return strategy.horizon_seconds, None


def _optional(
    sealed: Sequence[Sealed], trial_id: str, sizing: SizingMode, multiplier: Decimal | None
) -> Sealed | None:
    found = [
        item
        for item in sealed
        if item[1].sizing is sizing
        and is_research_run(item[1])
        and (multiplier is None or item[1].result.cost_model.stress_multiplier == multiplier)
    ]
    if len(found) > 1:
        level = "" if multiplier is None else f" at {multiplier}x"
        raise ScenarioEvidenceError() from ValueError(
            f"trial {trial_id} has {len(found)} {sizing.value} bundles{level}; at most one is read"
        )
    return found[0] if found else None


def read_validation_inputs(
    trial_id: str,
    ledger: PostgresTrialLedger,
    store: EvidenceStore,
    *,
    ignore: frozenset[LedgerEventType] = frozenset(),
) -> ValidationInputs:
    """Read a trial's sealed runs, its protocol's other candidates, and the chain's state.

    ``ignore`` names event types left out of the chain this reads, so the head it records
    is the last row of any other type. ``decide`` ignores its own two event types: the
    chain it appends to is then not part of the evidence it was made on. Nothing here
    counts those types, so the counters are the same either way.
    """

    # One read of the chain: the counters, the head and the protocol describe the same chain.
    events, head = ledger.snapshot(ignore)
    if head is None:
        raise ScenarioEvidenceError() from ValueError("the chain holds no event")
    protocol = registered_protocol(events, trial_id)
    sealed = sealed_bundles(ledger.events_for(trial_id), store.read)
    baseline = sealed_baseline(sealed, trial_id)
    candidates: dict[str, Sealed] = {trial_id: baseline}
    excluded: list[str] = []
    for spec in protocol.candidates:
        if spec.trial_id == trial_id:
            continue
        try:
            candidates[spec.trial_id] = sealed_baseline(
                sealed_bundles(ledger.events_for(spec.trial_id), store.read), spec.trial_id
            )
        except ScenarioEvidenceError:
            excluded.append(spec.trial_id)
    stressed: dict[Decimal, Sealed] = {}
    for level in declared_grid(protocol):
        found = (
            None if level == 1 else _optional(sealed, trial_id, SizingMode.CONSTANT_NOTIONAL, level)
        )
        if found is not None:
            stressed[level] = found
    horizon = horizon_of(protocol)
    return ValidationInputs(
        trial_id=trial_id,
        protocol=protocol,
        baseline=baseline,
        compounding=_optional(sealed, trial_id, SizingMode.COMPOUNDING, None),
        stressed=stressed,
        candidates=candidates,
        excluded=tuple(excluded),
        counters=trial_counters(events),
        chain_head_sha256=head,
        horizon_seconds=horizon[0],
        horizon_reason=horizon[1],
    )


def _is_mark_to_market(bundle: EvidenceBundle) -> bool:
    return bundle.return_series_basis is ReturnSeriesBasis.MARK_TO_MARKET


def statistical_evidence(inputs: ValidationInputs) -> StatisticalEvidence:
    """Every measurement over ``inputs``. Pure; raises ``StatisticalInputError`` or
    ``ScenarioEvidenceError`` when the evidence is not a usable input."""

    trial_id, protocol = inputs.trial_id, inputs.protocol
    digest, bundle = inputs.baseline
    others = [item for item in (inputs.compounding, *inputs.stressed.values()) if item is not None]
    refuse_reportable(trial_id, others)
    if inputs.candidates.get(trial_id) != inputs.baseline:
        raise refusal("the trial's own baseline is not among the ranked candidates")
    for candidate_id, entry in inputs.candidates.items():
        refuse_reportable(candidate_id, [entry])
        refuse_unfaithful(candidate_id, protocol, entry[1])
    if inputs.compounding is not None:
        refuse_unfaithful(trial_id, protocol, inputs.compounding[1])
    for level, (_, stressed_bundle) in inputs.stressed.items():
        refuse_unfaithful(trial_id, protocol, stressed_bundle, level)

    # Refuses a bundle that is not a usable series, or a policy the series cannot honour.
    report = splits_report(trial_id=trial_id, protocol=protocol, sealed=inputs.baseline)
    series = ReturnSeries.from_bundle(bundle, digest)
    trades = TradeSample.from_bundle(bundle)
    policy = protocol.validation
    digests, grade = (digest,), series.promotion_grade

    folds = cpcv_folds(series.days, policy.cpcv_folds)
    splits = cpcv_splits(policy.cpcv_folds)
    path_samples: list[TradeSample] = []
    path_drawdowns: list[Measurement] = []
    for path in cpcv_paths(policy.cpcv_folds):
        kept, _ = path_trade_sample(
            trades,
            series.days,
            folds,
            splits,
            path,
            purge_days=report.purge_days,
            embargo_days=report.embargo_days,
        )
        path_samples.append(trade_subset(trades, kept.tolist()))
        path_drawdowns.append(
            path_drawdown(
                path_return_series(series, folds, path),
                name=f"max_drawdown_cpcv_path_{path.index}",
            )
        )

    ranked: dict[str, TradeSample] = {}
    ranked_digests: list[str] = []
    sharpes: list[float] = []
    all_mark_to_market = True
    for candidate_id in sorted(inputs.candidates):
        candidate_digest, candidate = inputs.candidates[candidate_id]
        candidate_series = ReturnSeries.from_bundle(candidate, candidate_digest)
        if candidate_series.days != series.days:
            raise refusal(f"candidate {candidate_id} was sealed over different days")
        ranked[candidate_id] = TradeSample.from_bundle(candidate)
        ranked_digests.append(candidate_digest)
        all_mark_to_market = all_mark_to_market and candidate_series.promotion_grade
        found = moments(candidate_series.values)
        if isinstance(found, Moments):
            sharpes.append(found.sharpe)

    horizon_days = (
        None if inputs.horizon_seconds is None else -(-inputs.horizon_seconds // SECONDS_PER_DAY)
    )
    spec_sha256, attempt_id = bundle.spec_sha256, bundle.attempt_id
    constant = float(policy.bootstrap_c)

    wfa_folds = (
        Measurement.defined(
            "wfa_folds",
            len(report.wfa.folds),
            evidence_sha256=digests,
            basis_is_mark_to_market=grade,
        )
        if report.wfa.folds
        else Measurement.undefined(
            "wfa_folds",
            report.wfa.undefined_reason or "no walk-forward fold exists",
            evidence_sha256=digests,
            basis_is_mark_to_market=grade,
        )
    )

    if inputs.compounding is None:
        compounding = Measurement.undefined(
            "max_drawdown_compounding",
            "no compounding run is sealed",
            evidence_sha256=digests,
            basis_is_mark_to_market=grade,
        )
    else:
        compounding_digest, compounding_bundle = inputs.compounding
        compounding = equity_drawdown(
            compounding_bundle.mark_to_market,
            name="max_drawdown_compounding",
            evidence_sha256=compounding_digest,
            basis_is_mark_to_market=_is_mark_to_market(compounding_bundle),
        )

    scenarios: list[ScenarioMeasurement] = []
    for level in declared_grid(protocol):
        if level == 1:
            continue
        found_run = inputs.stressed.get(level)
        scenarios.append(
            ScenarioMeasurement(
                multiplier=level,
                evidence_sha256=None if found_run is None else found_run[0],
                expectancy=scenario_expectancy(
                    None if found_run is None else TradeSample.from_bundle(found_run[1]),
                    level,
                    evidence_sha256=digests if found_run is None else (found_run[0],),
                    basis_is_mark_to_market=grade
                    if found_run is None
                    else _is_mark_to_market(found_run[1]),
                ),
            )
        )

    return StatisticalEvidence(
        trial_id=trial_id,
        spec_sha256=spec_sha256,
        attempt_id=attempt_id,
        basis_is_mark_to_market=grade,
        evidence=EvidenceDigests(
            baseline=digest,
            compounding=None if inputs.compounding is None else inputs.compounding[0],
            pbo_candidates=tuple(ranked_digests),
            chain_head=inputs.chain_head_sha256,
        ),
        counters=inputs.counters,
        first_day=series.days[0],
        last_day=series.days[-1],
        series_days=len(series.days),
        purge_days=report.purge_days,
        embargo_days=report.embargo_days,
        cpcv_folds=policy.cpcv_folds,
        wfa_folds=wfa_folds,
        psr=psr(series),
        sharpe_annualised=sharpe_annualised(series),
        dsr=dsr(
            series,
            trials=inputs.counters,
            horizon_days=horizon_days,
            chain_head_sha256=inputs.chain_head_sha256,
            candidate_sharpes=sharpes,
            horizon_reason=inputs.horizon_reason,
        ),
        pbo=pbo(
            ranked,
            inputs.excluded,
            series.days,
            folds,
            splits,
            purge_days=report.purge_days,
            embargo_days=report.embargo_days,
            primary_metric=policy.primary_metric,
            evidence_sha256=ranked_digests,
            basis_is_mark_to_market=all_mark_to_market,
        ),
        bootstrap=bootstrap_mean(
            series,
            spec_sha256=spec_sha256,
            attempt_id=attempt_id,
            replicates=policy.bootstrap_replicates,
            c=constant,
        ),
        monte_carlo=drawdown_loss_probabilities(
            series,
            replicates=policy.bootstrap_replicates,
            block_length=block_length(len(series.values), constant),
            seed=mc_seed(spec_sha256, attempt_id),
        ),
        max_drawdown_baseline=equity_drawdown(
            bundle.mark_to_market,
            name="max_drawdown_baseline",
            evidence_sha256=digest,
            basis_is_mark_to_market=grade,
        ),
        max_drawdown_cpcv_paths=tuple(path_drawdowns),
        max_drawdown_compounding=compounding,
        cpcv_p5=cpcv_p5(
            path_samples,
            paths_differ=report.cpcv.paths_differ,
            closed_trades=len(trades),
            evidence_sha256=digests,
            basis_is_mark_to_market=grade,
        ),
        coverage=oos_coverage(
            path_samples,
            protocol.regimes.labels,
            closed_trades=len(trades),
            evidence_sha256=digests,
            basis_is_mark_to_market=grade,
        ),
        scenario_expectancy=tuple(scenarios),
        capacity=capacity_diagnostic(protocol),
    )
