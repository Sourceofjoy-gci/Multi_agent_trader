"""A compounding rerun set beside its constant-notional baseline.

Phase 8B3. The compounding run is the same candidate on the same replay at the
declared baseline costs, sized from equity plus realized profit instead of from
initial capital. This module reports the two side by side and describes the
difference in final equity. It states facts about the evidence and nothing else.

Whether the two runs traded the same sequence is reported, not checked: with
re-based equity the same costs can change a size and therefore a trade, and
refusing on that would forbid the very difference the rerun exists to show. What
is checked is everything that makes the two runs one candidate on one replay,
because unlike the trade sequence those cannot legitimately differ.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
from fractions import Fraction
from typing import cast

from pydantic import NonNegativeInt

from trading_house.core.errors import ScenarioEvidenceError
from trading_house.core.values import CanonicalModel, NonEmptyStr
from trading_house.ops.scenarios import (
    baseline_at,
    is_research_run,
    refuse_reportable,
    required_candidate,
)
from trading_house.research.backtest.engine import BacktestRequest
from trading_house.research.backtest.mark import EquitySeries
from trading_house.research.backtest.result import BacktestResult
from trading_house.research.backtest.sizing import SizingMode
from trading_house.research.canonical import canonical_sha256
from trading_house.research.evidence import EvidenceBundle
from trading_house.research.trial_ledger import TrialProtocol


class CompoundingRun(CanonicalModel):
    """One run's sealed figures, read from its own bundle."""

    attempt_id: NonEmptyStr
    evidence_sha256: NonEmptyStr
    source_result_sha256: NonEmptyStr
    trades: NonNegativeInt
    net_pnl: Decimal
    final_equity: Decimal
    """Equity at the last observation of the bundle's mark-to-market series. It
    includes an unrealized mark when ``ends_flat`` is false."""
    ends_flat: bool
    """Whether the run ended with nothing open. False means ``final_equity`` carries
    an open position's unrealized mark and ``net_pnl`` does not."""


class CompoundingReport(CanonicalModel):
    """The constant-notional run and the compounding run of one candidate.

    ``final_equity_difference`` is compounding minus constant: a delta in account
    currency, never a ratio.
    """

    trial_id: NonEmptyStr
    spec_sha256: NonEmptyStr
    constant_notional: CompoundingRun
    compounding: CompoundingRun
    same_trade_sequence: bool
    """Whether the two runs closed trades with the same ordered ``proposal_id``s.
    It compares closed trades' proposal ids only: it says nothing about lots,
    fills or exits, nor about a position still open (and so discarded from the
    trade list) when a run ended."""
    final_equity_difference: Decimal


def compounding_report(
    *,
    trial_id: str,
    protocol: TrialProtocol,
    constant: tuple[str, EvidenceBundle],
    compounding: tuple[str, EvidenceBundle],
) -> CompoundingReport:
    """Report a compounding run against its constant-notional baseline.

    Each side pairs a bundle with the digest the chain names for it. Refuses, in
    order, when the pair is not one candidate on one replay: a blank trial or
    digest, a bundle of another trial, sizing modes that are not the two named,
    a missing equity series, costs other than the protocol's baseline at level 1,
    a window other than the protocol's, another strategy, a spec digest that is
    not the protocol candidate's, a replay input that differs between the two runs
    (``REPLAY_FIELDS``) or a different number of bars read. The trade sequence and
    trade count may differ.
    """

    refuse_reportable(trial_id, (constant, compounding))
    a, b = constant[1], compounding[1]
    _refuse_sizing(a, b)
    for bundle in (a, b):
        refuse_unfaithful(trial_id, protocol, bundle)
    refuse_other_replay(replay_inputs(a.result), b.result, what="the compounding run")
    if a.result.bars_seen != b.result.bars_seen:
        raise ScenarioEvidenceError() from ValueError(
            f"the runs read {a.result.bars_seen} and {b.result.bars_seen} bars"
        )

    base, comp = _run(*constant), _run(*compounding)
    return CompoundingReport(
        trial_id=trial_id,
        spec_sha256=a.spec_sha256,
        constant_notional=base,
        compounding=comp,
        same_trade_sequence=_sequence(a) == _sequence(b),
        final_equity_difference=comp.final_equity - base.final_equity,
    )


REPLAY_FIELDS = (
    "firm_equity",
    "exit_policy",
    "atr_period",
    "spread_window",
    "defective_bar_tolerance",
    "contract_sha256",
    "constitution_sha256",
    "instrument_id",
    "timeframe",
)
"""The ``BacktestResult`` fields a protocol does not own and a candidate's runs must
share, for them to be one candidate on one replay. Everything the protocol does own
(costs, window, strategy, specification) is checked against the protocol instead."""


def replay_inputs(result: BacktestResult) -> dict[str, object]:
    """What a sealed run says its replay inputs were, by ``REPLAY_FIELDS``."""

    return {field: getattr(result, field) for field in REPLAY_FIELDS}


def request_replay_inputs(
    request: BacktestRequest, *, contract_sha256: str, constitution_sha256: str
) -> dict[str, object]:
    """What a run built from ``request`` would record as its replay inputs.

    The same keys and the same values ``BacktestResult`` would hold for that run
    (the engine derives each one from the request, the loaded contract and the
    loaded constitution), so a pre-flight can compare a run that has not happened
    to one that has.
    """

    return {
        "firm_equity": request.firm_equity,
        "exit_policy": request.strategy.exit_policy(),
        "atr_period": request.atr_period,
        "spread_window": request.spread_window,
        "defective_bar_tolerance": Fraction(request.defective_bar_tolerance),
        "contract_sha256": contract_sha256,
        "constitution_sha256": constitution_sha256,
        "instrument_id": request.instrument_id,
        "timeframe": request.timeframe,
    }


def refuse_other_replay(
    expected: Mapping[str, object], result: BacktestResult, *, what: str
) -> None:
    """Refuse a run whose replay inputs differ from ``expected``, naming each field.

    The one predicate behind the report's "one candidate on one replay" and the
    commands' pre-flight, which is why they cannot drift: both compare exactly
    ``REPLAY_FIELDS``.
    """

    found = replay_inputs(result)
    differing = [f for f in REPLAY_FIELDS if found[f] != expected[f]]
    if differing:
        raise ScenarioEvidenceError() from ValueError(
            f"{what} differs in its replay inputs: "
            + ", ".join(f"{f} {found[f]!r} against {expected[f]!r}" for f in differing)
        )


def _only(
    sealed: Sequence[tuple[str, EvidenceBundle]],
    trial_id: str,
    sizing: SizingMode,
    multiplier: Decimal | None,
) -> tuple[str, EvidenceBundle]:
    found = [
        (d, b)
        for d, b in sealed
        if b.sizing is sizing
        and is_research_run(b)
        and (multiplier is None or b.result.cost_model.stress_multiplier == multiplier)
    ]
    if len(found) != 1:
        level = "" if multiplier is None else f" at {multiplier}x"
        raise ScenarioEvidenceError() from ValueError(
            f"trial {trial_id} has {len(found)} {sizing.value} bundles{level}; one is required"
        )
    return found[0]


def sealed_baseline(
    sealed: Sequence[tuple[str, EvidenceBundle]], trial_id: str
) -> tuple[str, EvidenceBundle]:
    """The trial's one constant-notional bundle at 1.0x, or a refusal."""

    return _only(sealed, trial_id, SizingMode.CONSTANT_NOTIONAL, Decimal(1))


def sealed_rerun(
    sealed: Sequence[tuple[str, EvidenceBundle]], trial_id: str
) -> tuple[str, EvidenceBundle]:
    """The trial's one compounding bundle, or a refusal."""

    return _only(sealed, trial_id, SizingMode.COMPOUNDING, None)


def _refuse_sizing(constant: EvidenceBundle, compounding: EvidenceBundle) -> None:
    if constant.sizing is not SizingMode.CONSTANT_NOTIONAL:
        raise ScenarioEvidenceError() from ValueError(
            f"the baseline run was sealed under {constant.sizing.value} sizing"
        )
    if compounding.sizing is not SizingMode.COMPOUNDING:
        raise ScenarioEvidenceError() from ValueError(
            f"the rerun was sealed under {compounding.sizing.value} sizing"
        )
    for bundle in (constant, compounding):
        if bundle.mark_to_market is None:
            raise ScenarioEvidenceError() from ValueError(
                f"the {bundle.sizing.value} run carries no equity series"
            )


def refuse_unfaithful(
    trial_id: str,
    protocol: TrialProtocol,
    bundle: EvidenceBundle,
    multiplier: Decimal = Decimal(1),
) -> None:
    """Refuse a bundle that is not the protocol's candidate on the protocol's terms.

    Costs equal the protocol's baseline at ``multiplier`` (level 1 unless a stressed
    run is being checked), the window is the protocol's, the strategy id and version
    are the protocol's, and the specification digest is the one of the protocol's
    candidate for ``trial_id``. Shared by the compounding report and ``validate``.
    """

    result = bundle.result
    declared = baseline_at(protocol, multiplier)
    if result.cost_model != declared:
        raise ScenarioEvidenceError() from ValueError(
            f"the {bundle.sizing.value} run declares {result.cost_model}; "
            f"the protocol's baseline at level {multiplier} is {declared}"
        )
    window = (protocol.data.start, protocol.data.end)
    if (result.start, result.end) != window:
        raise ScenarioEvidenceError() from ValueError(
            f"the {bundle.sizing.value} run ran [{result.start}, {result.end}]; "
            f"the protocol declares [{window[0]}, {window[1]}]"
        )
    if (result.strategy_id, result.strategy_version) != (
        protocol.strategy_id,
        protocol.strategy_version,
    ):
        raise ScenarioEvidenceError() from ValueError(
            f"the {bundle.sizing.value} run is strategy {result.strategy_id} "
            f"{result.strategy_version}; the protocol declares {protocol.strategy_id} "
            f"{protocol.strategy_version}"
        )
    expected = canonical_sha256(required_candidate(protocol, trial_id))
    if bundle.spec_sha256 != expected:
        raise ScenarioEvidenceError() from ValueError(
            f"the {bundle.sizing.value} run declares spec {bundle.spec_sha256}; "
            f"the protocol's candidate {trial_id} is {expected}"
        )


def _sequence(bundle: EvidenceBundle) -> tuple[str, ...]:
    return tuple(trade.proposal_id for trade in bundle.result.trades)


def _run(evidence_sha256: str, bundle: EvidenceBundle) -> CompoundingRun:
    series = cast(EquitySeries, bundle.mark_to_market)
    return CompoundingRun(
        attempt_id=bundle.attempt_id,
        evidence_sha256=evidence_sha256,
        source_result_sha256=bundle.source_result_sha256,
        trades=len(bundle.result.trades),
        net_pnl=bundle.result.net_pnl,
        final_equity=series.observations[-1].equity,
        ends_flat=series.is_flat,
    )
