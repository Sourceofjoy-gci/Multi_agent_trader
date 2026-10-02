"""Build a strategy package from a recorded decision, and check one against the chain.

Phase 8D2. Both functions read the chain and the evidence store and APPEND NOTHING: a
package is a file the operator asked for, not a ledger event, and no stage anywhere is
changed by making one. Neither signs anything, and neither can check that a person made the
authorization or capital authorization a package names: those are references, and the
only thing verified about a reference is that it is present, distinct where it must be,
and attached to the stage that requires it.

``research/promotion.py`` holds the pure rules (``create_package``, ``check_package``); this
module only reads for them.
"""

from __future__ import annotations

from collections.abc import Sequence

from trading_house.core.errors import PromotionRefusedError
from trading_house.core.values import AssetClass, CanonicalModel, Horizon, NonEmptyStr
from trading_house.ops.decide import holdout_status, latest_report
from trading_house.ops.scenarios import registered_protocol, required_candidate
from trading_house.research.evidence import EvidenceStore
from trading_house.research.ledger_store import PostgresTrialLedger
from trading_house.research.packages import PromotionStage, StrategyPackage, StrategySpec
from trading_house.research.promotion import check_package, create_package, require_paper_approved
from trading_house.research.trial_ledger import HoldoutState, LedgerEventType, ValidatedPayload


def _refuse_contaminated(trial_id: str, ledger: PostgresTrialLedger, store: EvidenceStore) -> None:
    """A stale report must not license a package: the holdout is re-derived now."""

    status = holdout_status(trial_id, ledger, store)
    if status.state is HoldoutState.CONTAMINATED:
        raise PromotionRefusedError() from ValueError(
            f"the holdout is contaminated now: {status.reason}"
        )


class VerifiedPackage(CanonicalModel):
    """What ``verify`` re-derived: the trial and report a package rests on."""

    trial_id: NonEmptyStr
    stage: PromotionStage
    report_sha256: NonEmptyStr
    decision: NonEmptyStr


def package_from_chain(
    trial_id: str,
    *,
    ledger: PostgresTrialLedger,
    store: EvidenceStore,
    stage: PromotionStage,
    book: str,
    horizon: Horizon,
    asset_classes: Sequence[AssetClass],
    authorization_ref: str | None,
    capital_authorization_ref: str | None,
    signature_sha256: str | None,
    paper_package: StrategyPackage | None,
) -> StrategyPackage:
    """The package the trial's latest recorded decision supports at ``stage``, or a refusal.

    The decision is read FIRST and a stage above ``SANDBOX`` is refused unless it is a full
    approval, so a trial with no protocol (a legacy import) or a rejected one is answered
    with the promotion refusal and not a lookup failure.
    """

    report_sha256, report = latest_report(trial_id, ledger, store)
    if stage is not PromotionStage.SANDBOX:
        require_paper_approved(report, report_sha256)
    if stage is not PromotionStage.SANDBOX:
        _refuse_contaminated(trial_id, ledger, store)
    protocol = registered_protocol(ledger.replay(), trial_id)
    candidate = required_candidate(protocol, trial_id)
    _, head = ledger.snapshot()
    if head is None:  # pragma: no cover
        # Narrowing: the decision read above is itself a chain row.
        raise PromotionRefusedError() from ValueError("the chain holds no event")
    return create_package(
        report,
        report_sha256,
        package_id=f"{trial_id}-{stage.value}-{report_sha256[:12]}",
        spec=StrategySpec(
            spec_id=candidate.spec_id,
            hypothesis=candidate.rationale,
            book=book,
            horizon=horizon,
            asset_classes=tuple(asset_classes),
        ),
        source_sha256=protocol.strategy_sha256,
        trial_ledger_reference=head,
        stage=stage,
        authorization_ref=authorization_ref,
        capital_authorization_ref=capital_authorization_ref,
        signature_sha256=signature_sha256,
        paper_package=paper_package,
    )


def verify_package(
    package: StrategyPackage,
    *,
    ledger: PostgresTrialLedger,
    store: EvidenceStore,
    paper_package: StrategyPackage | None = None,
) -> VerifiedPackage:
    """Re-derive a package's references against the chain; refuse the first that fails.

    The report digest must name a readable report (an altered one is an integrity failure);
    the chain must hold a ``VALIDATED`` event naming it and its trial's LATEST ``GATE_DECIDED``
    must name it too; ``trial_ledger_reference`` must be the hash of a row the chain holds at
    or after that latest decision; a PAPER or LIVE package is refused while the holdout derives
    as CONTAMINATED;
    ``source_sha256`` must be the registered protocol's strategy hash; the specification must be
    the declared candidate's; and the stage rules of ``check_package`` must hold. A ``LIVE``
    package is verified only against the ``PAPER`` package it continues.
    """

    report_sha256 = package.validation_report_sha256
    report = store.read_report(report_sha256)
    trial_id = report.trial_id
    validated = {
        ValidatedPayload.model_validate(record.event_json["payload"]).report_sha256
        for record in ledger.events_for(trial_id)
        if record.event_type is LedgerEventType.VALIDATED
    }
    if report_sha256 not in validated:
        raise PromotionRefusedError() from ValueError("no VALIDATED event names this report")
    latest_sha256, _ = latest_report(trial_id, ledger, store)
    if latest_sha256 != report_sha256:
        raise PromotionRefusedError() from ValueError(
            "the trial's latest decision names another report"
        )
    chain = ledger.events()
    referenced = [r.sequence for r in chain if r.event_hash == package.trial_ledger_reference]
    if not referenced:
        raise PromotionRefusedError() from ValueError("the ledger reference is not in the chain")
    decided = [
        r.sequence
        for r in ledger.events_for(trial_id)
        if r.event_type is LedgerEventType.GATE_DECIDED
    ]
    if referenced[0] < max(decided):
        raise PromotionRefusedError() from ValueError(
            "the ledger reference precedes the trial's latest decision"
        )
    if package.stage is not PromotionStage.SANDBOX:
        _refuse_contaminated(trial_id, ledger, store)
    protocol = registered_protocol(ledger.replay(), trial_id)
    if package.source_sha256 != protocol.strategy_sha256:
        raise PromotionRefusedError() from ValueError("the source hash is not the protocol's")
    candidate = required_candidate(protocol, trial_id)
    if (package.spec.spec_id, package.spec.hypothesis) != (candidate.spec_id, candidate.rationale):
        raise PromotionRefusedError() from ValueError("the specification is not the candidate's")
    check_package(package, report, report_sha256, paper=paper_package)
    return VerifiedPackage(
        trial_id=trial_id,
        stage=package.stage,
        report_sha256=report_sha256,
        decision=report.decision.value,
    )
