"""The promotion judgement: holdout state, the nine gates, the decision, the report.

Phase 8D1. This is the first module in the repository that judges a candidate, and it is
**pure**: no ledger, no evidence store, no filesystem, no clock. Everything it reads has
already been read by ``ops/decide.py`` and arrives as an argument, so the same inputs give
the same outcome on every call and in every process. A gate that could read the chain
itself could answer differently twice.

Thresholds are **not arguments**. The numeric ones are the constants in
``research/validation/policy.py`` (fixed in 8C3 before any measurement existed) and the
protocol's own ``ValidationSpec``; ``policy_sha256`` digests every one of them, a report
embeds that digest, and ``evaluate_gates`` refuses inputs that name a different one than
the constants in force recompute to. A trial's first report pins its digest, so a threshold
that moves after a result exists is refused for that trial (umbrella 11.4: a policy change
needs a new trial).

Four statuses of knowledge, two kinds of block. ``UNAVAILABLE`` is evidence that is missing,
undefined or non-finite, or a precondition that does not exist (a locked holdout, a capacity
model). ``FAIL`` is a *measured* value on the wrong side of its threshold. Both block.
``PASS`` requires a defined, finite, correct-side measurement and nothing else.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal
from enum import Enum
from typing import Annotated, Literal, NoReturn, Self

from pydantic import Field, NonNegativeInt, ValidationError, model_validator

from trading_house.core.errors import PromotionRefusedError
from trading_house.core.values import CanonicalModel, FiniteFloat, NonEmptyStr
from trading_house.research.canonical import canonical_sha256
from trading_house.research.packages import PromotionStage, StrategyPackage, StrategySpec
from trading_house.research.trial_ledger import (
    CostAttributionStatus,
    EvidenceSealedPayload,
    GateDecidedPayload,
    HoldoutState,
    LedgerEvent,
    LegacyImportedPayload,
    PreregisteredPayload,
    RegistrationState,
    ReturnSeriesBasis,
    TrialProtocol,
    ValidationSpec,
)
from trading_house.research.validation.capacity import CapacityStatus
from trading_house.research.validation.evidence import StatisticalEvidence
from trading_house.research.validation.measurement import Measurement
from trading_house.research.validation.policy import (
    CPCV_P5_QUANTILE,
    DRAWDOWN_TOLERANCE,
    DSR_MINIMUM,
    MAX_DRAWDOWN,
    MC_POLICY_VERSION,
    MIN_OOS_TRADES,
    MIN_REGIMES,
    MIN_WFA_FOLDS,
    PBO_MAXIMUM,
)

GATE_COUNT = 9
HOLDOUT_GATE = 2
RESEARCH_GATES = frozenset({1, 3, 4, 5, 6, 7, 8, 9})
"""Every gate but the holdout's: §8.1 lets only a strategy that passes all of these open it."""

GATE_NAMES = {
    1: "wfa_complete",
    2: "locked_oos_expectancy",
    3: "dsr_minimum",
    4: "pbo_maximum",
    5: "cpcv_p5_expectancy",
    6: "bootstrap_lower_bound",
    7: "max_drawdown",
    8: "oos_coverage",
    9: "capacity_model",
}

_OPENING_STATES = frozenset({HoldoutState.OPENED, HoldoutState.CONSUMED})
_REGISTERED_AS_SPENT = frozenset(
    {HoldoutState.OPENED, HoldoutState.CONSUMED, HoldoutState.CONTAMINATED}
)

# The six reasons of umbrella 8.4, verbatim. A fixed vocabulary: a decision names exactly these.
REASON_NEGATIVE_EXPECTANCY = "negative expectancy at baseline costs"
REASON_NO_LOCKED_HOLDOUT = "no locked unseen holdout"
REASON_NOT_PREREGISTERED = "legacy evidence was not preregistered"
REASON_NO_DATASET_HASH = "dataset-content hash is unavailable"
REASON_NO_MARK_TO_MARKET = "mark-to-market returns are unavailable"
REASON_COST_NOT_ATTRIBUTED = "spread and slippage cannot be separately attributed"


class GateStatus(str, Enum):  # noqa: UP042
    PASS = "PASS"  # noqa: S105  (a gate outcome, not a credential)
    FAIL = "FAIL"
    UNAVAILABLE = "UNAVAILABLE"


class Decision(str, Enum):  # noqa: UP042
    REJECTED = "REJECTED"
    RESEARCH_PASSED = "RESEARCH_PASSED"
    PAPER_APPROVED = "PAPER_APPROVED"


class GateResult(CanonicalModel):
    gate: Annotated[int, Field(ge=1, le=GATE_COUNT)]
    name: NonEmptyStr
    status: GateStatus
    measured_value: FiniteFloat | None
    threshold: FiniteFloat | None
    evidence_sha256: tuple[NonEmptyStr, ...]
    reason: NonEmptyStr

    @model_validator(mode="after")
    def a_pass_or_a_fail_rests_on_a_measurement(self) -> Self:
        if self.status is not GateStatus.UNAVAILABLE and self.measured_value is None:
            raise ValueError("a PASS or a FAIL is a statement about a measured value")
        if self.status is GateStatus.PASS and not self.evidence_sha256:
            raise ValueError("a PASS names the evidence it rests on")
        return self


# --- the policy digest ---------------------------------------------------------------------


class PolicyDigestInput(CanonicalModel):
    """Every signed constant and the protocol's own validation spec, in one document."""

    dsr_minimum: FiniteFloat
    pbo_maximum: FiniteFloat
    max_drawdown: FiniteFloat
    drawdown_tolerance: FiniteFloat
    min_oos_trades: int
    min_regimes: int
    cpcv_p5_quantile: FiniteFloat
    min_wfa_folds: int
    mc_policy_version: NonEmptyStr
    validation: ValidationSpec | None
    """``None`` for a legacy trial, which was registered under no protocol."""


def policy_digest_input(spec: ValidationSpec | None) -> PolicyDigestInput:
    return PolicyDigestInput(
        dsr_minimum=DSR_MINIMUM,
        pbo_maximum=PBO_MAXIMUM,
        max_drawdown=MAX_DRAWDOWN,
        drawdown_tolerance=DRAWDOWN_TOLERANCE,
        min_oos_trades=MIN_OOS_TRADES,
        min_regimes=MIN_REGIMES,
        cpcv_p5_quantile=CPCV_P5_QUANTILE,
        min_wfa_folds=MIN_WFA_FOLDS,
        mc_policy_version=MC_POLICY_VERSION,
        validation=spec,
    )


def policy_sha256(spec: ValidationSpec | None) -> str:
    return canonical_sha256(policy_digest_input(spec))


# --- holdout lifecycle, derived from the chain ---------------------------------------------


class HoldoutStatus(CanonicalModel):
    state: HoldoutState
    reason: NonEmptyStr
    opened_bundle_count: NonNegativeInt


def _status(state: HoldoutState, reason: str, opened: int = 0) -> HoldoutStatus:
    return HoldoutStatus(state=state, reason=reason, opened_bundle_count=opened)


HoldoutKey = tuple[object, object, str]
"""A declared holdout's identity: its start, its end and its dataset hash."""


def _holdout_key(protocol: TrialProtocol) -> HoldoutKey | None:
    """The identity of a declared holdout; ``None`` if the protocol declares no dates or hash."""

    spec = protocol.holdout
    if spec.start is None or spec.end is None or spec.dataset_sha256 is None:
        return None
    return (spec.start, spec.end, spec.dataset_sha256)


def holdout_sharing_trials(trial_id: str, events: Sequence[LedgerEvent]) -> frozenset[str]:
    """The OTHER trials whose registered protocol declares this trial's holdout.

    The same holdout is the same start, end and dataset hash. A holdout opens at most once
    however many candidates or registrations name it, so ``derive_holdout`` reads these trials'
    sealed bundles too (amended 2026-10-02 after the 8D2 review). Pure over ``events``.
    """

    own: set[HoldoutKey] = set()
    declared: dict[str, set[HoldoutKey]] = {}
    for event in events:
        payload = event.payload
        if not isinstance(payload, PreregisteredPayload):
            continue
        key = _holdout_key(payload.protocol)
        if key is None:
            continue
        for candidate in payload.protocol.candidates:
            declared.setdefault(candidate.trial_id, set()).add(key)
            if candidate.trial_id == trial_id:
                own.add(key)
    return frozenset(other for other, keys in declared.items() if other != trial_id and keys & own)


def derive_holdout(
    trial_id: str,
    events: Sequence[LedgerEvent],
    sealed_holdout_states: Mapping[str, HoldoutState],
    sealed_levels: Mapping[str, Decimal],
) -> HoldoutStatus:
    """The holdout state of one trial, replayed from the chain (P-8). Never stored.

    ``events`` is the chain in replay order and that ORDER is the semantics: a decision
    consumes a holdout only if it follows the opening. ``sealed_holdout_states`` maps an
    evidence digest to the ``provenance.holdout_state`` of the bundle it names, which the
    caller has read; a sealed digest it does not hold is a refusal, never a guess.
    ``sealed_levels`` maps the same digests to the cost multiplier each bundle ran at.
    ``sealed_holdout_states`` must also hold the digests sealed by every trial in
    ``holdout_sharing_trials`` (the caller reads only those).

    Rules, in the order they bind: legacy evidence is contaminated (§5.7); a protocol that
    registers a spent state is contaminated; two registrations that are not one protocol
    are contaminated; a sealed bundle that says it was opened without a locked holdout, or
    says it is contaminated, contaminates; a second opened bundle AT AN ALREADY OPENED COST
    LEVEL contaminates; opened bundles are OPENED, and OPENED followed by any decision of the
    trial is CONSUMED.

    A holdout opens at most once across trials too (amended 2026-10-02 after the 8D2 review): a
    trial is CONTAMINATED when another trial whose registered protocol declares the same holdout
    (same start, end and dataset hash) has sealed an opened or consumed bundle. A trial's own
    opening is never counted against it by this rule, so the per-level rule above is unchanged.

    One opening is the cost grid run over the locked window (8D2: three bundles, 1.0x, 1.5x
    and 2.0x, all opened). The 8D1 text counted every opened bundle as an opening, which made
    that opening contaminate itself on its second bundle; a second opening is one that runs
    a cost level the first already ran.
    """

    registered: list[tuple[str, HoldoutState]] = []
    legacy = False
    for event in events:
        payload = event.payload
        if isinstance(payload, PreregisteredPayload) and any(
            candidate.trial_id == trial_id for candidate in payload.protocol.candidates
        ):
            registered.append((canonical_sha256(payload.protocol), payload.protocol.holdout.state))
        elif isinstance(payload, LegacyImportedPayload) and event.trial_id == trial_id:
            legacy = True
    if legacy:
        return _status(
            HoldoutState.CONTAMINATED,
            "legacy-imported evidence has no locked holdout and counts as contaminated",
        )
    if not registered:
        return _status(HoldoutState.NOT_DEFINED, "no registration declares this trial")
    if len({digest for digest, _ in registered}) > 1:
        return _status(
            HoldoutState.CONTAMINATED,
            "more than one registration that is not the same protocol declares this trial",
        )
    protocol_state = registered[0][1]
    if protocol_state in _REGISTERED_AS_SPENT:
        return _status(
            HoldoutState.CONTAMINATED,
            f"the protocol registered its holdout as {protocol_state.value}: an opening "
            "before the research gates is an inspection before lock",
        )

    sharing = holdout_sharing_trials(trial_id, events)
    for event in events:
        payload = event.payload
        if event.trial_id in sharing and isinstance(payload, EvidenceSealedPayload):
            if payload.evidence_sha256 not in sealed_holdout_states:
                raise PromotionRefusedError() from ValueError(
                    f"sealed evidence {payload.evidence_sha256} has no holdout state to derive from"
                )
            if sealed_holdout_states[payload.evidence_sha256] in _OPENING_STATES:
                return _status(
                    HoldoutState.CONTAMINATED,
                    f"this holdout was opened by trial {event.trial_id}",
                )
    opened: dict[str, Decimal] = {}
    consumed = False
    for event in events:
        if event.trial_id != trial_id:
            continue
        payload = event.payload
        if isinstance(payload, EvidenceSealedPayload):
            digest = payload.evidence_sha256
            if digest not in sealed_holdout_states:
                raise PromotionRefusedError() from ValueError(
                    f"sealed evidence {digest} has no holdout state to derive from"
                )
            state = sealed_holdout_states[digest]
            if state is HoldoutState.CONTAMINATED:
                return _status(
                    HoldoutState.CONTAMINATED,
                    f"sealed evidence {digest} says its holdout was contaminated",
                    len(opened),
                )
            if state in _OPENING_STATES:
                if protocol_state is HoldoutState.NOT_DEFINED:
                    return _status(
                        HoldoutState.CONTAMINATED,
                        f"sealed evidence {digest} was run on a holdout that was never locked",
                        len(opened),
                    )
                if digest not in opened:
                    if digest not in sealed_levels:
                        raise PromotionRefusedError() from ValueError(
                            f"sealed evidence {digest} has no cost level to derive from"
                        )
                    level = sealed_levels[digest]
                    if level in opened.values():
                        return _status(
                            HoldoutState.CONTAMINATED,
                            f"a second bundle was opened on the holdout at the {level}x level",
                            len(opened) + 1,
                        )
                    opened[digest] = level
        elif isinstance(payload, GateDecidedPayload) and opened:
            consumed = True
    if consumed:
        return _status(
            HoldoutState.CONSUMED, "a decision followed the opening of the holdout", len(opened)
        )
    if opened:
        return _status(HoldoutState.OPENED, "the holdout was opened once", len(opened))
    if protocol_state is HoldoutState.LOCKED:
        return _status(HoldoutState.LOCKED, "the protocol locked a holdout; nothing has opened it")
    return _status(HoldoutState.NOT_DEFINED, "the protocol defines no holdout")


# --- the gates -----------------------------------------------------------------------------


class EvidenceFacts(CanonicalModel):
    """What the baseline's sealed bundle says about itself, read by the blocking reasons."""

    registration_state: RegistrationState
    dataset_sha256_present: bool
    return_series_basis: ReturnSeriesBasis
    cost_status: CostAttributionStatus
    baseline_net_expectancy: FiniteFloat | None
    """Mean net P&L per closed trade of the baseline run; ``None`` when it closed none."""
    baseline_evidence_sha256: NonEmptyStr


class GateInputs(CanonicalModel):
    trial_id: NonEmptyStr
    policy_sha256: NonEmptyStr
    """The digest this evaluation claims to run under: the trial's first report's, or the
    one in force when it has none. ``evaluate_gates`` recomputes the one in force."""
    validation_spec: ValidationSpec | None
    evidence: StatisticalEvidence | None
    """``None`` for legacy evidence, which no 8C measurement can be taken over."""
    holdout: HoldoutStatus
    holdout_expectancy_1_5: Measurement | None
    holdout_expectancy_2_0: Measurement | None
    """Always ``None`` until 8D2 opens a holdout; gate 2 is then ``UNAVAILABLE``."""
    facts: EvidenceFacts


def _gate(
    number: int,
    name: str,
    status: GateStatus,
    reason: str,
    *,
    value: float | None = None,
    threshold: float | None = None,
    evidence: Sequence[str] = (),
) -> GateResult:
    return GateResult(
        gate=number,
        name=name,
        status=status,
        measured_value=value,
        threshold=threshold,
        evidence_sha256=tuple(dict.fromkeys(evidence)),
        reason=reason,
    )


NOT_MARK_TO_MARKET = "the measurement is not on a mark-to-market basis"


def _marked(measurement: Measurement) -> Measurement:
    """The measurement itself if it is undefined or on the mark-to-market basis, else it
    restated as undefined for that reason.

    Realized and mark-to-market series are never interchangeable (umbrella 5.5), and a
    candidate set whose baseline is marked can still hold a candidate that is not (a PBO
    ranking, say). The gates read measurements only through this, so a defined value on
    another basis blocks as ``UNAVAILABLE`` and can neither pass nor fail a gate.
    Gate 1 (a fold count) and gate 9 (capacity) read no return series and are exempt.
    """

    if measurement.value is None or measurement.basis_is_mark_to_market:
        return measurement
    return Measurement.undefined(
        measurement.name,
        NOT_MARK_TO_MARKET,
        evidence_sha256=measurement.evidence_sha256,
        basis_is_mark_to_market=False,
    )


def _compared(
    number: int,
    name: str,
    measurement: Measurement,
    *,
    threshold: float,
    relation: str,
    passes: Callable[[float], bool],
    note: str = "",
    series_gate: bool = True,
) -> GateResult:
    """One measurement against one threshold: UNAVAILABLE if undefined, else PASS or FAIL."""

    if series_gate:
        measurement = _marked(measurement)
    if measurement.value is None:
        return _gate(
            number,
            name,
            GateStatus.UNAVAILABLE,
            f"{measurement.name} is undefined: {measurement.undefined_reason}",
            threshold=threshold,
            evidence=measurement.evidence_sha256,
        )
    value = measurement.value
    met = passes(value)
    return _gate(
        number,
        name,
        GateStatus.PASS if met else GateStatus.FAIL,
        f"{measurement.name} is {value!r}; the requirement is {relation} {threshold!r}.{note}",
        value=value,
        threshold=threshold,
        evidence=measurement.evidence_sha256,
    )


def _no_evidence(number: int, name: str) -> GateResult:
    return _gate(number, name, GateStatus.UNAVAILABLE, "no statistical evidence")


def _wfa_gate(evidence: StatisticalEvidence) -> GateResult:
    minimum = float(MIN_WFA_FOLDS)
    return _compared(
        1,
        GATE_NAMES[1],
        evidence.wfa_folds,
        threshold=minimum,
        relation="at least",
        passes=lambda value: value >= minimum,
        series_gate=False,
    )


def _holdout_gate(inputs: GateInputs) -> GateResult:
    name = GATE_NAMES[HOLDOUT_GATE]
    if inputs.holdout.state not in _OPENING_STATES:
        return _gate(
            HOLDOUT_GATE,
            name,
            GateStatus.UNAVAILABLE,
            f"the holdout is {inputs.holdout.state.value}, not opened: {inputs.holdout.reason}",
            threshold=0.0,
        )
    measurements = (inputs.holdout_expectancy_1_5, inputs.holdout_expectancy_2_0)
    if any(item is None for item in measurements):
        return _gate(
            HOLDOUT_GATE,
            name,
            GateStatus.UNAVAILABLE,
            "the holdout is opened but its 1.5x and 2.0x expectancies were not both supplied",
            threshold=0.0,
        )
    held = [item for item in measurements if item is not None]
    return _positive_all(HOLDOUT_GATE, name, held, "holdout expectancy at 1.5x and 2.0x")


def _positive_all(
    number: int, name: str, measurements: Sequence[Measurement], label: str
) -> GateResult:
    """Every measurement strictly above zero: FAIL on any defined one that is not."""

    measurements = [_marked(item) for item in measurements]
    evidence = [digest for item in measurements for digest in item.evidence_sha256]
    defined = [item for item in measurements if item.value is not None]
    wrong = [item for item in defined if item.value is not None and not item.value > 0.0]
    if wrong:
        return _gate(
            number,
            name,
            GateStatus.FAIL,
            f"{label}: {wrong[0].name} is {wrong[0].value!r}, not above zero",
            value=wrong[0].value,
            threshold=0.0,
            evidence=evidence,
        )
    if len(defined) != len(measurements):
        return _gate(
            number,
            name,
            GateStatus.UNAVAILABLE,
            f"{label}: "
            + "; ".join(
                f"{item.name} is undefined: {item.undefined_reason}"
                for item in measurements
                if item.value is None
            ),
            threshold=0.0,
            evidence=evidence,
        )
    lowest = min(item.value for item in defined if item.value is not None)
    return _gate(
        number,
        name,
        GateStatus.PASS,
        f"{label}: every one is above zero; the lowest is {lowest!r}",
        value=lowest,
        threshold=0.0,
        evidence=evidence,
    )


def _drawdown_gate(evidence: StatisticalEvidence) -> GateResult:
    name = GATE_NAMES[7]
    limit = MAX_DRAWDOWN - DRAWDOWN_TOLERANCE
    drawdowns = tuple(
        _marked(item)
        for item in (
            evidence.max_drawdown_baseline,
            *evidence.max_drawdown_cpcv_paths,
            evidence.max_drawdown_compounding,
        )
    )
    monte_carlo = (_marked(evidence.monte_carlo.p_halt), _marked(evidence.monte_carlo.p_loss))
    everything = (*drawdowns, *monte_carlo)
    digests = [digest for item in everything for digest in item.evidence_sha256]
    over = [item for item in drawdowns if item.value is not None and not item.value < limit]
    if over:
        worst = max(item.value for item in over if item.value is not None)
        return _gate(
            7,
            name,
            GateStatus.FAIL,
            f"{over[0].name} is {over[0].value!r}, at or beyond the limit {limit!r} "
            "(MAX_DRAWDOWN less DRAWDOWN_TOLERANCE); a measured drawdown on the wrong side "
            "fails the gate whatever else is missing",
            value=worst,
            threshold=limit,
            evidence=digests,
        )
    missing = [item for item in everything if item.value is None]
    if not evidence.max_drawdown_cpcv_paths:
        return _gate(
            7,
            name,
            GateStatus.UNAVAILABLE,
            "no CPCV path drawdown was measured",
            threshold=limit,
            evidence=digests,
        )
    if missing:
        return _gate(
            7,
            name,
            GateStatus.UNAVAILABLE,
            "; ".join(f"{item.name} is undefined: {item.undefined_reason}" for item in missing),
            threshold=limit,
            evidence=digests,
        )
    worst = max(item.value for item in drawdowns if item.value is not None)
    return _gate(
        7,
        name,
        GateStatus.PASS,
        f"the worst of the baseline, every CPCV path and the compounding drawdown is "
        f"{worst!r}, below the limit {limit!r} (MAX_DRAWDOWN less DRAWDOWN_TOLERANCE); "
        "the Monte Carlo halt and loss measurements are defined",
        value=worst,
        threshold=limit,
        evidence=digests,
    )


def _coverage_gate(evidence: StatisticalEvidence) -> GateResult:
    name = GATE_NAMES[8]
    parts = (
        (_marked(evidence.coverage.oos_trades), float(MIN_OOS_TRADES)),
        (_marked(evidence.coverage.regimes_represented), float(MIN_REGIMES)),
    )
    digests = [digest for item, _ in parts for digest in item.evidence_sha256]
    short = [
        (item, minimum)
        for item, minimum in parts
        if item.value is not None and not item.value >= minimum
    ]
    if short:
        item, minimum = short[0]
        return _gate(
            8,
            name,
            GateStatus.FAIL,
            f"{item.name} is {item.value!r}; the requirement is at least {minimum!r}",
            value=item.value,
            threshold=minimum,
            evidence=digests,
        )
    missing = [item for item, _ in parts if item.value is None]
    if missing:
        return _gate(
            8,
            name,
            GateStatus.UNAVAILABLE,
            "; ".join(f"{item.name} is undefined: {item.undefined_reason}" for item in missing),
            evidence=digests,
        )
    trades, trades_minimum = parts[0]
    regimes, regimes_minimum = parts[1]
    return _gate(
        8,
        name,
        GateStatus.PASS,
        f"{trades.name} is {trades.value!r} (at least {trades_minimum!r}) and "
        f"{regimes.name} is {regimes.value!r} (at least {regimes_minimum!r})",
        value=trades.value,
        threshold=trades_minimum,
        evidence=digests,
    )


def _capacity_gate(evidence: StatisticalEvidence) -> GateResult:
    # ponytail: ``CapacityStatus`` has one member, so this gate is UNAVAILABLE by
    # construction, and a test pins the enum so that adding a member fails there and sends
    # whoever widens it to this function. Branch on the status when a volume-to-lots model
    # can be declared; until then no other answer exists to give.
    return _gate(
        9,
        GATE_NAMES[9],
        GateStatus.UNAVAILABLE,
        f"capacity is {CapacityStatus.UNAVAILABLE.value}: {evidence.capacity.reason}",
        evidence=(evidence.evidence.baseline,),
    )


def evaluate_gates(inputs: GateInputs) -> tuple[GateResult, ...]:
    """The nine gate outcomes, in order (P-5). Pure.

    Refuses, before any gate is evaluated, inputs whose policy digest is not the one the
    constants in force recompute to, and evidence of another trial.
    """

    if inputs.policy_sha256 != policy_sha256(inputs.validation_spec):
        raise PromotionRefusedError() from ValueError(
            "the policy digest in the inputs is not the one the constants in force give"
        )
    evidence = inputs.evidence
    if evidence is not None and evidence.trial_id != inputs.trial_id:
        raise PromotionRefusedError() from ValueError(
            f"evidence of trial {evidence.trial_id} was offered for trial {inputs.trial_id}"
        )
    holdout = _holdout_gate(inputs)
    if evidence is None:
        research = {number: _no_evidence(number, GATE_NAMES[number]) for number in RESEARCH_GATES}
        return tuple(
            holdout if number == HOLDOUT_GATE else research[number]
            for number in range(1, GATE_COUNT + 1)
        )

    cpcv = evidence.cpcv_p5
    return (
        _wfa_gate(evidence),
        holdout,
        _compared(
            3,
            GATE_NAMES[3],
            evidence.dsr.dsr,
            threshold=DSR_MINIMUM,
            relation="at least",
            passes=lambda value: value >= DSR_MINIMUM,
        ),
        _compared(
            4,
            GATE_NAMES[4],
            evidence.pbo.pbo,
            threshold=PBO_MAXIMUM,
            relation="at most",
            passes=lambda value: value <= PBO_MAXIMUM,
        ),
        _compared(
            5,
            GATE_NAMES[5],
            cpcv.p5,
            threshold=0.0,
            relation="above",
            passes=lambda value: value > 0.0,
            note=(
                ""
                if cpcv.paths_differ
                else " The CPCV paths were identical, so this is the net expectancy of the "
                "full sealed research-window sample, which is in-sample on that window and "
                "not an out-of-sample figure."
            ),
        ),
        _compared(
            6,
            GATE_NAMES[6],
            evidence.bootstrap.bootstrap_lower_bound,
            threshold=0.0,
            relation="above",
            passes=lambda value: value > 0.0,
        ),
        _drawdown_gate(evidence),
        _coverage_gate(evidence),
        _capacity_gate(evidence),
    )


def blocking_reasons(facts: EvidenceFacts, holdout: HoldoutStatus) -> tuple[str, ...]:
    """The reasons that force REJECTED whatever the gates say (P-11), in a fixed order.

    Computed from the evidence, so they apply to any trial: a prospective trial sealed with
    no dataset hash is blocked for the same reason a legacy import is.
    """

    reasons: list[str] = []
    if facts.baseline_net_expectancy is not None and facts.baseline_net_expectancy < 0:
        reasons.append(REASON_NEGATIVE_EXPECTANCY)
    if holdout.state in {HoldoutState.NOT_DEFINED, HoldoutState.CONTAMINATED}:
        reasons.append(REASON_NO_LOCKED_HOLDOUT)
    if facts.registration_state is RegistrationState.LEGACY_UNPREGISTERED:
        reasons.append(REASON_NOT_PREREGISTERED)
    if not facts.dataset_sha256_present:
        reasons.append(REASON_NO_DATASET_HASH)
    if facts.return_series_basis is not ReturnSeriesBasis.MARK_TO_MARKET:
        reasons.append(REASON_NO_MARK_TO_MARKET)
    if facts.cost_status is not CostAttributionStatus.COMPLETE:
        reasons.append(REASON_COST_NOT_ATTRIBUTED)
    return tuple(reasons)


def decide(
    results: Sequence[GateResult], holdout: HoldoutStatus, reasons: Sequence[str]
) -> Decision:
    """The three-valued decision over the nine outcomes (P-7).

    ``REJECTED`` when any blocking reason stands. Otherwise ``PAPER_APPROVED`` when all nine
    gates pass and the holdout is opened or consumed (a gate-2 PASS the holdout state does
    not support is inconsistent evidence, not an approval), else ``RESEARCH_PASSED`` when
    every research gate passes and the holdout is still ``LOCKED`` (the one state from which
    §8.1 lets it be opened), else ``REJECTED``. A holdout that is not defined or is
    contaminated therefore never reaches ``RESEARCH_PASSED``.
    """

    if sorted(result.gate for result in results) != list(range(1, GATE_COUNT + 1)):
        raise PromotionRefusedError() from ValueError("a decision needs exactly the nine gates")
    status = {result.gate: result.status for result in results}
    if reasons:
        return Decision.REJECTED
    research_pass = all(status[gate] is GateStatus.PASS for gate in RESEARCH_GATES)
    if (
        research_pass
        and status[HOLDOUT_GATE] is GateStatus.PASS
        and holdout.state in _OPENING_STATES
    ):
        return Decision.PAPER_APPROVED
    if research_pass and holdout.state is HoldoutState.LOCKED:
        return Decision.RESEARCH_PASSED
    return Decision.REJECTED


# --- the report ----------------------------------------------------------------------------


class ValidationReport(CanonicalModel):
    """One immutable judgement of one trial, and everything it rests on (P-9).

    It holds no field for its own digest: the digest is ``canonical_sha256`` of it, derived,
    so a report cannot name itself wrongly. The chain's ``VALIDATED`` and ``GATE_DECIDED``
    events reference that digest.
    """

    report_schema_version: Literal[1] = 1
    trial_id: NonEmptyStr
    attempt_id: NonEmptyStr
    spec_sha256: NonEmptyStr
    policy_sha256: NonEmptyStr
    policy: PolicyDigestInput
    chain_head: NonEmptyStr
    """The event hash of the last evidence-bearing row read: decisions are not counted, so
    the chain a decision appends does not change the evidence it was made on."""
    holdout: HoldoutStatus
    facts: EvidenceFacts
    evidence: StatisticalEvidence | None
    gates: tuple[GateResult, ...]
    blocking_reasons: tuple[str, ...]
    decision: Decision

    @model_validator(mode="after")
    def the_policy_gates_and_decision_agree(self) -> Self:
        if self.policy_sha256 != canonical_sha256(self.policy):
            raise ValueError("the policy digest is not the digest of the policy it embeds")
        if [gate.gate for gate in self.gates] != list(range(1, GATE_COUNT + 1)):
            raise ValueError("a report holds the nine gates, in order")
        if self.decision is not decide(self.gates, self.holdout, self.blocking_reasons):
            raise ValueError("the decision is not the one the gates, holdout and reasons give")
        return self

    def digest(self) -> str:
        return canonical_sha256(self)


# --- the package ---------------------------------------------------------------------------


def _refuse(reason: str) -> NoReturn:
    """A package refusal: opaque in public, the reason on the private cause."""

    raise PromotionRefusedError() from ValueError(reason)


def require_paper_approved(report: ValidationReport, report_sha256: str) -> None:
    """The report is the one named, and it is a full approval: the only ground for a package.

    The decision is checked AND the nine gates and the blocking reasons it rests on, so a
    report whose own validator was bypassed cannot stand in for an approval.
    """

    if report.digest() != report_sha256:
        _refuse("the report is not the one the digest names")
    if report.decision is not Decision.PAPER_APPROVED:
        _refuse(f"the decision is {report.decision.value}, not PAPER_APPROVED")
    if [gate.gate for gate in report.gates] != list(range(1, GATE_COUNT + 1)) or any(
        gate.status is not GateStatus.PASS for gate in report.gates
    ):
        _refuse("an approval needs all nine gates to pass")
    if report.blocking_reasons:
        _refuse("an approval stands under no blocking reason")


def check_package(
    package: StrategyPackage,
    report: ValidationReport,
    report_sha256: str,
    *,
    paper: StrategyPackage | None = None,
) -> None:
    """Refuse a package that its report and (for LIVE) its paper package do not support.

    Pure. ``SANDBOX`` grants nothing and rests on no approval; ``PAPER`` and ``LIVE`` rest on a
    full approval whose digest the package names. ``LIVE`` needs a valid ``PAPER`` package of
    the same candidate and report, carries that paper authorization, and its capital
    authorization is a different reference. A reference is not a signature and nothing here
    checks that a person made it.
    """

    if package.validation_report_sha256 != report_sha256:
        _refuse("the package names another report than the one supplied")
    if package.stage is PromotionStage.SANDBOX:
        return
    require_paper_approved(report, report_sha256)
    if package.stage is not PromotionStage.LIVE:
        return
    if paper is None:
        _refuse("a LIVE package needs the PAPER package it continues")
    if paper.stage is not PromotionStage.PAPER:
        _refuse("the package offered as the paper package is not a PAPER package")
    check_package(paper, report, report_sha256)
    if (paper.spec, paper.source_sha256) != (package.spec, package.source_sha256):
        _refuse("the paper package is of another candidate or source")
    if package.authorization_ref != paper.authorization_ref:
        _refuse("a LIVE package carries the paper authorization it continues")
    if package.capital_authorization_ref == paper.authorization_ref:
        _refuse("the capital authorization must differ from the paper authorization")


def create_package(
    report: ValidationReport,
    report_sha256: str,
    *,
    package_id: str,
    spec: StrategySpec,
    source_sha256: str,
    trial_ledger_reference: str,
    stage: PromotionStage = PromotionStage.SANDBOX,
    authorization_ref: str | None = None,
    capital_authorization_ref: str | None = None,
    signature_sha256: str | None = None,
    paper_package: StrategyPackage | None = None,
) -> StrategyPackage:
    """Build a package from a decision, or refuse. Pure; signs nothing.

    The default and only stage a decision alone yields is ``SANDBOX``. ``PAPER`` needs a
    ``PAPER_APPROVED`` report and an authorization reference. ``LIVE`` needs the paper
    package, a signature reference and a capital authorization that is not the paper one: a
    decision never reaches ``LIVE`` by itself, and every missing piece is a refusal.
    """

    try:
        package = StrategyPackage(
            package_id=package_id,
            spec=spec,
            source_sha256=source_sha256,
            trial_ledger_reference=trial_ledger_reference,
            validation_report_sha256=report_sha256,
            signature_sha256=signature_sha256,
            stage=stage,
            authorization_ref=authorization_ref,
            capital_authorization_ref=capital_authorization_ref,
        )
    except ValidationError as error:
        raise PromotionRefusedError() from error
    check_package(package, report, report_sha256, paper=paper_package)
    return package
