"""The two event builders and the report re-read, without a database.

``decide_trial`` itself reads a real chain and is the acceptance's; what a unit can pin is
that the builders' identities are content-derived (a rerun is the same event, a different
report or trial is not) and that ``verify_reports`` refuses each way a report can be wrong.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest

from tests.unit.research.test_promotion import _report
from trading_house.core.errors import EvidenceIntegrityError
from trading_house.ops.decide import DECISION_EVENT_TYPES, verify_reports
from trading_house.ops.ledger import gate_decided_event, validated_event
from trading_house.research.evidence import EvidenceStore
from trading_house.research.trial_ledger import (
    GateDecidedPayload,
    LedgerEventType,
    ScopeKind,
    ValidatedPayload,
)

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
T1 = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
SPEC = "a" * 64
R = "b" * 64


def test_the_decision_event_types_are_exactly_the_two_the_chain_head_ignores() -> None:
    assert {LedgerEventType.VALIDATED, LedgerEventType.GATE_DECIDED} == DECISION_EVENT_TYPES


def test_a_validated_event_names_the_report_in_the_attempts_scope() -> None:
    event = validated_event("t1", "a1", SPEC, R, T0)

    assert event.event_type is LedgerEventType.VALIDATED
    assert event.payload == ValidatedPayload(event_type=LedgerEventType.VALIDATED, report_sha256=R)
    assert (event.scope_kind, event.scope_id) == (ScopeKind.ATTEMPT, "a1")
    assert (event.trial_id, event.attempt_id, event.spec_sha256) == ("t1", "a1", SPEC)
    assert event.occurred_at == T0
    assert event.legacy is False


def test_a_gate_decided_event_carries_the_decision_and_the_report() -> None:
    event = gate_decided_event("t1", "a1", SPEC, R, "REJECTED", T0)

    assert event.event_type is LedgerEventType.GATE_DECIDED
    assert event.payload == GateDecidedPayload(
        event_type=LedgerEventType.GATE_DECIDED, decision="REJECTED", report_sha256=R
    )
    assert (event.scope_kind, event.scope_id, event.trial_id) == (ScopeKind.ATTEMPT, "a1", "t1")


def test_event_ids_are_content_derived_from_trial_report_and_event_type() -> None:
    validated = validated_event("t1", "a1", SPEC, R, T0)
    decided = gate_decided_event("t1", "a1", SPEC, R, "REJECTED", T0)

    assert validated.event_id == uuid5(NAMESPACE_URL, f"trading-house:trial-validated:t1:{R}")
    assert decided.event_id == uuid5(NAMESPACE_URL, f"trading-house:trial-gate-decided:t1:{R}")
    assert validated.event_id != decided.event_id
    # the retry the id exists for: the same evidence is the same event whatever clock
    assert validated_event("t1", "a1", SPEC, R, T1).event_id == validated.event_id
    assert gate_decided_event("t1", "a1", SPEC, R, "REJECTED", T1).event_id == decided.event_id
    # a different report or a different trial is a different event
    assert validated_event("t1", "a1", SPEC, "c" * 64, T0).event_id != validated.event_id
    assert validated_event("t2", "a1", SPEC, R, T0).event_id != validated.event_id
    assert (
        gate_decided_event("t1", "a1", SPEC, "c" * 64, "REJECTED", T0).event_id != decided.event_id
    )
    assert gate_decided_event("t2", "a1", SPEC, R, "REJECTED", T0).event_id != decided.event_id


def _events(trial_id: str, report_sha256: str, decision: str = "REJECTED") -> list:  # type: ignore[type-arg]
    return [
        validated_event(trial_id, "a1", SPEC, report_sha256, T0),
        gate_decided_event(trial_id, "a1", SPEC, report_sha256, decision, T0),  # type: ignore[arg-type]
    ]


def test_verify_reports_accepts_a_report_that_is_the_events_own(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path)
    digest = store.write_report(_report()).sha256

    verify_reports(_events("trial-1", digest), store)


def test_verify_reports_ignores_events_that_name_no_report(tmp_path: Path) -> None:
    from tests.unit.research.test_promotion import _sealed

    verify_reports([_sealed("1" * 64)], EvidenceStore(tmp_path))


def test_verify_reports_refuses_a_missing_report(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path)
    digest = store.write_report(_report()).sha256
    (tmp_path / digest[:2] / f"{digest}.json").unlink()

    with pytest.raises(EvidenceIntegrityError):
        verify_reports(_events("trial-1", digest), store)


def test_verify_reports_refuses_an_altered_report(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path)
    digest = store.write_report(_report()).sha256
    path = tmp_path / digest[:2] / f"{digest}.json"
    path.write_bytes(path.read_bytes().replace(b"attempt-1", b"attempt-2"))

    with pytest.raises(EvidenceIntegrityError):
        verify_reports(_events("trial-1", digest), store)


def test_verify_reports_refuses_a_report_of_another_trial(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path)
    digest = store.write_report(_report()).sha256

    with pytest.raises(EvidenceIntegrityError):
        verify_reports(_events("trial-2", digest), store)


@pytest.mark.parametrize("decision", ["RESEARCH_PASSED", "PAPER_APPROVED"])
def test_verify_reports_refuses_a_decision_that_is_not_the_reports(
    tmp_path: Path, decision: str
) -> None:
    store = EvidenceStore(tmp_path)
    digest = store.write_report(_report()).sha256

    with pytest.raises(EvidenceIntegrityError):
        verify_reports(_events("trial-1", digest, decision), store)


def test_verify_reports_checks_the_validated_event_too_not_only_the_decision(
    tmp_path: Path,
) -> None:
    store = EvidenceStore(tmp_path)
    digest = store.write_report(_report()).sha256
    (tmp_path / digest[:2] / f"{digest}.json").unlink()

    with pytest.raises(EvidenceIntegrityError):
        verify_reports(_events("trial-1", digest)[:1], store)
