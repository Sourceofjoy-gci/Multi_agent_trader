"""Judge one sealed trial, record the judgement, and read it back.

Phase 8D1. ``research/promotion.py`` is pure; this is the one place that reads for it. It
assembles ``GateInputs`` from the chain and the evidence store (the 8C3 assembly for a
prospective trial, the sealed legacy bundle for a legacy one), runs the pure functions,
seals a ``ValidationReport`` and appends ``VALIDATED`` then ``GATE_DECIDED``.

It creates no package and changes no stage: a decision is a statement about evidence, and
the only thing that may move a stage is a package carrying a human authorization (8D2).

Three properties are deliberate, and each is a place a shorter version would break:

* **A rerun on an UNCHANGED chain is a no-op.** The report digest is a function of the
  evidence alone (``chain_head`` is taken with the two decision event types left out, so
  the rows a decision appends do not change the evidence a rerun reads), and the event ids
  are content-derived. But that head is the last evidence-bearing row of the WHOLE chain,
  so evidence sealed for another trial moves it and a rerun then seals a new report and
  appends a new pair for the same decision. On an unchanged chain a rerun finds both
  events by id and appends nothing, whatever ``occurred_at`` it is given; a crash between
  the two appends is closed by the next run (and is visible to ``verify`` until then).
* **The first report pins the policy.** A trial that already has a report is evaluated
  under the policy digest that report names. If the constants in force have since moved,
  ``evaluate_gates`` refuses: a threshold may not change after a result exists.
* **Declared trials only.** The ledger vouches for no ``VALIDATED`` or ``GATE_DECIDED``
  row (``_REQUIRES_REGISTRATION`` omits both, and the append function checks no spec
  digest), so this module refuses a trial no registration or legacy import declares.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal

from trading_house.core.errors import EvidenceIntegrityError, PromotionRefusedError
from trading_house.core.values import CanonicalModel, NonEmptyStr
from trading_house.ops.ledger import gate_decided_event, validated_event
from trading_house.ops.scenarios import declared_candidate, registered_protocol
from trading_house.ops.validate import read_validation_inputs, statistical_evidence
from trading_house.research.canonical import canonical_sha256
from trading_house.research.evidence import EvidenceBundle, EvidenceStore
from trading_house.research.ledger_store import PostgresTrialLedger
from trading_house.research.promotion import (
    EvidenceFacts,
    GateInputs,
    GateResult,
    HoldoutStatus,
    ValidationReport,
    blocking_reasons,
    decide,
    derive_holdout,
    evaluate_gates,
    policy_digest_input,
    policy_sha256,
)
from trading_house.research.trial_ledger import (
    EvidenceSealedPayload,
    GateDecidedPayload,
    HoldoutState,
    LedgerEvent,
    LedgerEventType,
    LegacyImportedPayload,
    PreregisteredPayload,
    TrialProtocol,
    ValidatedPayload,
)
from trading_house.research.validation.evidence import StatisticalEvidence

DECISION_EVENT_TYPES = frozenset({LedgerEventType.VALIDATED, LedgerEventType.GATE_DECIDED})


class DecideOutcome(CanonicalModel):
    """What one ``decide`` produced, and whether the chain already held it."""

    report_sha256: NonEmptyStr
    decision: NonEmptyStr
    holdout: HoldoutStatus
    gates: tuple[GateResult, ...]
    blocking_reasons: tuple[str, ...]
    already_recorded: bool
    """True when the chain already held both events for this report: nothing was appended."""


def _registered(events: Sequence[LedgerEvent], trial_id: str) -> bool:
    return any(
        isinstance(event.payload, PreregisteredPayload)
        and declared_candidate(event.payload.protocol, trial_id) is not None
        for event in events
    )


def _legacy_import(events: Sequence[LedgerEvent], trial_id: str) -> LegacyImportedPayload | None:
    for event in events:
        if isinstance(event.payload, LegacyImportedPayload) and event.trial_id == trial_id:
            return event.payload
    return None


def _sealed_holdout_states(
    events: Sequence[LedgerEvent], trial_id: str, store: EvidenceStore
) -> dict[str, HoldoutState]:
    """The provenance holdout state of every bundle the chain seals for this trial."""

    return {
        event.payload.evidence_sha256: store.read(
            event.payload.evidence_sha256
        ).provenance.holdout_state
        for event in events
        if event.trial_id == trial_id and isinstance(event.payload, EvidenceSealedPayload)
    }


def holdout_status(
    trial_id: str, ledger: PostgresTrialLedger, store: EvidenceStore
) -> HoldoutStatus:
    """The derived holdout state of a declared trial; an undeclared one is refused."""

    events, _ = ledger.snapshot()
    if _legacy_import(events, trial_id) is None:
        registered_protocol(events, trial_id)
    return derive_holdout(trial_id, events, _sealed_holdout_states(events, trial_id, store))


def _facts(bundle: EvidenceBundle, digest: str) -> EvidenceFacts:
    trades = bundle.result.trades
    expectancy = (
        float(sum((trade.net_pnl for trade in trades), Decimal(0)) / len(trades))
        if trades
        else None
    )
    return EvidenceFacts(
        registration_state=bundle.provenance.registration_state,
        dataset_sha256_present=bundle.provenance.dataset_sha256 is not None,
        return_series_basis=bundle.return_series_basis,
        cost_status=bundle.costs.status,
        baseline_net_expectancy=expectancy,
        baseline_evidence_sha256=digest,
    )


def _pinned_policy(
    events: Sequence[LedgerEvent], trial_id: str, store: EvidenceStore, current: str
) -> str:
    """The policy digest this trial's evaluation must run under.

    The digest the trial's FIRST report names, when it has one, else the one in force.
    """

    for event in events:
        if event.trial_id == trial_id and isinstance(event.payload, ValidatedPayload):
            return store.read_report(event.payload.report_sha256).policy_sha256
    return current


def decide_trial(
    trial_id: str,
    *,
    ledger: PostgresTrialLedger,
    store: EvidenceStore,
    occurred_at: datetime,
) -> DecideOutcome:
    """Judge one declared trial, seal the report and record it. Idempotent."""

    events, _ = ledger.snapshot()
    legacy = _legacy_import(events, trial_id)
    protocol: TrialProtocol | None = None
    evidence: StatisticalEvidence | None = None
    if legacy is not None:
        if _registered(events, trial_id):
            raise PromotionRefusedError() from ValueError(
                f"trial {trial_id} is both legacy-imported and preregistered"
            )
        digest = legacy.evidence_sha256
        bundle = store.read(digest)
        _, head = ledger.snapshot(DECISION_EVENT_TYPES)
        if head is None:  # pragma: no cover
            # Narrowing, not a reachable guard: the legacy import this trial rests on is
            # itself an evidence-bearing row. Kept fail-closed for the day that changes.
            raise PromotionRefusedError() from ValueError("the chain holds no evidence-bearing row")
    else:
        inputs = read_validation_inputs(trial_id, ledger, store, ignore=DECISION_EVENT_TYPES)
        evidence = statistical_evidence(inputs)
        digest, bundle = inputs.baseline
        protocol = inputs.protocol
        head = evidence.evidence.chain_head

    spec = None if protocol is None else protocol.validation
    holdout = derive_holdout(trial_id, events, _sealed_holdout_states(events, trial_id, store))
    facts = _facts(bundle, digest)
    results = evaluate_gates(
        GateInputs(
            trial_id=trial_id,
            policy_sha256=_pinned_policy(events, trial_id, store, policy_sha256(spec)),
            validation_spec=spec,
            evidence=evidence,
            holdout=holdout,
            holdout_expectancy_1_5=None,
            holdout_expectancy_2_0=None,
            facts=facts,
        )
    )
    reasons = blocking_reasons(facts, holdout)
    decision = decide(results, holdout, reasons)
    report = ValidationReport(
        trial_id=trial_id,
        attempt_id=bundle.attempt_id,
        spec_sha256=bundle.spec_sha256,
        policy_sha256=policy_sha256(spec),
        policy=policy_digest_input(spec),
        chain_head=head,
        holdout=holdout,
        facts=facts,
        evidence=evidence,
        gates=results,
        blocking_reasons=reasons,
        decision=decision,
    )
    report_sha256 = store.write_report(report).sha256
    recorded = {record.event_id for record in ledger.events_for(trial_id)}
    pair = (
        validated_event(
            trial_id, bundle.attempt_id, bundle.spec_sha256, report_sha256, occurred_at
        ),
        gate_decided_event(
            trial_id,
            bundle.attempt_id,
            bundle.spec_sha256,
            report_sha256,
            decision.value,
            occurred_at,
        ),
    )
    for event in pair:
        if event.event_id not in recorded:
            ledger.append(event)
    return DecideOutcome(
        report_sha256=report_sha256,
        decision=decision.value,
        holdout=holdout,
        gates=results,
        blocking_reasons=reasons,
        already_recorded=all(event.event_id in recorded for event in pair),
    )


def latest_report(
    trial_id: str, ledger: PostgresTrialLedger, store: EvidenceStore
) -> tuple[str, ValidationReport]:
    """The report the trial's last recorded decision names, with its digest.

    Refused when the trial has no recorded decision, and when the decision the chain
    holds is not the one the report states.
    """

    named = [
        GateDecidedPayload.model_validate(record.event_json["payload"])
        for record in ledger.events_for(trial_id)
        if record.event_type is LedgerEventType.GATE_DECIDED
    ]
    if not named:
        raise PromotionRefusedError() from ValueError(f"trial {trial_id} has no recorded decision")
    report = store.read_report(named[-1].report_sha256)
    if report.trial_id != trial_id or report.decision.value != named[-1].decision:
        raise EvidenceIntegrityError()
    return named[-1].report_sha256, report


def verify_reports(events: Sequence[LedgerEvent], store: EvidenceStore) -> None:
    """Re-read every report the chain names, and check each is the event's own.

    A missing or altered report file is ``EvidenceIntegrityError`` through ``read_report``.
    The same error for a report that is not the event's own (another trial, attempt or
    specification), a report whose policy digest is not the digest of the policy it
    embeds, a ``GATE_DECIDED`` whose decision is not the report's, and a ``VALIDATED``
    with no ``GATE_DECIDED`` for the same report after it. The last is also what a crash
    between ``decide``'s two appends looks like: visible here until the next ``decide``
    closes it.
    """

    awaiting: set[str] = set()
    for event in events:
        payload = event.payload
        if not isinstance(payload, (ValidatedPayload, GateDecidedPayload)):
            continue
        report = store.read_report(payload.report_sha256)
        if (
            report.trial_id != event.trial_id
            or report.attempt_id != event.attempt_id
            or report.spec_sha256 != event.spec_sha256
            or report.policy_sha256 != canonical_sha256(report.policy)
        ):
            raise EvidenceIntegrityError()
        key = payload.report_sha256
        if isinstance(payload, ValidatedPayload):
            awaiting.add(key)
            continue
        if report.decision.value != payload.decision:
            raise EvidenceIntegrityError()
        awaiting.discard(key)
    if awaiting:
        raise EvidenceIntegrityError()
