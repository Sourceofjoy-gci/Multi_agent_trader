"""A NAMED TEST HELPER: record a decision no real candidate can earn today.

Gate 9 (capacity) is ``UNAVAILABLE`` for every candidate, and gate 9 is a research gate, so
``research trial decide`` can never answer ``RESEARCH_PASSED`` or ``PAPER_APPROVED`` for a real
trial. Everything downstream of those two answers (the holdout opening, the package rules) can
therefore only be exercised by putting such a decision on the chain some other way. This is that
other way, and it lives in ``tests/`` and nowhere a command can reach:

* it builds a ``ValidationReport`` whose nine gates are all ``PASS`` (gate 2 ``UNAVAILABLE`` for
  ``RESEARCH_PASSED``) with the model's OWN validators intact, so the decision, the policy digest
  and the nine gates still have to agree for the report to exist at all;
* it seals that report through the real ``EvidenceStore`` and appends a real ``VALIDATED`` and a
  real ``GATE_DECIDED`` through the real ledger;
* the evidence behind its gates is the trial's real sealed baseline, named by digest, but the
  gates themselves are asserted, not measured. It is a synthetic decision and says so in its
  name, its reasons and its gate reason text.

Nothing in ``src/`` imports this module, and ``test_no_command_can_reach_the_synthetic_helper``
holds that line.
"""

from __future__ import annotations

from datetime import datetime

from trading_house.ops.compounding import sealed_baseline
from trading_house.ops.decide import DECISION_EVENT_TYPES, holdout_status
from trading_house.ops.ledger import gate_decided_event, validated_event
from trading_house.ops.scenarios import registered_protocol, sealed_bundles
from trading_house.research.evidence import EvidenceStore
from trading_house.research.ledger_store import PostgresTrialLedger
from trading_house.research.promotion import (
    GATE_NAMES,
    Decision,
    EvidenceFacts,
    GateResult,
    GateStatus,
    ValidationReport,
    blocking_reasons,
    decide,
    policy_digest_input,
    policy_sha256,
)
from trading_house.research.trial_ledger import (
    CostAttributionStatus,
    RegistrationState,
    ReturnSeriesBasis,
)

SYNTHETIC = "SYNTHETIC: asserted by a test helper, not measured"


def synthetic_report(
    trial_id: str,
    ledger: PostgresTrialLedger,
    store: EvidenceStore,
    want: Decision,
) -> ValidationReport:
    """A sealed-able report that ``want`` is the decision, or a ``ValueError``."""

    protocol = registered_protocol(ledger.replay(), trial_id)
    digest, bundle = sealed_baseline(
        sealed_bundles(ledger.events_for(trial_id), store.read), trial_id
    )
    holdout = holdout_status(trial_id, ledger, store)
    gates = tuple(
        GateResult(
            gate=number,
            name=GATE_NAMES[number],
            status=(
                GateStatus.UNAVAILABLE
                if number == 2 and want is Decision.RESEARCH_PASSED
                else GateStatus.PASS
            ),
            measured_value=None if number == 2 and want is Decision.RESEARCH_PASSED else 1.0,
            threshold=0.0,
            evidence_sha256=(digest,),
            reason=SYNTHETIC,
        )
        for number in range(1, 10)
    )
    facts = EvidenceFacts(
        registration_state=RegistrationState.PROSPECTIVE,
        dataset_sha256_present=True,
        return_series_basis=ReturnSeriesBasis.MARK_TO_MARKET,
        cost_status=CostAttributionStatus.COMPLETE,
        baseline_net_expectancy=1.0,
        baseline_evidence_sha256=digest,
    )
    reasons = blocking_reasons(facts, holdout)
    decision = decide(gates, holdout, reasons)
    if decision is not want:
        raise ValueError(f"the synthetic gates give {decision.value}, not {want.value}")
    _, head = ledger.snapshot(DECISION_EVENT_TYPES)
    if head is None:
        raise ValueError("the chain holds no evidence-bearing row")
    return ValidationReport(
        trial_id=trial_id,
        attempt_id=bundle.attempt_id,
        spec_sha256=bundle.spec_sha256,
        policy_sha256=policy_sha256(protocol.validation),
        policy=policy_digest_input(protocol.validation),
        chain_head=head,
        holdout=holdout,
        facts=facts,
        evidence=None,
        gates=gates,
        blocking_reasons=reasons,
        decision=decision,
    )


def append_synthetic_decision(
    trial_id: str,
    ledger: PostgresTrialLedger,
    store: EvidenceStore,
    want: Decision,
    occurred_at: datetime,
) -> str:
    """Seal a synthetic report, append its ``VALIDATED`` and ``GATE_DECIDED``; return its digest."""

    report = synthetic_report(trial_id, ledger, store, want)
    report_sha256 = store.write_report(report).sha256
    for event in (
        validated_event(
            trial_id, report.attempt_id, report.spec_sha256, report_sha256, occurred_at
        ),
        gate_decided_event(
            trial_id,
            report.attempt_id,
            report.spec_sha256,
            report_sha256,
            want.value,
            occurred_at,
        ),
    ):
        ledger.append(event)
    return report_sha256
