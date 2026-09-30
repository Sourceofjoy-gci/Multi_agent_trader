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

from collections.abc import Sequence
from decimal import Decimal
from typing import cast

from pydantic import NonNegativeInt

from trading_house.core.errors import ScenarioEvidenceError
from trading_house.core.values import CanonicalModel, NonEmptyStr
from trading_house.ops.scenarios import _candidate, _refuse_reportable
from trading_house.research.backtest.mark import EquitySeries
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
    """Equity at the last observation of the bundle's mark-to-market series."""


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
    not the protocol candidate's, a different starting equity or a different
    number of bars read. The trade sequence and trade count may differ.
    """

    _refuse_reportable(trial_id, (constant, compounding))
    a, b = constant[1], compounding[1]
    _refuse_sizing(a, b)
    for bundle in (a, b):
        _refuse_unfaithful(trial_id, protocol, bundle)
    if a.result.firm_equity != b.result.firm_equity:
        raise ScenarioEvidenceError() from ValueError(
            f"the runs started from {a.result.firm_equity} and {b.result.firm_equity}"
        )
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


def _refuse_unfaithful(trial_id: str, protocol: TrialProtocol, bundle: EvidenceBundle) -> None:
    result = bundle.result
    declared = protocol.costs.baseline.model_copy(update={"stress_multiplier": Decimal(1)})
    if result.cost_model != declared:
        raise ScenarioEvidenceError() from ValueError(
            f"the {bundle.sizing.value} run declares {result.cost_model}; "
            f"the protocol's baseline at level 1 is {declared}"
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
    expected = canonical_sha256(_candidate(protocol, trial_id))
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
    )
