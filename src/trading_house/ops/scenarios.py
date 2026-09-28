"""The declared cost grid, the six checks a candidate's scenarios must pass,
and the report they produce.

Phase 8B2b. Umbrella 6.4 requires every preregistered candidate to be rerun at
1.0x, 1.5x and 2.0x costs, and 8B2a sealed the per-trade attribution each run
produces. This module is the read over those three sealed bundles.

The grid is taken from the protocol **as the chain holds it**, recovered from the
``PREREGISTERED`` event, rather than from a file the operator names. A report
that validated a hand-supplied protocol would be checking the operator's copy
rather than the record, and the whole point of preregistration is that the
declared grid is the one that was fixed before any result existed.

Nothing here judges. The six checks below are all about whether the evidence
*is* what it claims to be, and all of them fail closed. Whether a candidate
survives its grid is 8D's question, with thresholds fixed in advance; answering
it here would fix a threshold after seeing how the numbers came out.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import cast

from pydantic import NonNegativeInt

from trading_house.core.errors import ScenarioEvidenceError
from trading_house.core.values import CanonicalModel, NonEmptyStr
from trading_house.research.backtest.costs_attribution import CostAttribution
from trading_house.research.evidence import EvidenceBundle
from trading_house.research.trial_ledger import (
    CostAttributionStatus,
    LedgerEvent,
    PreregisteredPayload,
    TrialProtocol,
)


class ScenarioTotals(CanonicalModel):
    """One scenario's sealed figures, read from its own bundle and never
    recomputed from anything else.

    ``swap`` is signed and is reported signed. A charge is negative and a credit
    positive, and the four cost terms do not all move the same way under stress
    on a carry-earning candidate, so a single "total cost" would hide a credit
    inside a positive-looking number. The relation is
    ``net = market - spread - slippage - commission + swap``.
    """

    multiplier: Decimal
    attempt_id: NonEmptyStr
    evidence_sha256: NonEmptyStr
    """The digest the chain's ``EVIDENCE_SEALED`` event names for this
    scenario's document -- which sealed file this row is talking about. Taken
    from the chain rather than recomputed, because the store is what holds that
    name and re-deriving it would answer a different question."""
    source_result_sha256: NonEmptyStr
    """The digest of the result the bundle carries inline. Distinct from the
    field above and reported beside it: one names the document, the other names
    the run inside it, and a reader verifying the report needs both to get back
    from a number here to the bytes that produced it."""
    trades: NonNegativeInt
    market_pnl: Decimal
    spread_cost: Decimal
    slippage_cost: Decimal
    commission: Decimal
    swap: Decimal
    net_pnl: Decimal


class ScenarioDegradation(CanonicalModel):
    """How one stressed level differs from the baseline, term by term.

    Deltas, not levels, and never a ratio: at 2.0x a candidate's expectancy may
    cross zero, and a percentage of a number that changed sign says nothing.
    """

    multiplier: Decimal
    net_pnl_delta: Decimal
    market_pnl_delta: Decimal
    spread_cost_delta: Decimal
    slippage_cost_delta: Decimal
    commission_delta: Decimal
    swap_delta: Decimal


class ScenarioReport(CanonicalModel):
    """What the declared grid asked for, and what the sealed evidence says.

    ``declared_multipliers`` is first in the field order and in every rendering,
    so a reader meets the preregistration before the outcome.
    """

    trial_id: NonEmptyStr
    spec_sha256: NonEmptyStr
    declared_multipliers: tuple[Decimal, ...]
    scenarios: tuple[ScenarioTotals, ...]
    degradations: tuple[ScenarioDegradation, ...]


def declared_grid(protocol: TrialProtocol) -> tuple[Decimal, ...]:
    """The multipliers this protocol preregistered, ascending, baseline first.

    Read from the protocol rather than written down: ``CostSpec`` already holds
    the validator that pins the stressed levels to exactly ``{1.5, 2}``, and a
    constant here would be a second statement of a rule that has one home.

    ``CostSpec.stress_grid_is_exact`` means the union is belt-and-braces rather
    than the rule -- the stressed levels are always ``{1.5, 2}``. It is written
    as a union anyway because "there is always a baseline at 1" is *this*
    function's claim, and ``CostSpec``'s validator says nothing about whether a
    baseline exists.
    """

    return tuple(sorted({Decimal(1), *protocol.costs.stress_multipliers}))


def registered_protocol(events: Sequence[LedgerEvent], trial_id: str) -> TrialProtocol:
    """The protocol this trial was registered against, out of the chain.

    Recovered from the ``PREREGISTERED`` event's payload rather than from
    anything the caller supplies, so the grid the report checks is the grid that
    was sealed. A trial that was never registered has no such event, and there is
    no honest answer for it.

    Takes the whole chain and not ``ledger.events_for(trial_id)``, which is the
    trap here. A protocol seals its entire candidate family inside one event, so
    the ``PREREGISTERED`` row's ``trial_id`` column is null and
    ``_TRIAL_EVENTS_SQL`` -- ``WHERE trial_id = %s`` -- never returns it. A
    candidate would be reported as unregistered while the ledger's own
    ``declares_trial`` says the opposite. ``replay()`` is the read that carries
    the registration, and it is what ``counters()`` already uses for the same
    reason.
    """

    for event in events:
        if not isinstance(event.payload, PreregisteredPayload):
            continue
        protocol = event.payload.protocol
        if any(candidate.trial_id == trial_id for candidate in protocol.candidates):
            return protocol
    raise ScenarioEvidenceError() from ValueError(f"trial {trial_id} has no preregistered protocol")


def scenario_report(
    *, trial_id: str, protocol: TrialProtocol, sealed: Sequence[tuple[str, EvidenceBundle]]
) -> ScenarioReport:
    """Check a candidate's sealed scenarios against its declared grid, and report.

    ``sealed`` pairs each bundle with the evidence digest the chain's
    ``EVIDENCE_SEALED`` event names for it. The pairing is the caller's because
    the chain is where the digest lives, and taking the documents alone would
    leave the report unable to say which sealed file any of its numbers came
    from.

    Six checks, in this order, so an incomplete candidate is refused for the
    first reason that applies rather than for a later one it also happens to
    break:

    1. **Completeness** — every declared multiplier present exactly once.
    2. **Attribution** — every scenario carries a ``COMPLETE`` ``CostSummary``
       and a ``cost_attribution``, because the four separable terms and
       ``market_pnl`` are read from them and a ``PARTIAL`` summary has two of
       them as ``None``.
    3. **Baseline fidelity** — the ``1.0`` scenario's ``cost_model`` equals the
       protocol's ``CostSpec.baseline`` exactly.
    4. **Scenario fidelity** — each stressed scenario differs from the baseline
       in ``stress_multiplier`` and nothing else.
    5. **Window fidelity** — every scenario's ``result.start``/``end`` equals the
       window ``protocol.data`` declared.
    6. **Identity** — one candidate: same ``trial_id``, ``spec_sha256``,
       ``strategy_id``/``strategy_version``, ``bars_seen``, and the same ordered
       ``proposal_id``s.

    Check 6 pins the trade *sequence* and says nothing about prices, because the
    prices must differ: scaling the spread is the stress, and 8B2a measured
    404.4 -> 606.6 of it across the same twelve trades. A report that pinned
    either the prices or the sequence as equal would be wrong in one direction
    or the other.
    """

    grid = declared_grid(protocol)
    by_multiplier: dict[Decimal, list[tuple[str, EvidenceBundle]]] = {}
    for evidence_sha256, bundle in sealed:
        by_multiplier.setdefault(bundle.result.cost_model.stress_multiplier, []).append(
            (evidence_sha256, bundle)
        )

    _refuse_completeness(grid, by_multiplier, trial_id)
    pairs = [by_multiplier[m][0] for m in grid]
    bundles = [bundle for _, bundle in pairs]
    _refuse_attribution(bundles)
    _refuse_baseline_fidelity(protocol, bundles)
    _refuse_scenario_fidelity(protocol, bundles)
    _refuse_window_fidelity(protocol, bundles)
    _refuse_identity(trial_id, bundles)

    scenarios = tuple(
        _totals(m, bundle, evidence_sha256)
        for m, (evidence_sha256, bundle) in zip(grid, pairs, strict=True)
    )
    baseline = scenarios[0]
    degradations = tuple(
        ScenarioDegradation(
            multiplier=s.multiplier,
            net_pnl_delta=baseline.net_pnl - s.net_pnl,
            market_pnl_delta=s.market_pnl - baseline.market_pnl,
            spread_cost_delta=s.spread_cost - baseline.spread_cost,
            slippage_cost_delta=s.slippage_cost - baseline.slippage_cost,
            commission_delta=s.commission - baseline.commission,
            swap_delta=s.swap - baseline.swap,
        )
        for s in scenarios[1:]
    )
    return ScenarioReport(
        trial_id=trial_id,
        # Taken from the first bundle because ``_refuse_identity`` has already
        # established that all three agree; a report that read it from the
        # protocol instead would be asserting a cross-check the chain does not
        # make. The candidate's ``TrialSpec`` has no digest in the ledger -- the
        # ``spec_sha256`` column is the operator's declared value, unvouched by
        # 8A -- so this reports what the evidence says, not what was intended.
        spec_sha256=bundles[0].spec_sha256,
        declared_multipliers=grid,
        scenarios=scenarios,
        degradations=degradations,
    )


def _refuse_completeness(
    grid: tuple[Decimal, ...],
    by_multiplier: Mapping[Decimal, Sequence[tuple[str, EvidenceBundle]]],
    trial_id: str,
) -> None:
    """Every declared multiplier present exactly once, and nothing else present.

    A set comparison on its own would accept a level sealed twice -- two
    attempts at ``1.0x`` are two audit attempts, and which of them the report
    should read is not a question this function can answer -- so the counts are
    compared as well as the names. All three sets are named in the refusal
    because each is a different defect with a different remedy: a level never
    run, a level run twice, and a level run that nobody declared.
    """

    missing = [m for m in grid if m not in by_multiplier]
    repeated = sorted(m for m, entries in by_multiplier.items() if len(entries) > 1)
    undeclared = sorted(m for m in by_multiplier if m not in grid)
    if not (missing or repeated or undeclared):
        return
    raise ScenarioEvidenceError() from ValueError(
        f"trial {trial_id} declared the grid {grid} and sealed "
        f"{sorted(by_multiplier)}: missing {missing}, sealed more than once {repeated}, "
        f"never declared {undeclared}"
    )


def _refuse_attribution(bundles: Sequence[EvidenceBundle]) -> None:
    """Every scenario separates its costs, or the report refuses to name four terms.

    ``spread_cost`` and ``slippage_cost`` are ``None`` on a summary that is not
    ``COMPLETE`` -- that is the honest record of a run that could not separate
    them -- and reading a ``None`` as a zero would turn an unmeasured term into
    a flattering one. So the two halves are refused together rather than one
    and then the other: ``EvidenceBundle`` already couples them (a ``COMPLETE``
    summary must carry the split it aggregates, and only a ``COMPLETE`` summary
    may carry one), so no document can fail one and pass the other, and a
    two-branch refusal here would be two lines that can never both run.
    """

    for bundle in bundles:
        costs = bundle.costs
        if costs.status is not CostAttributionStatus.COMPLETE or bundle.cost_attribution is None:
            raise ScenarioEvidenceError() from ValueError(
                f"scenario {bundle.result.cost_model.stress_multiplier} sealed a "
                f"{costs.status.value} cost summary and "
                f"{'a' if bundle.cost_attribution is not None else 'no'} per-trade split; "
                "the report names four separable costs and will not read an unmeasured "
                "one as a zero"
            )


def _refuse_baseline_fidelity(protocol: TrialProtocol, bundles: Sequence[EvidenceBundle]) -> None:
    """The ``1.0`` scenario's declared costs are the protocol's baseline, exactly.

    Field for field rather than term for term, because a baseline whose
    ``triple_swap_weekday`` or ``stress_multiplier`` drifted is a different
    baseline and a report that compared only the money terms would call it the
    declared one. ``CostModel``'s own equality is the whole of the check, so a
    term added to the model later is compared for free.
    """

    declared = protocol.costs.baseline
    sealed = bundles[0].result.cost_model
    if sealed != declared:
        raise ScenarioEvidenceError() from ValueError(
            f"the 1.0 scenario declares {sealed}; the protocol's baseline is {declared}"
        )


def _refuse_scenario_fidelity(protocol: TrialProtocol, bundles: Sequence[EvidenceBundle]) -> None:
    """Every stressed scenario is the baseline at its own multiplier, and nothing else.

    Written as a copy-then-replace rather than as a list of the five fields that
    must match, because "the only difference is the multiplier" is the property
    and a hand-written field list is a second statement of ``CostModel``'s
    shape -- one that would keep passing, and quietly, if a sixth term were ever
    added. A new term is then compared here for free.
    """

    baseline = protocol.costs.baseline
    for bundle in bundles[1:]:
        multiplier = bundle.result.cost_model.stress_multiplier
        expected = baseline.model_copy(update={"stress_multiplier": multiplier})
        if bundle.result.cost_model != expected:
            raise ScenarioEvidenceError() from ValueError(
                f"scenario {multiplier} declares {bundle.result.cost_model}; "
                f"the protocol's baseline at that multiplier is {expected}"
            )


def _refuse_window_fidelity(protocol: TrialProtocol, bundles: Sequence[EvidenceBundle]) -> None:
    """Every scenario ran the window the protocol declared.

    Separate from the identity check below, and deliberately so: a candidate's
    three runs sharing one window says they are one replay, while each of them
    matching the *protocol* says it is the replay that was preregistered. A
    grid run over a window nobody declared measures something else at three
    different prices, and every number in the report would still reconcile.
    """

    declared = (protocol.data.start, protocol.data.end)
    for bundle in bundles:
        result = bundle.result
        if (result.start, result.end) != declared:
            raise ScenarioEvidenceError() from ValueError(
                f"scenario {result.cost_model.stress_multiplier} ran "
                f"[{result.start}, {result.end}]; the protocol declares "
                f"[{protocol.data.start}, {protocol.data.end}]"
            )


def _refuse_identity(trial_id: str, bundles: Sequence[EvidenceBundle]) -> None:
    """Every scenario in the grid is the reported candidate, run the reported way.

    One comparison against one record rather than a field-by-field ladder, and
    the record is the identity the grid is *supposed* to have -- the reported
    ``trial_id`` among it. Reading that field off the evidence instead would
    leave a set of three scenarios belonging to some other candidate perfectly
    consistent with itself and therefore reportable as this one's evidence,
    which is the substitution this check exists to catch.

    A mapping rather than a tuple so the refusal can name *which* field
    differs: a grid whose runs disagree about ``bars_seen`` and a grid carrying
    another candidate's bundle are different defects, and a message printing two
    six-field tuples would leave an operator to find that difference themselves.
    """

    expected = {**_identity(bundles[0]), "trial_id": trial_id}
    for bundle in bundles:
        found = _identity(bundle)
        differing = sorted(field for field, value in found.items() if value != expected[field])
        if differing:
            raise ScenarioEvidenceError() from ValueError(
                f"the grid reported for trial {trial_id} is not that candidate's, at scenario "
                f"{bundle.result.cost_model.stress_multiplier}: "
                + ", ".join(
                    f"{field} {found[field]!r} against {expected[field]!r}" for field in differing
                )
            )


def _identity(bundle: EvidenceBundle) -> dict[str, str]:
    """What must not move between a candidate's three scenarios, as text.

    ``proposal_id``s in result order, which pins the trade *sequence* and says
    nothing about prices: scaling the spread is the stress, so the prices must
    differ and the sequence must not. A report that pinned either one as equal
    would be wrong in one direction or the other.
    """

    result = bundle.result
    return {
        "trial_id": bundle.trial_id,
        "spec_sha256": bundle.spec_sha256,
        "strategy_id": result.strategy_id,
        "strategy_version": result.strategy_version,
        "bars_seen": str(result.bars_seen),
        "proposal_ids": ",".join(trade.proposal_id for trade in result.trades),
    }


def _totals(multiplier: Decimal, bundle: EvidenceBundle, evidence_sha256: str) -> ScenarioTotals:
    """One scenario's own sealed figures, read rather than recomputed.

    ``commission`` and ``swap`` come from the ``CostSummary``; so do
    ``spread_cost`` and ``slippage_cost``, which are ``None`` on a ``PARTIAL``
    summary and are only non-``None`` because ``_refuse_attribution`` has
    already required ``COMPLETE``. ``market_pnl`` has no result-level field at
    all -- it exists only on ``TradeCostAttribution`` -- so it is summed over
    the split, whose per-trade ``post_fill_gross`` check and whose parallelism
    to ``result.trades`` are the bundle's own validator's work, not this
    function's.

    Nothing is recomputed from anything. Every figure here is a field the bundle
    sealed and its own validators checked, or a digest the chain already holds;
    a report that re-derived them would be a second implementation of the
    engine, and a disagreement between the two would be unresolvable.
    """

    costs = bundle.costs
    attribution = bundle.cost_attribution
    # ``cast`` and not ``assert``. ``src/`` contains no ``assert`` statement
    # anywhere and this module should not be the first: an assert is stripped
    # under ``-O``, which would turn a narrowing aid into an ``AttributeError``
    # on ``None`` in an optimised run, and it raises something that is not this
    # repo's typed-error contract. The guarantee is real --
    # ``CostSummary.the_status_licenses_exactly_the_components_it_carries``
    # refuses a ``COMPLETE`` summary that omits either term -- so ``cast`` is the
    # honest way to tell mypy about a check that has already run, and it costs
    # nothing at runtime. ``cli.py`` uses ``cast`` throughout for the same job.
    return ScenarioTotals(
        multiplier=multiplier,
        attempt_id=bundle.attempt_id,
        evidence_sha256=evidence_sha256,
        source_result_sha256=bundle.source_result_sha256,
        trades=len(bundle.result.trades),
        market_pnl=sum(
            (t.market_pnl for t in cast(CostAttribution, attribution).trades), Decimal(0)
        ),
        spread_cost=cast(Decimal, costs.spread_cost),
        slippage_cost=cast(Decimal, costs.slippage_cost),
        commission=costs.commission,
        swap=costs.swap,
        net_pnl=bundle.result.net_pnl,
    )
