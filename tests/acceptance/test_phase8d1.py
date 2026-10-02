"""Phase 8D1 acceptance: ``research trial decide``, ``report`` and ``holdout``.

The first slice that judges. This file claims, against real PostgreSQL and a real evidence
store:

1. **A legacy trial is REJECTED with all six reasons** and nine gates none of which passes,
   by the same ``decide`` a prospective trial uses, and nothing in the system changes a
   stage or creates a package (the evidence root holds one bundle and one report).
2. **The reasons are computed from the evidence**, not hard-coded to a trial: a legacy trial
   with a profitable baseline names five.
3. **A prospective trial over a real sealed run is REJECTED today**, and its nine gate
   statuses equal an independent reading of ``validate``'s numbers against the spec's
   thresholds; capacity (gate 9) and the holdout (gate 2) are UNAVAILABLE, and the report's
   chain head is the last evidence-bearing row, not the decision it was appended by.
4. **A rerun on identical evidence is a no-op** (rows and files unchanged, same digest), in
   this process and in two others under different hash seeds.
5. **``verify`` re-reads reports**: a deleted or altered report file exits 17.
6. **A threshold that moves after a first report is refused** (exit 21) with nothing written.
7. **The holdout is derived from the real chain**: legacy contaminated, registered locked or
   not defined, one sealed opening opened, a decision after it consumed, a second opening
   contaminated.
8. **The refusals** (an undeclared trial, a trial both legacy and registered, no ``--stage``
   option, a missing ``--occurred-at``) write nothing.
9. **The help of ``decide``, ``report`` and ``holdout`` is the named exception, and the README
   says what is true**: the nine gates and their thresholds, the three-valued decision, the
   six reasons, that a decision moves no stage, and the limits of spec section 7.
"""

# ruff: noqa: F811
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from tests.acceptance.test_phase8b2b import (
    DECISION_COMMANDS,
    _assert_refused,
    _envelope,
    _verdict_claims,
)
from tests.acceptance.test_phase8c1 import long_seeded  # noqa: F401
from tests.integration.research.conftest import research_env  # noqa: F401
from tests.integration.research.test_backtest_evidence import Fixture
from tests.integration.research.test_compounding import _compounding, _files, _rows
from tests.integration.research.test_scenarios import TRIAL_ID, _register, _scenarios
from tests.integration.research.test_trial_cli import (
    _bundle,
    _import_legacy,
    _ledger,
    _result,
    _store,
    _trade,
    _verify,
    _write_phase7_artifact,
)
from tests.unit.ops.test_scenarios import _protocol as _unit_protocol
from trading_house import cli
from trading_house.ops.ledger import gate_decided_event, seal_bundle, validated_event
from trading_house.research import promotion
from trading_house.research.canonical import canonical_sha256
from trading_house.research.trial_ledger import (
    HoldoutSpec,
    HoldoutState,
    LedgerEvent,
    LedgerEventType,
)

runner = CliRunner()

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("isolated_research_ledger")]

ROOT = Path(__file__).resolve().parents[2]
AT = "2026-10-01T12:00:00"
LATER = "2026-10-02T12:00:00"
_AT_LATER = datetime(2026, 10, 2, 12, tzinfo=UTC)

SIX_REASONS = [
    "negative expectancy at baseline costs",
    "no locked unseen holdout",
    "legacy evidence was not preregistered",
    "dataset-content hash is unavailable",
    "mark-to-market returns are unavailable",
    "spread and slippage cannot be separately attributed",
]
GATE_ORDER = [
    "wfa_complete",
    "locked_oos_expectancy",
    "dsr_minimum",
    "pbo_maximum",
    "cpcv_p5_expectancy",
    "bootstrap_lower_bound",
    "max_drawdown",
    "oos_coverage",
    "capacity_model",
]


def _decide(trial_id: str, at: str = AT) -> Any:
    return runner.invoke(
        cli.app, ["research", "trial", "decide", "--trial-id", trial_id, "--occurred-at", at]
    )


def _trial(*argv: str) -> Any:
    return runner.invoke(cli.app, ["research", "trial", *argv])


def _losing_artifact(tmp_path: Path) -> Path:
    loss = _trade(gross_pnl=Decimal("-100"), net_pnl=Decimal("-100"))
    return _write_phase7_artifact(tmp_path / "phase7.json", _result(trades=(loss,)))


def _legacy_trial(tmp_path: Path, *, profitable: bool = False) -> str:
    artifact = (
        _write_phase7_artifact(tmp_path / "phase7.json", _result())
        if profitable
        else _losing_artifact(tmp_path)
    )
    trial_id = _import_legacy(artifact)["trial_id"]
    assert isinstance(trial_id, str)
    return trial_id


def _evidence_rows(dsn: str) -> list[Any]:
    return [
        r
        for r in _ledger(dsn).events()
        if r.event_type not in {LedgerEventType.VALIDATED, LedgerEventType.GATE_DECIDED}
    ]


# --- 1. the legacy trial: REJECTED, six reasons, nine blocked gates, no stage ---------------


def test_the_legacy_trial_is_rejected_with_all_six_reasons_and_nine_blocked_gates(
    research_env: Path, research_ledger_dsn: str, tmp_path: Path
) -> None:
    trial_id = _legacy_trial(tmp_path)
    rows, files = _rows(research_ledger_dsn), _files(research_env)
    counters = _envelope(_trial("count"))

    result = _decide(trial_id)

    assert result.exit_code == cli.ExitCode.OK, result.stderr
    payload = _envelope(result)
    assert payload["decision"] == "REJECTED"
    assert payload["blocking_reasons"] == SIX_REASONS
    assert [g["name"] for g in payload["gates"]] == GATE_ORDER
    assert [g["gate"] for g in payload["gates"]] == list(range(1, 10))
    assert [g["status"] for g in payload["gates"]] == ["UNAVAILABLE"] * 9
    assert all(g["status"] != "PASS" for g in payload["gates"])
    assert payload["holdout"]["state"] == "contaminated"
    assert payload["already_recorded"] is False

    # exactly one report and two events: VALIDATED then GATE_DECIDED, naming that report
    assert _files(research_env) == files + 1
    assert _rows(research_ledger_dsn) == rows + 2
    appended = _ledger(research_ledger_dsn).events()[-2:]
    assert [r.event_type for r in appended] == [
        LedgerEventType.VALIDATED,
        LedgerEventType.GATE_DECIDED,
    ]
    assert [r.event_json["payload"]["report_sha256"] for r in appended] == [
        payload["report_sha256"]
    ] * 2
    assert appended[1].event_json["payload"]["decision"] == "REJECTED"
    assert {r.trial_id for r in appended} == {trial_id}
    # scoped to the baseline attempt and carrying its specification digest
    report = _store(research_env).read_report(payload["report_sha256"])
    assert {r.attempt_id for r in appended} == {report.attempt_id}
    assert {r.scope_kind.value for r in appended} == {"attempt"}
    assert {r.scope_id for r in appended} == {report.attempt_id}
    assert {r.spec_sha256 for r in appended} == {report.spec_sha256}
    # the three deflation denominators are counted from starts and imports, never decisions
    assert _envelope(_trial("count")) == counters

    # nothing created a package or touched a stage: the root holds the bundle and the report
    assert _files(research_env) == 2
    assert not any("stage" in key or "package" in key for key in payload)


def test_a_decision_changes_no_stage_and_the_command_has_no_stage_option(
    research_env: Path, research_ledger_dsn: str, tmp_path: Path
) -> None:
    trial_id = _legacy_trial(tmp_path)
    rows, files = _rows(research_ledger_dsn), _files(research_env)

    for option in ("--stage", "--promote", "--package", "--threshold"):
        refused = _trial("decide", "--trial-id", trial_id, "--occurred-at", AT, option, "paper")
        assert refused.exit_code == cli.ExitCode.CONFIGURATION
        assert "No such option" in refused.stderr

    assert (_rows(research_ledger_dsn), _files(research_env)) == (rows, files)
    assert not any(
        "stage" in command.name
        for command in cli.trial_app.registered_commands
        if command.name is not None
    )


def test_the_reasons_come_from_the_evidence_not_from_the_trial(
    research_env: Path, tmp_path: Path
) -> None:
    """A legacy artifact with a profitable baseline: five reasons, the negative one absent."""

    trial_id = _legacy_trial(tmp_path, profitable=True)

    payload = _envelope(_decide(trial_id))

    assert payload["decision"] == "REJECTED"
    assert payload["blocking_reasons"] == SIX_REASONS[1:]


def test_a_rerun_on_identical_evidence_is_a_no_op_whatever_clock_it_is_given(
    research_env: Path, research_ledger_dsn: str, tmp_path: Path
) -> None:
    trial_id = _legacy_trial(tmp_path)
    first = _envelope(_decide(trial_id))
    rows, files = _rows(research_ledger_dsn), _files(research_env)

    again = _envelope(_decide(trial_id))
    later = _envelope(_decide(trial_id, LATER))

    assert again["already_recorded"] is True
    assert later["already_recorded"] is True
    assert again["report_sha256"] == later["report_sha256"] == first["report_sha256"]
    assert (_rows(research_ledger_dsn), _files(research_env)) == (rows, files)
    for field in ("decision", "gates", "blocking_reasons", "holdout"):
        assert again[field] == first[field]


def test_report_prints_what_decide_sealed_and_the_file_is_the_digest_it_names(
    research_env: Path, tmp_path: Path
) -> None:
    trial_id = _legacy_trial(tmp_path)
    decided = _envelope(_decide(trial_id))

    printed = _envelope(_trial("report", "--trial-id", trial_id))

    assert printed["report_sha256"] == decided["report_sha256"]
    report = printed["report"]
    assert report["trial_id"] == trial_id
    assert report["decision"] == "REJECTED"
    assert report["report_schema_version"] == 1
    assert report["evidence"] is None
    assert report["policy"]["validation"] is None
    assert report["policy_sha256"] == canonical_sha256(promotion.policy_digest_input(None))
    assert [g["status"] for g in report["gates"]] == ["UNAVAILABLE"] * 9
    stored = _store(research_env).read_report(decided["report_sha256"])
    assert stored.digest() == decided["report_sha256"] == canonical_sha256(stored)
    assert json.loads(stored.model_dump_json()) == report


def test_report_for_a_trial_with_no_decision_is_refused(
    research_env: Path, research_ledger_dsn: str, tmp_path: Path
) -> None:
    trial_id = _legacy_trial(tmp_path)
    rows, files = _rows(research_ledger_dsn), _files(research_env)

    result = _trial("report", "--trial-id", trial_id)

    _assert_refused(result, cli.ExitCode.PROMOTION_REFUSED)
    assert (_rows(research_ledger_dsn), _files(research_env)) == (rows, files)


# --- 5. verify re-reads the second document kind --------------------------------------------


def test_verify_re_reads_the_reports_a_deleted_or_altered_one_exits_17(
    research_env: Path, tmp_path: Path
) -> None:
    trial_id = _legacy_trial(tmp_path)
    digest = _envelope(_decide(trial_id))["report_sha256"]
    path = research_env / digest[:2] / f"{digest}.json"
    original = path.read_bytes()
    assert _verify().exit_code == cli.ExitCode.OK

    path.write_bytes(original.replace(b'"attempt_id"', b'"attempt_ix"', 1))
    altered = _verify()
    path.unlink()
    deleted = _verify()
    path.write_bytes(original)
    restored = _verify()

    assert altered.exit_code == cli.ExitCode.EVIDENCE_INTEGRITY
    assert deleted.exit_code == cli.ExitCode.EVIDENCE_INTEGRITY
    assert restored.exit_code == cli.ExitCode.OK


def test_a_decision_on_the_chain_that_is_not_the_reports_own_fails_report_and_verify(
    research_env: Path, research_ledger_dsn: str, tmp_path: Path
) -> None:
    """A GATE_DECIDED naming a real report but another decision: the chain and the report
    disagree about what was decided, and nothing may read either as the truth."""

    trial_id = _legacy_trial(tmp_path)
    digest = _envelope(_decide(trial_id))["report_sha256"]
    genuine = _ledger(research_ledger_dsn).events()[-1]
    forged = LedgerEvent.model_validate_json(
        json.dumps(
            {
                **genuine.event_json,
                "event_id": str(uuid4()),
                "payload": {**genuine.event_json["payload"], "decision": "PAPER_APPROVED"},
            }
        )
    )
    assert forged.payload.report_sha256 == digest  # type: ignore[union-attr]
    _ledger(research_ledger_dsn).append(forged)

    _assert_refused(_trial("report", "--trial-id", trial_id), cli.ExitCode.EVIDENCE_INTEGRITY)
    assert _verify().exit_code == cli.ExitCode.EVIDENCE_INTEGRITY


def test_report_prints_the_last_decision_and_refuses_one_naming_another_trials_report(
    research_env: Path, research_ledger_dsn: str, tmp_path: Path
) -> None:
    ledger, store = _ledger(research_ledger_dsn), _store(research_env)
    trial_a = _legacy_trial(tmp_path)
    first = _envelope(_decide(trial_a))["report_sha256"]
    genuine = store.read_report(first)

    # a later, different report for the same trial, recorded the way decide records one
    later = genuine.model_copy(update={"chain_head": "9" * 64})
    later_digest = store.write_report(later).sha256
    for event in (
        validated_event(trial_a, genuine.attempt_id, genuine.spec_sha256, later_digest, _AT_LATER),
        gate_decided_event(
            trial_a, genuine.attempt_id, genuine.spec_sha256, later_digest, "REJECTED", _AT_LATER
        ),
    ):
        ledger.append(event)
    printed = _envelope(_trial("report", "--trial-id", trial_a))
    assert printed["report_sha256"] == later_digest
    assert printed["report"]["chain_head"] == "9" * 64  # the later report's own content

    # a decision of trial B naming trial A's report is refused as an integrity failure
    other = tmp_path / "other"
    other.mkdir()
    artifact = _write_phase7_artifact(
        other / "phase7.json",
        _result(
            run_id="run-2",
            trades=(_trade(gross_pnl=Decimal("-50"), net_pnl=Decimal("-50")),),
        ),
    )
    trial_b = _import_legacy(artifact)["trial_id"]
    assert trial_b != trial_a
    ledger.append(gate_decided_event(trial_b, "attempt-b", "a" * 64, first, "REJECTED", _AT_LATER))

    _assert_refused(_trial("report", "--trial-id", trial_b), cli.ExitCode.EVIDENCE_INTEGRITY)


def test_report_refuses_a_report_file_that_was_altered(research_env: Path, tmp_path: Path) -> None:
    trial_id = _legacy_trial(tmp_path)
    digest = _envelope(_decide(trial_id))["report_sha256"]
    path = research_env / digest[:2] / f"{digest}.json"
    path.write_bytes(path.read_bytes().replace(b'"chain_head"', b'"chain_hear"', 1))

    _assert_refused(_trial("report", "--trial-id", trial_id), cli.ExitCode.EVIDENCE_INTEGRITY)


# --- 8. refusals write nothing -------------------------------------------------------------


def test_an_undeclared_trial_and_a_declared_trial_with_nothing_sealed_are_refused(
    research_env: Path, research_ledger_dsn: str, tmp_path: Path
) -> None:
    protocol = tmp_path / "p.json"
    protocol.write_text(_unit_protocol(("trial-1",)).model_dump_json(), encoding="utf-8")
    assert _trial("register", "--protocol", str(protocol)).exit_code == cli.ExitCode.OK
    rows, files = _rows(research_ledger_dsn), _files(research_env)

    undeclared = _decide("nobody")
    unsealed = _decide("trial-1")

    _assert_refused(undeclared, cli.ExitCode.SCENARIO_EVIDENCE)
    _assert_refused(unsealed, cli.ExitCode.SCENARIO_EVIDENCE)
    assert (_rows(research_ledger_dsn), _files(research_env)) == (rows, files)


def test_a_trial_that_is_both_legacy_and_registered_is_refused(
    research_env: Path, research_ledger_dsn: str, tmp_path: Path
) -> None:
    trial_id = _legacy_trial(tmp_path)
    protocol = tmp_path / "p.json"
    protocol.write_text(_unit_protocol((trial_id,)).model_dump_json(), encoding="utf-8")
    assert _trial("register", "--protocol", str(protocol)).exit_code == cli.ExitCode.OK
    rows, files = _rows(research_ledger_dsn), _files(research_env)

    result = _decide(trial_id)

    _assert_refused(result, cli.ExitCode.PROMOTION_REFUSED)
    assert (_rows(research_ledger_dsn), _files(research_env)) == (rows, files)


def test_the_decision_time_is_required(research_env: Path, tmp_path: Path) -> None:
    trial_id = _legacy_trial(tmp_path)

    result = _trial("decide", "--trial-id", trial_id)

    assert result.exit_code == cli.ExitCode.CONFIGURATION
    assert "--occurred-at" in result.stderr


def test_a_holdout_for_an_undeclared_trial_is_refused(research_env: Path) -> None:
    _assert_refused(_trial("holdout", "--trial-id", "nobody"), cli.ExitCode.SCENARIO_EVIDENCE)


# --- 7. the holdout, derived from the real chain --------------------------------------------


def _locked_protocol(tmp_path: Path, trial_id: str = "trial-1") -> Path:
    protocol = _unit_protocol((trial_id,))
    locked = protocol.model_copy(
        update={
            "holdout": HoldoutSpec(
                state=HoldoutState.LOCKED,
                start=datetime(2026, 1, 1, tzinfo=UTC),
                end=datetime(2026, 2, 1, tzinfo=UTC),
                dataset_sha256="9" * 64,
            )
        }
    )
    path = tmp_path / "locked.json"
    path.write_text(locked.model_dump_json(), encoding="utf-8")
    return path


def _holdout(trial_id: str = "trial-1") -> tuple[str, int]:
    payload = _envelope(_trial("holdout", "--trial-id", trial_id))
    return payload["state"], payload["opened_bundle_count"]


def test_the_holdout_is_derived_from_the_chain_through_every_state(
    research_env: Path, research_ledger_dsn: str, tmp_path: Path
) -> None:
    unlocked = tmp_path / "unlocked.json"
    unlocked.write_text(_unit_protocol(("trial-9",)).model_dump_json(), encoding="utf-8")
    assert _trial("register", "--protocol", str(unlocked)).exit_code == cli.ExitCode.OK
    assert _holdout("trial-9") == ("not_defined", 0)

    assert (
        _trial("register", "--protocol", str(_locked_protocol(tmp_path))).exit_code
        == cli.ExitCode.OK
    )
    assert _holdout() == ("locked", 0)

    ledger, store = _ledger(research_ledger_dsn), _store(research_env)

    def seal_opened(attempt_id: str) -> str:
        bundle = _bundle("trial-1", attempt_id, _result())
        opened = bundle.model_copy(
            update={
                "provenance": bundle.provenance.model_copy(
                    update={"holdout_state": HoldoutState.OPENED}
                )
            }
        )
        return seal_bundle(opened, ledger=ledger, store=store)

    def decide_event() -> None:
        ledger.append(
            gate_decided_event(
                "trial-1", "a1", "a" * 64, "e" * 64, "REJECTED", datetime(2026, 10, 1, tzinfo=UTC)
            )
        )

    decide_event()  # a decision BEFORE any opening does not consume it
    assert _holdout() == ("locked", 0)
    seal_opened("a1")
    assert _holdout() == ("opened", 1)
    decide_event_again = gate_decided_event(
        "trial-1", "a1", "a" * 64, "f" * 64, "REJECTED", datetime(2026, 10, 2, tzinfo=UTC)
    )
    ledger.append(decide_event_again)
    assert _holdout() == ("consumed", 1)
    seal_opened("a2")
    assert _holdout() == ("contaminated", 2)
    seal_opened("a3")  # terminal: nothing heals it
    assert _holdout()[0] == "contaminated"


def test_the_holdout_of_a_legacy_trial_is_contaminated(
    research_env: Path, research_ledger_dsn: str, tmp_path: Path
) -> None:
    trial_id = _legacy_trial(tmp_path)
    rows, files = _rows(research_ledger_dsn), _files(research_env)

    payload = _envelope(_trial("holdout", "--trial-id", trial_id))

    assert payload["state"] == "contaminated"
    assert "legacy" in payload["reason"]
    assert (_rows(research_ledger_dsn), _files(research_env)) == (rows, files)


def test_a_snapshot_that_excludes_the_decision_types_has_the_head_of_the_last_other_row(
    research_env: Path, research_ledger_dsn: str, tmp_path: Path
) -> None:
    trial_id = _legacy_trial(tmp_path)
    ledger = _ledger(research_ledger_dsn)
    before = ledger.snapshot()
    _decide(trial_id)
    excluded = frozenset({LedgerEventType.VALIDATED, LedgerEventType.GATE_DECIDED})

    after_all = ledger.snapshot()
    after_evidence = ledger.snapshot(excluded)

    assert after_evidence == before
    assert after_all[1] != before[1]
    assert len(after_all[0]) == len(before[0]) + 2
    assert after_evidence[1] == _evidence_rows(research_ledger_dsn)[-1].event_hash


# --- 3. the prospective trial over a real sealed run --------------------------------------


def _independent_statuses(v: dict[str, Any]) -> list[str]:
    """The nine statuses from ``validate``'s numbers and the spec's thresholds, in plain code."""

    def status(measure: dict[str, Any], ok: Any) -> str:
        if measure["value"] is None:
            return "UNAVAILABLE"
        return "PASS" if ok(measure["value"]) else "FAIL"

    drawdowns = [
        v["max_drawdown_baseline"],
        *v["max_drawdown_cpcv_paths"],
        v["max_drawdown_compounding"],
    ]
    limit = 0.10 - 1e-12
    halt_and_loss = [v["monte_carlo"]["p_halt"], v["monte_carlo"]["p_loss"]]
    if any(d["value"] is not None and d["value"] >= limit for d in drawdowns):
        g7 = "FAIL"
    elif any(m["value"] is None for m in [*drawdowns, *halt_and_loss]):
        g7 = "UNAVAILABLE"
    else:
        g7 = "PASS"
    trades, regimes = v["coverage"]["oos_trades"], v["coverage"]["regimes_represented"]
    if (trades["value"] is not None and trades["value"] < 30) or (
        regimes["value"] is not None and regimes["value"] < 2
    ):
        g8 = "FAIL"
    elif trades["value"] is None or regimes["value"] is None:
        g8 = "UNAVAILABLE"
    else:
        g8 = "PASS"
    return [
        status(v["wfa_folds"], lambda x: x >= 1),
        "UNAVAILABLE",  # no holdout has ever been opened for any candidate
        status(v["dsr"]["dsr"], lambda x: x >= 0.95),
        status(v["pbo"]["pbo"], lambda x: x <= 0.5),
        status(v["cpcv_p5"]["p5"], lambda x: x > 0),
        status(v["bootstrap"]["bootstrap_lower_bound"], lambda x: x > 0),
        g7,
        g8,
        "UNAVAILABLE",  # no capacity model can be declared
    ]


def _grid_with_compounding(seeded: Fixture, tmp_path: Path) -> None:
    protocol_path = _register(tmp_path, seeded)
    _scenarios(seeded, protocol_path)
    assert _compounding(seeded, protocol_path).exit_code == cli.ExitCode.OK


def test_a_prospective_trial_over_a_real_sealed_run_is_rejected_today(
    long_seeded: Fixture, research_env: Path, research_ledger_dsn: str, tmp_path: Path
) -> None:
    _grid_with_compounding(long_seeded, tmp_path)
    validated = _envelope(_trial("validate", "--trial-id", TRIAL_ID))
    baseline = _store(research_env).read(validated["evidence"]["baseline"])
    rows, files = _rows(research_ledger_dsn), _files(research_env)

    result = _decide(TRIAL_ID)

    assert result.exit_code == cli.ExitCode.OK, result.stderr
    payload = _envelope(result)
    assert payload["decision"] == "REJECTED"
    assert [g["name"] for g in payload["gates"]] == GATE_ORDER
    assert [g["status"] for g in payload["gates"]] == _independent_statuses(validated)
    assert payload["gates"][8]["status"] == "UNAVAILABLE"  # capacity: always, today
    assert payload["gates"][1]["status"] == "UNAVAILABLE"  # holdout: never opened
    assert payload["holdout"]["state"] == "not_defined"
    assert "capacity is unavailable" in payload["gates"][8]["reason"]
    # every PASS names the sealed evidence it rests on, and the chain holds all of it
    chain_digests = {
        r.event_json["payload"]["evidence_sha256"]
        for r in _ledger(research_ledger_dsn).events()
        if r.event_type is LedgerEventType.EVIDENCE_SEALED
    }
    for gate in payload["gates"]:
        if gate["status"] == "PASS":
            assert gate["evidence_sha256"]
            assert set(gate["evidence_sha256"]) <= chain_digests

    # the reasons, derived by hand from the sealed baseline: not a legacy trial, mark-to-market,
    # complete attribution; the backtest sealed no dataset hash, and no holdout is defined
    expectancy = sum((t.net_pnl for t in baseline.result.trades), Decimal(0))
    assert baseline.provenance.dataset_sha256 is None
    expected = (["negative expectancy at baseline costs"] if expectancy < 0 else []) + [
        "no locked unseen holdout",
        "dataset-content hash is unavailable",
    ]
    assert payload["blocking_reasons"] == expected

    # the report: its chain head is the last evidence row, not the decision just appended
    report = _store(research_env).read_report(payload["report_sha256"])
    assert report.chain_head == _evidence_rows(research_ledger_dsn)[-1].event_hash
    assert report.evidence is not None
    assert report.evidence.evidence.chain_head == report.chain_head
    assert report.evidence.attempt_id == report.attempt_id == baseline.attempt_id
    assert report.spec_sha256 == baseline.spec_sha256
    assert report.policy.validation is not None
    assert report.policy_sha256 == promotion.policy_sha256(report.policy.validation)
    assert (_rows(research_ledger_dsn), _files(research_env)) == (rows + 2, files + 1)

    # idempotent: the second run reads the same evidence and finds both events
    again = _envelope(_decide(TRIAL_ID, LATER))
    assert again["already_recorded"] is True
    assert again["report_sha256"] == payload["report_sha256"]
    assert (_rows(research_ledger_dsn), _files(research_env)) == (rows + 2, files + 1)
    # and so does `report`
    assert (
        _envelope(_trial("report", "--trial-id", TRIAL_ID))["report_sha256"]
        == (payload["report_sha256"])
    )
    assert _verify().exit_code == cli.ExitCode.OK
    assert _envelope(_trial("holdout", "--trial-id", TRIAL_ID))["state"] == "not_defined"


def _decide_in_a_new_process(hash_seed: str) -> str:
    completed = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "trading_house.cli",
            "research",
            "trial",
            "decide",
            "--trial-id",
            TRIAL_ID,
            "--occurred-at",
            AT,
        ],
        env={**os.environ, "PYTHONHASHSEED": hash_seed},
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout


def test_the_decision_is_deterministic_across_processes_and_a_moved_threshold_is_refused(
    long_seeded: Fixture,
    research_env: Path,
    research_ledger_dsn: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _grid_with_compounding(long_seeded, tmp_path)
    first = _envelope(_decide(TRIAL_ID))
    rows, files = _rows(research_ledger_dsn), _files(research_env)

    # two fresh interpreters under different hash seeds recompute the report from the chain;
    # each finds the same digest already recorded, which it could not if either differed
    out_a = json.loads(_decide_in_a_new_process("1"))
    out_b = json.loads(_decide_in_a_new_process("2"))

    assert out_a["report_sha256"] == out_b["report_sha256"] == first["report_sha256"]
    assert out_a["already_recorded"] is out_b["already_recorded"] is True
    assert out_a == out_b
    assert (_rows(research_ledger_dsn), _files(research_env)) == (rows, files)

    # a threshold that moves after the trial's first report is refused, with nothing written
    signed = promotion.DSR_MINIMUM
    monkeypatch.setattr(promotion, "DSR_MINIMUM", 0.5)
    refused = _decide(TRIAL_ID)
    _assert_refused(refused, cli.ExitCode.PROMOTION_REFUSED)
    assert (_rows(research_ledger_dsn), _files(research_env)) == (rows, files)
    monkeypatch.setattr(promotion, "DSR_MINIMUM", signed)  # not undo(): that drops the env too
    assert _envelope(_decide(TRIAL_ID))["already_recorded"] is True

    # deleting the report is caught by verify, and report refuses it
    digest = first["report_sha256"]
    (research_env / digest[:2] / f"{digest}.json").unlink()
    assert _verify().exit_code == cli.ExitCode.EVIDENCE_INTEGRITY
    _assert_refused(_trial("report", "--trial-id", TRIAL_ID), cli.ExitCode.EVIDENCE_INTEGRITY)


# --- 9. the help and the README ----------------------------------------------------------------


@pytest.mark.parametrize("command", sorted(DECISION_COMMANDS))
def test_the_decision_commands_help_is_the_named_exception_and_says_what_it_does_not_do(
    command: str,
) -> None:
    result = runner.invoke(cli.app, ["research", "trial", command, "--help"])

    assert result.exit_code == 0, result.stderr
    assert command in DECISION_COMMANDS


def test_decide_help_speaks_in_decisions_so_the_exception_is_needed_and_says_it_moves_nothing() -> (
    None
):
    text = " ".join(
        runner.invoke(cli.app, ["research", "trial", "decide", "--help"]).stdout.split()
    )

    assert _verdict_claims(text) != []
    assert "creates no package, changes no stage and takes no stage option" in text
    for word in ("REJECTED", "RESEARCH_PASSED", "PAPER_APPROVED"):
        assert word in text


README = (ROOT / "README.md").read_text(encoding="utf-8")


def _flat(text: str) -> str:
    return " ".join(text.split())


def _section(heading: str) -> str:
    start = README.index(heading)
    end = README.find("\n## ", start + 1)
    return README[start : end if end != -1 else len(README)]


def test_the_readme_documents_the_three_commands_in_the_table_and_the_section() -> None:
    for command in ("decide", "report", "holdout"):
        rows = [
            line
            for line in README.splitlines()
            if line.startswith("| `") and f"trading-house research trial {command}`" in line
        ]
        assert len(rows) == 1, command
    assert README.index("## Phase 8C3") < README.index("## Phase 8D1")
    assert "research trial decide --trial-id T --occurred-at" in _flat(_section("## Phase 8D1"))
    assert "| 21 |" in README


def test_the_readme_states_the_gates_the_decision_the_reasons_and_the_limits() -> None:
    section = _flat(_section("## Phase 8D1"))

    for sentence in (
        "`DSR_MINIMUM = 0.95`",
        "`PBO_MAXIMUM = 0.50`",
        "`MAX_DRAWDOWN = 0.10`",
        "`MIN_OOS_TRADES = 30`",
        "`MIN_REGIMES = 2`",
        "`MIN_WFA_FOLDS = 1`",
        "Thresholds are not options",
        "passes only when the drawdown is `< MAX_DRAWDOWN - DRAWDOWN_TOLERANCE`",
        "`UNAVAILABLE` blocks and `FAIL` blocks",
        "`PASS` requires a defined, finite measurement on the right side of its threshold",
        "`PAPER_APPROVED` only when all nine gates pass",
        "`RESEARCH_PASSED` only when every research gate passes and the holdout is still `LOCKED`",
        "`NOT_DEFINED` or `CONTAMINATED` holdout can never reach `RESEARCH_PASSED`",
        "negative expectancy at baseline costs",
        "no locked unseen holdout",
        "legacy evidence was not preregistered",
        "dataset-content hash is unavailable",
        "mark-to-market returns are unavailable",
        "spread and slippage cannot be separately attributed",
        "A decision moves no stage and creates no package",
        "Gate 2 (the holdout) and gate 9 (capacity) are `UNAVAILABLE` for every candidate today",
        "so every decision is `REJECTED` today",
        "The first report of a trial pins its policy digest",
        "A rerun on identical evidence appends nothing",
        "`research trial verify` re-reads every report",
        "The report is a second document kind in the existing evidence store",
        "The ledger vouches for no `VALIDATED` or `GATE_DECIDED` row",
        "`research trial decide`, `report` and `holdout` are the only trial commands whose help "
        "may speak in decisions",
    ):
        assert sentence in section, sentence


def test_the_readme_does_not_claim_a_stage_can_change_or_that_a_candidate_can_pass() -> None:
    section = _flat(_section("## Phase 8D1")).lower()

    for claim in (
        "moves the candidate",
        "promotes the candidate",
        "is promoted",
        "reaches paper",
        "can currently pass",
        "creates a package",
        "changes the stage",
    ):
        assert claim not in section, claim
