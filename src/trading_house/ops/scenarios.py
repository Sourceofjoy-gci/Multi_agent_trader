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

from collections.abc import Callable, Collection, Mapping, Sequence
from decimal import Decimal
from typing import cast

from pydantic import NonNegativeInt

from trading_house.core.errors import ScenarioEvidenceError
from trading_house.core.values import CanonicalModel, NonEmptyStr
from trading_house.research.backtest.costs import CostModel
from trading_house.research.backtest.costs_attribution import CostAttribution
from trading_house.research.backtest.sizing import SizingMode
from trading_house.research.canonical import canonical_sha256
from trading_house.research.evidence import EvidenceBundle
from trading_house.research.trial_ledger import (
    CostAttributionStatus,
    EvidenceSealedPayload,
    LedgerEvent,
    LedgerEventType,
    LedgerRecord,
    PreregisteredPayload,
    TrialProtocol,
    TrialSpec,
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

    ``trial_id`` and ``spec_sha256`` name the candidate first, so a reader knows
    *whose* grid this is before seeing a figure, and ``declared_multipliers``
    carries the preregistration before ``scenarios`` carries the outcome. The
    order is a rendering property and nothing computes it, which is why the
    acceptance gate holds the prefix rather than depending on it.
    """

    trial_id: NonEmptyStr
    spec_sha256: NonEmptyStr
    declared_multipliers: tuple[Decimal, ...]
    declared_baseline_multiplier: Decimal
    """The protocol's own ``costs.baseline.stress_multiplier``, named rather than
    normalised away. The grid above is always relative to level 1, so a protocol
    declaring a stressed baseline is reported against unstressed costs; this field
    is where a reader is told the declaration said otherwise."""
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

    The baseline level is the literal ``1`` and not
    ``costs.baseline.stress_multiplier``, and that is a deliberate mismatch worth
    stating rather than a slip. A protocol may declare a baseline whose own
    ``stress_multiplier`` is not ``1`` -- nothing in ``CostModel`` or
    ``CostSpec`` forbids it -- but a *stress* grid is defined relative to
    unstressed costs, so a report that moved its baseline level would be
    reporting a grid nobody declared. ``_refuse_baseline_fidelity`` compares
    the level-1 scenario to the declared baseline with its multiplier set to
    ``1``, so a protocol declaring an odd baseline is compared on its money
    terms and refused on the one field that cannot be right.
    """

    return tuple(sorted({Decimal(1), *protocol.costs.stress_multipliers}))


def baseline_at(protocol: TrialProtocol, multiplier: Decimal) -> CostModel:
    """The protocol's declared baseline costs at one stress level.

    The one statement of "the declared baseline at level *m*": the run commands
    build their cost models from it and every fidelity check compares against it,
    so a level a command ran and a level a report checks cannot disagree about
    what the declaration was. Level ``1`` is the reference level; see
    ``declared_grid`` for why the baseline's own ``stress_multiplier`` is not used.
    """

    return protocol.costs.baseline.model_copy(update={"stress_multiplier": multiplier})


def declared_candidate(protocol: TrialProtocol, trial_id: str) -> TrialSpec | None:
    """The one candidate a protocol declares under this trial id, or ``None``.

    Exported rather than written twice, and that is the whole reason. Two
    callers need this expression and the second of them is the *identity check*:
    the orchestrator seals ``canonical_sha256`` of this candidate, and the report
    compares each bundle's declared digest against ``canonical_sha256`` of the
    same candidate. A future edit to one copy and not the other would make a
    correctly sealed grid fail at exit 19 — after three documents and nine events
    are already in the chain, which is the one-way door the orchestrator's option
    list exists to close. The soundness of the check is the claim that these agree,
    so there is one of them.

    Returns ``None`` rather than raising, because the two callers want different
    errors: the report is refusing evidence and the CLI boundary is refusing an
    operator's argument. Each raises its own.
    """

    for candidate in protocol.candidates:
        if candidate.trial_id == trial_id:
            return candidate
    return None


def registered_protocol(events: Sequence[LedgerEvent], trial_id: str) -> TrialProtocol:
    """The one protocol this trial was registered against, out of the chain.

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

    **Refuses rather than picks, if the chain names the trial twice.** Not a
    hypothetical: ``PostgresTrialLedger.register`` derives its event id from
    ``canonical_sha256`` of the protocol, so a protocol differing in a single byte
    is a different id and not a conflict, and nothing in Phase 8A stops a second
    registration from declaring a trial the first one already declared.
    First-match-wins would then pick silently -- and a grid sealed under the
    second registration would be reported against the first, which is a clean
    report describing the wrong experiment. The two registrations differ in
    fields none of the six checks read (``execution``, ``validation``,
    ``regimes``, ``holdout``), so nothing downstream would notice.

    Keyed on the **content digest, not on ``protocol_id``**, and that is the load
    bearing detail: ``protocol_id`` is a bare ``NonEmptyStr`` that nothing binds to
    what the protocol says, and re-registering an amended protocol under the same
    id is the natural way to amend, since that is what an id is for. Two such
    registrations carry the same ``protocol_id`` and would pass an id-keyed
    comparison -- the first-match defect again, for a case as reachable as the one
    a digest key catches. Since an identical protocol collapses to a single event
    on the way in, a content key cannot false-positive. The message names the
    digests rather than the ids for the same reason: the ids carry no authority.

    Refusing here leaves the fix where it belongs: the ledger's own surface, which
    is 8A's, not this read.
    """

    found: list[TrialProtocol] = []
    for event in events:
        if not isinstance(event.payload, PreregisteredPayload):
            continue
        protocol = event.payload.protocol
        if declared_candidate(protocol, trial_id) is not None:
            found.append(protocol)
    if not found:
        raise ScenarioEvidenceError() from ValueError(
            f"trial {trial_id} has no preregistered protocol"
        )
    digests = {canonical_sha256(protocol) for protocol in found}
    if len(digests) > 1:
        raise ScenarioEvidenceError() from ValueError(
            f"trial {trial_id} is declared by {len(found)} registrations that are not "
            f"the same protocol: {sorted(digests)}"
        )
    return found[0]


def sealed_bundles(
    records: Sequence[LedgerRecord], read: Callable[[str], EvidenceBundle]
) -> list[tuple[str, EvidenceBundle]]:
    """Every bundle a trial's chain rows seal, paired with the digest that names it.

    ``records`` is ``events_for(trial_id)``; ``read`` is ``EvidenceStore.read``,
    which re-verifies each document's bytes on the way in. The one scan shared by
    the reports and the pre-flight below, so what a command refuses before writing
    is read exactly as what a report later reads.
    """

    sealed: list[tuple[str, EvidenceBundle]] = []
    for record in records:
        if record.event_type is not LedgerEventType.EVIDENCE_SEALED:
            continue
        digest = EvidenceSealedPayload.model_validate(record.event_json["payload"]).evidence_sha256
        sealed.append((digest, read(digest)))
    return sealed


def refuse_edited_protocol(registered: TrialProtocol, supplied: TrialProtocol) -> None:
    """The ``--protocol`` file must be the registered protocol, byte for canonical byte.

    Names both digests, because the operator's next move is to diff the two.
    """

    registered_digest, supplied_digest = canonical_sha256(registered), canonical_sha256(supplied)
    if registered_digest != supplied_digest:
        raise ScenarioEvidenceError() from ValueError(
            f"the --protocol file is {supplied_digest}; the registered protocol is "
            f"{registered_digest}"
        )


def refuse_other_attempts(
    sealed: Sequence[tuple[str, EvidenceBundle]],
    *,
    sizing: SizingMode,
    allowed: Callable[[Decimal], str | None],
) -> None:
    """No level this run would seal is already sealed under a different attempt id.

    ``allowed`` maps a bundle's cost multiplier to the one attempt id this run
    would use there, or ``None`` for a level it does not seal. A level already
    sealed under *that* attempt id is not refused here: ``sealed_levels`` names it
    and the caller skips it, because re-running it would derive a different bundle
    (8B3 moved the stressed levels' ``run_id`` and provenance options are part of
    the bytes) and seal a second document at a level the report refuses for good.
    A level sealed under any other attempt id is refused before a write for the
    same reason.
    """

    for _, bundle in sealed:
        if bundle.sizing is not sizing:
            continue
        multiplier = bundle.result.cost_model.stress_multiplier
        attempt_id = allowed(multiplier)
        if attempt_id is not None and bundle.attempt_id != attempt_id:
            raise ScenarioEvidenceError() from ValueError(
                f"{multiplier}x is already sealed as {sizing.value} under attempt "
                f"{bundle.attempt_id}; this run would seal it under {attempt_id}"
            )


def sealed_levels(
    sealed: Sequence[tuple[str, EvidenceBundle]],
    *,
    sizing: SizingMode,
    allowed: Callable[[Decimal], str | None],
) -> dict[Decimal, str]:
    """The levels this run has nothing to do at: sealed already, under its own attempt id.

    Maps each such level's multiplier to the evidence digest the chain names for
    it. The caller runs nothing at these levels -- no start event, no simulation,
    no seal -- and reports the existing digest, which is what makes a retry a true
    no-op. Call ``refuse_other_attempts`` first: it refuses the levels this skips
    would otherwise have to disagree about.
    """

    return {
        bundle.result.cost_model.stress_multiplier: digest
        for digest, bundle in sealed
        if bundle.sizing is sizing
        and allowed(bundle.result.cost_model.stress_multiplier) == bundle.attempt_id
    }


def started_attempts(records: Sequence[LedgerRecord]) -> set[str]:
    """Every attempt id the chain holds a start event for, under one trial."""

    return {
        record.attempt_id
        for record in records
        if record.event_type is LedgerEventType.EXECUTION_STARTED and record.attempt_id is not None
    }


def refuse_reused_attempts(
    started: Collection[str],
    sealed: Sequence[tuple[str, EvidenceBundle]],
    *,
    running: Mapping[Decimal, str],
    unsealed_retry: bool,
) -> None:
    """No attempt id this run starts is one the trial has already started.

    ``running`` is the level-to-attempt map of what this run will actually start
    (``sealed_levels`` already removed). A start event is content-derived from the
    trial and the attempt id alone, so reusing an id appends nothing and the audit
    count does not rise for a run that really happened -- the count is what the
    deflation denominator divides by.

    ``unsealed_retry`` allows an id that was started but never sealed anywhere: the
    orphan a failed run leaves, which a retry under the same id legitimately
    completes. With it off, any started id is refused.
    """

    sealed_ids = {bundle.attempt_id for _, bundle in sealed}
    for multiplier, attempt_id in running.items():
        if attempt_id in started and (not unsealed_retry or attempt_id in sealed_ids):
            raise ScenarioEvidenceError() from ValueError(
                f"attempt {attempt_id} is already started for this trial; "
                f"the {multiplier}x run would not add an audit attempt under it"
            )


def required_candidate(protocol: TrialProtocol, trial_id: str) -> TrialSpec:
    """The one candidate a protocol declares, or a refusal naming the protocol.

    ``declared_candidate`` returns ``None`` because its two callers want
    different errors; this is the report's half. ``_refuse_identity`` cannot fall
    back to the first candidate or to the bundle's own, since "which candidate was
    this grid registered as" is precisely the question the check answers.
    """

    candidate = declared_candidate(protocol, trial_id)
    if candidate is None:
        raise ScenarioEvidenceError() from ValueError(
            f"protocol {protocol.protocol_id} declares no {trial_id}"
        )
    return candidate


def scenario_report(
    *, trial_id: str, protocol: TrialProtocol, sealed: Sequence[tuple[str, EvidenceBundle]]
) -> ScenarioReport:
    """Check a candidate's sealed scenarios against its declared grid, and report.

    ``sealed`` pairs each bundle with the evidence digest the chain's
    ``EVIDENCE_SEALED`` event names for it. The pairing is the caller's because
    the chain is where the digest lives, and taking the documents alone would
    leave the report unable to say which sealed file any of its numbers came
    from.

    Bundles sealed under compounding sizing are ignored, not refused: the grid is
    the constant-notional set, and a compounding rerun of the same trial is a
    different experiment. A protocol declaring a stressed baseline is reportable;
    the declaration is carried as ``declared_baseline_multiplier``.

    Six checks, in this order, so an incomplete candidate is refused for the
    first reason that applies rather than for a later one it also happens to
    break:

    1. **Completeness** — every declared multiplier present exactly once.
    2. **Attribution** — every scenario carries a ``COMPLETE`` ``CostSummary``
       and a ``cost_attribution``, because the four separable terms and
       ``market_pnl`` are read from them and a ``PARTIAL`` summary has two of
       them as ``None``.
    3. **Baseline fidelity** — the ``1.0`` scenario's ``cost_model`` equals the
       protocol's ``CostSpec.baseline`` at level 1: every declared term, with
       the multiplier normalised. ``CostSpec`` pins ``stress_multipliers`` but
       says nothing about the baseline's own multiplier, so a protocol may declare
       a baseline of ``1.5``; comparing verbatim would make that registration
       permanently unreportable, and a registration cannot be amended after the
       fact. The declaration is not hidden by the normalisation: the report names
       it as ``declared_baseline_multiplier`` — see ``_refuse_baseline_fidelity``
       and ``declared_grid``.
    4. **Scenario fidelity** — each stressed scenario differs from the baseline
       in ``stress_multiplier`` and nothing else.
    5. **Window fidelity** — every scenario's ``result.start``/``end`` equals the
       window ``protocol.data`` declared.
    6. **Identity** — one candidate: same ``bars_seen`` and the same ordered
       ``proposal_id``s across the set; every bundle names the ``trial_id`` the
       report was asked about; and ``strategy_id``, ``strategy_version`` and
       ``spec_sha256`` match the *protocol's*, not merely each other's, since
       three runs agreeing with each other is not what makes them this
       candidate's runs. The digest is cross-checkable because the report holds
       the sealed protocol and ``canonical_sha256`` over a ``TrialSpec`` is
       deterministic — the same expression the orchestrator computes — so this is
       arithmetic over two sealed documents, not a question of anybody's honesty.

    Check 6 pins the trade *sequence* and says nothing about prices, because the
    prices must differ: scaling the spread is the stress, and 8B2a measured
    404.4 -> 606.6 of it across the same twelve trades. A report that pinned
    either the prices or the sequence as equal would be wrong in one direction
    or the other.
    """

    # Only constant-notional bundles are the grid (8B3, C-5). A compounding rerun
    # of the same trial is sealed at the same multiplier and would otherwise read
    # as a level sealed twice; it is ignored here, and ``compounding_report`` is
    # the read that consumes it.
    sealed = [(d, b) for d, b in sealed if b.sizing is SizingMode.CONSTANT_NOTIONAL]
    refuse_reportable(trial_id, sealed)

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
    _refuse_identity(trial_id, protocol, bundles)

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
        # From the first bundle, and that is now a fact rather than a fallback:
        # ``_refuse_identity`` has established that this digest equals
        # ``canonical_sha256`` of the protocol's own ``TrialSpec``, so the
        # evidence and the registration agree and either would print the same
        # thing. Reading it from the protocol instead would be a second, weaker
        # source for a field the check has just proven.
        spec_sha256=bundles[0].spec_sha256,
        declared_multipliers=grid,
        declared_baseline_multiplier=protocol.costs.baseline.stress_multiplier,
        scenarios=scenarios,
        degradations=degradations,
    )


def refuse_reportable(trial_id: str, sealed: Sequence[tuple[str, EvidenceBundle]]) -> None:
    """The two caller's inputs are things a report may name.

    Every other refusal in this module is a check on the *evidence*; this one is
    on the two values that arrive from outside it -- the trial the operator asked
    about and the digests the chain paired with the documents. Both land in
    ``NonEmptyStr`` fields, and a ``strict`` model answers an empty string with a
    ``ValidationError`` rather than with ``ScenarioEvidenceError``. That is the
    wrong class for a boundary: a ``ValidationError`` echoes the offending
    value, and the whole reason this module raises a typed error with an opaque
    ``public_message`` is that a failed report does not narrate its inputs back
    to whoever asked for one.

    In practice ``Task 3``'s caller supplies chain-held digests and a
    ``trial_id`` the chain just matched, so neither can be empty. This is the
    boundary being closed rather than a failure anyone has observed, and it is
    cheap now and not cheap later -- once a caller can reach this path, a
    ``ValidationError`` escaping it reads as an internal fault to the catch-all
    and answers the operator with a correlation id.
    """

    if not trial_id.strip():
        raise ScenarioEvidenceError() from ValueError("a report must name the trial it is about")
    for evidence_sha256, bundle in sealed:
        if not evidence_sha256.strip():
            raise ScenarioEvidenceError() from ValueError(
                f"a sealed scenario of trial {trial_id} carries no digest to name it by"
            )
        if bundle.trial_id != trial_id:
            # A pairing the chain could not produce -- ``events_for`` only returns
            # rows carrying this trial's id -- so it means a caller assembled the
            # set by hand, and every number below would be attributed to a
            # candidate one of them does not belong to. Refused here, before
            # ``_refuse_identity``, so the message names the substitution rather
            # than the disagreement it causes.
            raise ScenarioEvidenceError() from ValueError(
                f"trial {trial_id} was reported with a scenario sealed under {bundle.trial_id}"
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
    """The baseline scenario's declared costs are the protocol's, exactly.

    Field for field rather than term for term, because a baseline whose
    ``triple_swap_weekday`` or ``slippage_points_per_side`` drifted is a
    different baseline and a report that compared only the money terms would
    call it the declared one. ``CostModel``'s own equality is the whole of the
    check, so a term added to the model later is compared for free.

    Compared against the baseline *at level 1* rather than against
    ``costs.baseline`` as declared, and the two are not the same thing.
    ``CostSpec.stress_grid_is_exact`` pins ``stress_multipliers`` to exactly
    ``{1.5, 2}`` and says nothing about ``baseline.stress_multiplier``, which
    ``CostModel`` requires only to be positive. A protocol that declared a
    baseline of ``1.5`` is a model-accepted registration, and comparing its
    level-1 scenario to that declaration would refuse a candidate whose grid
    *is* the grid that was preregistered -- with a message blaming the sealed
    scenarios, and with no remedy, because a registration cannot be amended
    after the fact. Substituting the level makes the check say what it means:
    every scenario, the baseline one included, is the declared baseline at its
    own multiplier.
    """

    declared = baseline_at(protocol, Decimal(1))
    sealed = bundles[0].result.cost_model
    if sealed != declared:
        raise ScenarioEvidenceError() from ValueError(
            f"the 1.0 scenario declares {sealed}; the protocol's baseline at level 1 is {declared}"
        )


def _refuse_scenario_fidelity(protocol: TrialProtocol, bundles: Sequence[EvidenceBundle]) -> None:
    """Every stressed scenario is the baseline at its own multiplier, and nothing else.

    Written as a copy-then-replace rather than as a list of the five fields that
    must match, because "the only difference is the multiplier" is the property
    and a hand-written field list is a second statement of ``CostModel``'s
    shape -- one that would keep passing, and quietly, if a sixth term were ever
    added. A new term is then compared here for free.
    """

    for bundle in bundles[1:]:
        multiplier = bundle.result.cost_model.stress_multiplier
        expected = baseline_at(protocol, multiplier)
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


def _refuse_identity(
    trial_id: str, protocol: TrialProtocol, bundles: Sequence[EvidenceBundle]
) -> None:
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

    The expected record is seeded from the first bundle and then overridden from
    the *protocol* -- ``trial_id``, ``strategy_id``, ``strategy_version`` and
    ``spec_sha256`` -- rather than from that first bundle. Everything else here is
    internal agreement: three runs that all read the same bars, all traded the same
    sequence. Internal agreement is not what makes them *this candidate's* runs, and
    a record seeded from the evidence would let a set of three belonging to nobody
    in particular pass as self-consistent. The window check above is already
    against ``protocol.data``; this is the same statement about the run's identity.

    ``spec_sha256`` is the one that matters most, and it is cross-checkable here
    for a reason worth stating because it has been got wrong before. The ledger's
    ``spec_sha256`` column is the operator's *declared* value and 8A never
    vouched it -- but this function is not reading the column. It holds the sealed
    ``TrialProtocol``, ``canonical_sha256`` over a ``TrialSpec`` is deterministic,
    and ``research trial scenarios`` computes precisely that expression from the
    same document through the same ``declared_candidate`` lookup. So the digest is
    a fact about two sealed artifacts, not about anybody's honesty, and comparing
    it is arithmetic rather than trust. The machine that verified 8B1 and 8B2a has
    a trial in its chain right now counted as two effective specifications because
    a digest was typed off the wrong candidate; that drift produces a report whose
    ``spec_sha256`` names no registered candidate at all, which is why it is
    refused here rather than reported.

    A grid whose three levels all declare the *same* wrong digest is refused too:
    it agrees with itself, and agreement is not authority. What none of this can do
    is stop a wrong digest being *written* in the first place -- the ledger's
    unvouched column is 8A's surface, and the README says so in full. What 8B2b
    does is stop one being *read as a report*.

    ``attempt_id`` is deliberately *not* here. It names an attempt rather than a
    candidate, and the report prints it on every row; pinning it would assert
    that one attempt ran at three cost levels, which is a claim about the
    ledger's counting rather than about whether these three runs belong together.
    """

    expected = {
        **_identity(bundles[0]),
        "trial_id": trial_id,
        "strategy_id": protocol.strategy_id,
        "strategy_version": protocol.strategy_version,
        "spec_sha256": canonical_sha256(required_candidate(protocol, trial_id)),
    }
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
