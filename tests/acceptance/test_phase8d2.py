"""Phase 8D2 acceptance: the one-time holdout opening and the package contract.

Real PostgreSQL, a real evidence store, real bars. Because gate 9 (capacity) is UNAVAILABLE
for every candidate, no REAL decision reaches RESEARCH_PASSED or PAPER_APPROVED, so the rules
that start from those answers are driven by the NAMED TEST HELPER in
``tests/integration/research/synthetic_decision.py``, which appends real events for a decision no
candidate can earn. What is real: the bars, the runs, the bundles, the ledger, the store, the
commands and their refusals. What is synthetic: the two decisions the helper writes, and nothing
else.
"""

# ruff: noqa: F811
from __future__ import annotations

import json
import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
import pytest
from pydantic import SecretStr
from typer.testing import CliRunner

from tests.acceptance.test_phase8b2b import _assert_refused, _envelope
from tests.acceptance.test_phase8d1 import _locked_protocol as _locked_unit_protocol
from tests.conftest import DatabaseHarness
from tests.integration.marketdata.conftest import seed
from tests.integration.research.conftest import research_env  # noqa: F401
from tests.integration.research.synthetic_decision import append_synthetic_decision
from tests.integration.research.test_backtest_evidence import ORIGIN, Fixture, _ramp
from tests.integration.research.test_compounding import _compounding, _files, _rows
from tests.integration.research.test_scenarios import (
    TRIAL_ID,
    _protocol,
    _scenario_args,
    _scenarios,
)
from tests.integration.research.test_trial_cli import _bundle as _unit_bundle
from tests.integration.research.test_trial_cli import _ledger, _store, _verify
from tests.integration.research.test_trial_cli import _result as _unit_result
from tests.unit.ops.test_scenarios import _protocol as _unit_protocol
from tests.unit.research.backtest.conftest import _contract
from trading_house import cli
from trading_house.database.connection import open_runtime_connection
from trading_house.marketdata.store import PostgresBarStore
from trading_house.ops.ledger import gate_decided_event, seal_bundle
from trading_house.ops.scenarios import baseline_at, sealed_bundles
from trading_house.research import promotion as promotion_constants
from trading_house.research.backtest.sizing import SizingMode
from trading_house.research.canonical import canonical_sha256
from trading_house.research.evidence import EvidenceBundle
from trading_house.research.packages import StrategyPackage
from trading_house.research.promotion import Decision
from trading_house.research.trial_ledger import (
    HoldoutSpec,
    HoldoutState,
    LedgerEventType,
    ReturnSeriesBasis,
    TrialProtocol,
)

runner = CliRunner()

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("isolated_research_ledger")]

BAR_DAYS = 45
RESEARCH_DAYS = 35
"""Thirty-five days of research bars (the walk-forward and CPCV reads need thirty or more), then
ten days that the protocol locks as its holdout."""
HOLDOUT_HASH = "9" * 64
AT = "2026-10-01T12:00:00"
BARS_PER_DAY = 96


@pytest.fixture(scope="module")
def opening_seeded(
    database: DatabaseHarness, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[Fixture]:
    """A real store holding 45 days of M15 bars, emptied afterwards."""

    bars = _ramp(BAR_DAYS)
    store = PostgresBarStore(lambda: open_runtime_connection(SecretStr(database.runtime_dsn)))
    seed(store, bars)
    contract = tmp_path_factory.mktemp("opening") / "contract.json"
    contract.write_text(_contract().model_dump_json(), encoding="utf-8")
    try:
        yield Fixture(
            contract=contract,
            first_bar=bars[0].event_time,
            last_bar=bars[-1].event_time,
            mid_session_bar=bars[33].event_time,
        )
    finally:
        with (
            psycopg.connect(database.test_superuser_dsn, autocommit=True) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute("TRUNCATE marketdata.bars, marketdata.ingest_runs")


def _locked_protocol(
    seeded: Fixture,
    *,
    holdout_end_days: int = 0,
    commission: str | None = None,
) -> TrialProtocol:
    """The grid's own protocol with its research window cut short and a holdout locked behind it.

    ``holdout_end_days`` moves the holdout's end that many days PAST the last stored bar, for
    the coverage refusal. ``commission`` replaces the declared per-lot commission, which the
    grid, the opened runs and every check then all read from the protocol.
    """

    base = _protocol(seeded)
    research_end = ORIGIN + timedelta(minutes=15 * (RESEARCH_DAYS * BARS_PER_DAY - 1))
    holdout_start = ORIGIN + timedelta(minutes=15 * RESEARCH_DAYS * BARS_PER_DAY)
    update: dict[str, Any] = {
        "data": base.data.model_copy(update={"end": research_end}),
        "holdout": HoldoutSpec(
            state=HoldoutState.LOCKED,
            start=holdout_start,
            end=seeded.last_bar + timedelta(days=holdout_end_days),
            dataset_sha256=HOLDOUT_HASH,
        ),
    }
    if commission is not None:
        baseline = base.costs.baseline.model_copy(
            update={"commission_per_lot_per_side": Decimal(commission)}
        )
        update["costs"] = base.costs.model_copy(update={"baseline": baseline})
    return base.model_copy(update=update)


def _register(tmp_path: Path, protocol: TrialProtocol, name: str = "protocol.json") -> Path:
    path = tmp_path / name
    path.write_text(protocol.model_dump_json(), encoding="utf-8")
    result = runner.invoke(cli.app, ["research", "trial", "register", "--protocol", str(path)])
    assert result.exit_code == cli.ExitCode.OK, result.stderr
    return path


def _open_args(seeded: Fixture, protocol_path: Path, **overrides: str) -> list[str]:
    argv = _scenario_args(seeded, protocol_path, **overrides)
    argv[2] = "open-holdout"
    return argv


def _open(seeded: Fixture, protocol_path: Path, **overrides: str) -> Any:
    return runner.invoke(cli.app, _open_args(seeded, protocol_path, **overrides))


def _trial(*argv: str) -> Any:
    return runner.invoke(cli.app, ["research", "trial", *argv])


def _decide(trial_id: str = TRIAL_ID, at: str = AT) -> Any:
    return _trial("decide", "--trial-id", trial_id, "--occurred-at", at)


def _holdout(trial_id: str = TRIAL_ID) -> tuple[str, int]:
    payload = _envelope(_trial("holdout", "--trial-id", trial_id))
    return payload["state"], payload["opened_bundle_count"]


def _state(dsn: str, root: Path) -> tuple[int, int]:
    return _rows(dsn), _files(root)


def _synthetic(
    dsn: str, root: Path, want: Decision, trial_id: str = TRIAL_ID, hour: int = 13
) -> str:
    return append_synthetic_decision(
        trial_id,
        _ledger(dsn),
        _store(root),
        want,
        datetime(2026, 10, 1, hour, tzinfo=UTC),
    )


def _expectancy(bundle: EvidenceBundle) -> float:
    """Net P&L per closed trade, written out here and not read from the code under test."""

    trades = bundle.result.trades
    return float(sum((t.net_pnl for t in trades), Decimal(0)) / len(trades))


def _candidate_digest(protocol: TrialProtocol) -> str:
    return canonical_sha256(protocol.candidates[0])


# --- 1. the opening, and decide reading what it sealed -----------------------------------------


@pytest.mark.parametrize(("commission", "status"), [("3.50", "PASS"), ("60", "FAIL")])
def test_the_opening_seals_three_opened_bundles_and_decide_then_evaluates_gate_two(
    opening_seeded: Fixture,
    research_env: Path,
    research_ledger_dsn: str,
    tmp_path: Path,
    commission: str,
    status: str,
) -> None:
    protocol = _locked_protocol(opening_seeded, commission=commission)
    path = _register(tmp_path, protocol)
    _scenarios(opening_seeded, path)
    assert _compounding(opening_seeded, path).exit_code == cli.ExitCode.OK
    assert _holdout() == ("locked", 0)
    store = _store(research_env)
    _synthetic(research_ledger_dsn, research_env, Decision.RESEARCH_PASSED)
    rows, files = _state(research_ledger_dsn, research_env)
    counters = _envelope(_trial("count"))
    untouched = {
        name: _trial(name, "--trial-id", TRIAL_ID).stdout
        for name in ("scenario-report", "splits", "compounding-report")
    }
    validated = _envelope(_trial("validate", "--trial-id", TRIAL_ID))

    result = _open(opening_seeded, path)

    assert result.exit_code == cli.ExitCode.OK, result.stderr
    payload = _envelope(result)
    assert payload["trial_id"] == TRIAL_ID
    assert [b["multiplier"] for b in payload["bundles"]] == ["1", "1.5", "2"]
    assert [b["attempt_id"] for b in payload["bundles"]] == [
        "grid-holdout-1",
        "grid-holdout-1.5",
        "grid-holdout-2",
    ]
    assert payload["holdout"]["state"] == "opened"
    assert payload["holdout"]["opened_bundle_count"] == 3
    # three attempts, each a start, a result and a seal: nine rows and three documents
    assert _state(research_ledger_dsn, research_env) == (rows + 9, files + 3)
    appended = _ledger(research_ledger_dsn).events()[-9:]
    assert [r.event_type for r in appended] == [
        LedgerEventType.EXECUTION_STARTED,
        LedgerEventType.RESULT_RECORDED,
        LedgerEventType.EVIDENCE_SEALED,
    ] * 3
    assert [r.attempt_id for r in appended[::3]] == [b["attempt_id"] for b in payload["bundles"]]
    after = _envelope(_trial("count"))
    assert after["audit_attempts"] == counters["audit_attempts"] + 3
    assert after["selection_lotteries"] == counters["selection_lotteries"]
    assert after["effective_specifications"] == counters["effective_specifications"]

    # every bundle is the protocol's candidate, on the HOLDOUT window, at the declared costs
    holdout = protocol.holdout
    opened = {}
    for entry in payload["bundles"]:
        bundle = store.read(entry["evidence_sha256"])
        level = Decimal(entry["multiplier"])
        opened[level] = (entry["evidence_sha256"], bundle)
        assert bundle.provenance.holdout_state is HoldoutState.OPENED
        assert bundle.provenance.dataset_sha256 == HOLDOUT_HASH == holdout.dataset_sha256
        assert (bundle.result.start, bundle.result.end) == (holdout.start, holdout.end)
        assert bundle.result.cost_model == baseline_at(protocol, level)
        assert bundle.sizing is SizingMode.CONSTANT_NOTIONAL
        assert bundle.trial_id == TRIAL_ID
        assert bundle.spec_sha256 == _candidate_digest(protocol)
        assert bundle.return_series_basis is ReturnSeriesBasis.MARK_TO_MARKET
        assert bundle.mark_to_market is not None
    assert _holdout() == ("opened", 3)

    # the opened bundles change nothing the research reads (the 8B3 filter, end to end)
    for name, text in untouched.items():
        assert _trial(name, "--trial-id", TRIAL_ID).stdout == text, name
    revalidated = _envelope(_trial("validate", "--trial-id", TRIAL_ID))
    for field in (
        "max_drawdown_baseline",
        "max_drawdown_cpcv_paths",
        "max_drawdown_compounding",
        "cpcv_p5",
        "coverage",
        "scenario_expectancy",
        "wfa_folds",
        "first_day",
        "last_day",
        "series_days",
    ):
        assert revalidated[field] == validated[field], field
    # the chain head and the counters move with the opening; the research digests do not
    assert {k: v for k, v in revalidated["evidence"].items() if k != "chain_head"} == {
        k: v for k, v in validated["evidence"].items() if k != "chain_head"
    }
    assert not {d for d, _ in opened.values()} & set(revalidated["evidence"]["pbo_candidates"])
    first_rerun = _scenarios(opening_seeded, path)  # every level is already sealed: a no-op
    assert _state(research_ledger_dsn, research_env) == (rows + 9, files + 3)
    assert not {s["evidence_sha256"] for s in first_rerun["scenarios"]} & {
        d for d, _ in opened.values()
    }

    # decide now reads the opened 1.5x and 2.0x bundles: gate 2 evaluates, and consumes the holdout
    decided = _decide()
    assert decided.exit_code == cli.ExitCode.OK, decided.stderr
    verdict = _envelope(decided)
    expected = [_expectancy(opened[Decimal("1.5")][1]), _expectancy(opened[Decimal(2)][1])]
    gate = verdict["gates"][1]
    assert gate["name"] == "locked_oos_expectancy"
    assert gate["status"] == status
    assert gate["threshold"] == 0.0
    assert gate["evidence_sha256"] == [opened[Decimal("1.5")][0], opened[Decimal(2)][0]]
    if status == "PASS":
        assert all(value > 0 for value in expected)
        assert gate["measured_value"] == pytest.approx(min(expected), abs=1e-9)
    else:
        assert not all(value > 0 for value in expected)
        assert gate["measured_value"] == pytest.approx(
            next(v for v in expected if not v > 0), abs=1e-9
        )
    assert verdict["holdout"]["state"] == "opened"
    assert verdict["decision"] == "REJECTED"  # capacity: no candidate can pass gate 9
    assert verdict["gates"][8]["status"] == "UNAVAILABLE"
    baseline = store.read(validated["evidence"]["baseline"])
    assert verdict["blocking_reasons"] == (
        ["negative expectancy at baseline costs"] if _expectancy(baseline) < 0 else []
    ) + ["dataset-content hash is unavailable"]
    assert _holdout() == ("consumed", 3)
    assert _verify().exit_code == cli.ExitCode.OK

    # a consumed holdout cannot be opened again, and nothing was written trying
    after_decision = _state(research_ledger_dsn, research_env)
    _assert_refused(_open(opening_seeded, path), cli.ExitCode.PROMOTION_REFUSED)
    assert _state(research_ledger_dsn, research_env) == after_decision

    # a rerun reads the same bundles but now finds the holdout consumed: a second report that
    # says so and decides the same thing; a third is a no-op
    rerun = _envelope(_decide())
    assert rerun["holdout"]["state"] == "consumed"
    assert (rerun["decision"], rerun["gates"][1]) == (verdict["decision"], verdict["gates"][1])
    assert rerun["report_sha256"] != verdict["report_sha256"]
    settled = _state(research_ledger_dsn, research_env)
    assert _envelope(_decide())["already_recorded"] is True
    assert _state(research_ledger_dsn, research_env) == settled
    assert _verify().exit_code == cli.ExitCode.OK


# --- 2. every refusal of the opening writes nothing ---------------------------------------------


def test_every_refusal_of_the_opening_writes_nothing_and_the_opening_still_works_after_them(
    opening_seeded: Fixture,
    research_env: Path,
    research_ledger_dsn: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    protocol = _locked_protocol(opening_seeded)
    path = _register(tmp_path, protocol)
    _scenarios(opening_seeded, path)
    edited = tmp_path / "edited.json"
    edited.write_text(
        protocol.model_copy(
            update={"execution": protocol.execution.model_copy(update={"seed": "changed"})}
        ).model_dump_json(),
        encoding="utf-8",
    )

    def refused(code: int, *, argv_path: Path = path, **options: str) -> None:
        before = _state(research_ledger_dsn, research_env)
        _assert_refused(_open(opening_seeded, argv_path, **options), code)
        assert _state(research_ledger_dsn, research_env) == before
        assert _holdout()[0] != "opened"

    # no decision of the trial has ever been recorded
    refused(cli.ExitCode.PROMOTION_REFUSED)
    # the latest decision is a real one, and it is REJECTED
    assert _decide().exit_code == cli.ExitCode.OK
    refused(cli.ExitCode.PROMOTION_REFUSED)
    # the latest is RESEARCH_PASSED; now each remaining refusal in turn
    _synthetic(research_ledger_dsn, research_env, Decision.RESEARCH_PASSED)
    refused(cli.ExitCode.CONFIGURATION, **{"--trial-id": "nobody"})
    refused(cli.ExitCode.SCENARIO_EVIDENCE, argv_path=edited)  # not the registered protocol
    refused(cli.ExitCode.SCENARIO_EVIDENCE, **{"--firm-equity": "50000"})  # other replay inputs
    refused(cli.ExitCode.CONFIGURATION, **{"--agent-run-id": " "})  # provenance validated first
    refused(cli.ExitCode.CONFIGURATION, **{"--atr-period": "0"})
    refused(cli.ExitCode.CONFIGURATION, **{"--firm-equity": "not-a-number"})
    signed = promotion_constants.DSR_MINIMUM
    monkeypatch.setattr(promotion_constants, "DSR_MINIMUM", 0.5)
    refused(cli.ExitCode.PROMOTION_REFUSED)  # the policy moved since the decision
    monkeypatch.setattr(promotion_constants, "DSR_MINIMUM", signed)

    # none of that spent anything: the opening is still available, once
    assert _holdout() == ("locked", 0)
    assert _open(opening_seeded, path).exit_code == cli.ExitCode.OK
    assert _holdout() == ("opened", 3)
    before = _state(research_ledger_dsn, research_env)
    _assert_refused(_open(opening_seeded, path), cli.ExitCode.PROMOTION_REFUSED)  # a second call
    assert _state(research_ledger_dsn, research_env) == before


@pytest.mark.parametrize(
    "registered",
    [
        HoldoutState.NOT_DEFINED,
        HoldoutState.OPENED,
        HoldoutState.CONSUMED,
        HoldoutState.CONTAMINATED,
    ],
)
def test_a_holdout_that_is_not_locked_is_never_opened(
    opening_seeded: Fixture,
    research_env: Path,
    research_ledger_dsn: str,
    tmp_path: Path,
    registered: HoldoutState,
) -> None:
    base = _protocol(opening_seeded)
    holdout = (
        HoldoutSpec(state=registered)
        if registered in {HoldoutState.NOT_DEFINED, HoldoutState.CONTAMINATED}
        else _locked_protocol(opening_seeded).holdout.model_copy(update={"state": registered})
    )
    path = _register(tmp_path, base.model_copy(update={"holdout": holdout}))
    before = _state(research_ledger_dsn, research_env)

    _assert_refused(_open(opening_seeded, path), cli.ExitCode.PROMOTION_REFUSED)

    assert _state(research_ledger_dsn, research_env) == before


def test_a_holdout_window_outside_the_stored_bars_is_refused_before_any_write(
    opening_seeded: Fixture, research_env: Path, research_ledger_dsn: str, tmp_path: Path
) -> None:
    path = _register(tmp_path, _locked_protocol(opening_seeded, holdout_end_days=2))
    _scenarios(opening_seeded, path)
    _synthetic(research_ledger_dsn, research_env, Decision.RESEARCH_PASSED)
    before = _state(research_ledger_dsn, research_env)

    _assert_refused(_open(opening_seeded, path), cli.ExitCode.SCENARIO_EVIDENCE)

    assert _state(research_ledger_dsn, research_env) == before
    assert _holdout() == ("locked", 0)


# --- 3. decide after a partial opening is refused, and the holdout stays opened ------------------


def _hand_sealed_opened(
    research_env: Path, dsn: str, level: str, attempt_id: str, **provenance: Any
) -> str:
    """A NAMED TEST HELPER: an opened bundle sealed through ``seal_bundle``, the lower-level path.

    It is what a crashed opening leaves, and it is NOT reachable through ``research trial record``,
    which refuses such a bundle (``test_record_cannot_seal_an_opening``).
    """

    store, ledger = _store(research_env), _ledger(dsn)
    baseline = next(
        bundle
        for _, bundle in sealed_bundles(ledger.events_for(TRIAL_ID), store.read)
        if bundle.result.cost_model.stress_multiplier == Decimal(level)
        and bundle.provenance.holdout_state is HoldoutState.NOT_DEFINED
    )
    opened = baseline.model_copy(
        update={
            "attempt_id": attempt_id,
            "provenance": baseline.provenance.model_copy(
                update={
                    "holdout_state": HoldoutState.OPENED,
                    "dataset_sha256": HOLDOUT_HASH,
                    **provenance,
                }
            ),
        }
    )
    return seal_bundle(opened, ledger=ledger, store=store)


def test_decide_after_a_partial_opening_is_refused_writes_nothing_and_keeps_the_holdout_opened(
    opening_seeded: Fixture,
    research_env: Path,
    research_ledger_dsn: str,
    tmp_path: Path,
) -> None:
    path = _register(tmp_path, _locked_protocol(opening_seeded))
    _scenarios(opening_seeded, path)
    _synthetic(research_ledger_dsn, research_env, Decision.RESEARCH_PASSED)

    def decide_is_refused(code: int = cli.ExitCode.PROMOTION_REFUSED) -> None:
        before = _state(research_ledger_dsn, research_env)
        _assert_refused(_decide(), code)
        assert _state(research_ledger_dsn, research_env) == before

    # one bundle sealed, as a crash after the first level would leave it
    _hand_sealed_opened(research_env, research_ledger_dsn, "1", "partial-1")
    assert _holdout() == ("opened", 1)
    decide_is_refused()
    assert _holdout() == ("opened", 1)  # the refusal did not consume it

    # two bundles: the 1.5x is there, the 2.0x is not
    _hand_sealed_opened(research_env, research_ledger_dsn, "1.5", "partial-1.5")
    assert _holdout() == ("opened", 2)
    decide_is_refused()
    assert _holdout() == ("opened", 2)

    # and the opening cannot be repeated to finish it: a half-opened holdout is final
    before = _state(research_ledger_dsn, research_env)
    _assert_refused(_open(opening_seeded, path), cli.ExitCode.PROMOTION_REFUSED)
    assert _state(research_ledger_dsn, research_env) == before

    # all three levels now exist, each exactly once, but they are copies of the research runs on
    # the RESEARCH window and not the holdout's: decide refuses them as unfaithful, writing nothing
    _hand_sealed_opened(research_env, research_ledger_dsn, "2", "partial-2")
    assert _holdout() == ("opened", 3)
    decide_is_refused(cli.ExitCode.SCENARIO_EVIDENCE)
    assert _holdout() == ("opened", 3)


def test_a_second_opening_at_an_already_opened_level_contaminates_and_decide_records_it(
    opening_seeded: Fixture,
    research_env: Path,
    research_ledger_dsn: str,
    tmp_path: Path,
) -> None:
    """A contaminated holdout cannot be consumed, so deciding it is allowed and says REJECTED."""

    path = _register(tmp_path, _locked_protocol(opening_seeded))
    _scenarios(opening_seeded, path)
    _synthetic(research_ledger_dsn, research_env, Decision.RESEARCH_PASSED)
    _hand_sealed_opened(research_env, research_ledger_dsn, "1", "first-1")
    _hand_sealed_opened(research_env, research_ledger_dsn, "1", "second-1")
    assert _holdout() == ("contaminated", 2)

    verdict = _envelope(_decide())

    assert verdict["decision"] == "REJECTED"
    assert "no locked unseen holdout" in verdict["blocking_reasons"]
    assert verdict["gates"][1]["status"] == "UNAVAILABLE"
    assert _holdout()[0] == "contaminated"


# --- 4. the package commands -------------------------------------------------------------------
#
# A cheap chain: a registered locked protocol, one sealed research baseline and (later) one sealed
# opened bundle, with the two decisions no real candidate can earn appended by the synthetic helper.
# The package rules read only the chain's decisions, so nothing here needs a simulation.

PACKAGE_TRIAL = "trial-1"
BASE = [
    "--trial-id",
    PACKAGE_TRIAL,
    "--book",
    "fx_scalp",
    "--horizon",
    "scalp",
    "--asset-class",
    "fx",
]


def _package_chain(research_env: Path, dsn: str, tmp_path: Path) -> TrialProtocol:
    """Registered with a locked holdout; one research baseline sealed; nothing decided."""

    path = _locked_unit_protocol(tmp_path, PACKAGE_TRIAL)
    assert _trial("register", "--protocol", str(path)).exit_code == cli.ExitCode.OK
    seal_bundle(
        _unit_bundle(PACKAGE_TRIAL, "base-1", _unit_result()),
        ledger=_ledger(dsn),
        store=_store(research_env),
    )
    return TrialProtocol.model_validate_json(path.read_text(encoding="utf-8"))


def _open_cheaply(
    research_env: Path, dsn: str, trial_id: str = PACKAGE_TRIAL, attempt_id: str = "open-1"
) -> None:
    """One opened bundle sealed by hand: enough for the derived holdout to read OPENED."""

    bundle = _unit_bundle(trial_id, attempt_id, _unit_result())
    opened = bundle.model_copy(
        update={
            "provenance": bundle.provenance.model_copy(
                update={"holdout_state": HoldoutState.OPENED}
            )
        }
    )
    seal_bundle(opened, ledger=_ledger(dsn), store=_store(research_env))


def _create(*argv: str) -> Any:
    return runner.invoke(cli.app, ["research", "package", "create", *argv])


def _verify_package(*argv: str) -> Any:
    return runner.invoke(cli.app, ["research", "package", "verify", *argv])


def _package_file(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _edited(path: Path, tmp_path: Path, name: str, **change: Any) -> Path:
    edited = tmp_path / name
    edited.write_text(json.dumps({**_package_file(path), **change}), encoding="utf-8")
    return edited


def test_a_package_is_created_and_verified_against_the_chain_and_neither_writes_to_it(
    research_env: Path, research_ledger_dsn: str, tmp_path: Path
) -> None:
    protocol = _package_chain(research_env, research_ledger_dsn, tmp_path)
    research_passed = _synthetic(research_ledger_dsn, research_env, Decision.RESEARCH_PASSED)
    paper_file, live_file = tmp_path / "paper.json", tmp_path / "live.json"
    paper_args = [*BASE, "--stage", "paper", "--authorization-ref", "AUTH-1", "--out"]

    # a RESEARCH_PASSED decision is not an approval: nothing is written, nothing is appended
    before = _state(research_ledger_dsn, research_env)
    _assert_refused(_create(*paper_args, str(paper_file)), cli.ExitCode.PROMOTION_REFUSED)
    assert _state(research_ledger_dsn, research_env) == before
    assert not paper_file.exists()
    # and verify refuses a package that names that decision's report as an approval
    forged = tmp_path / "forged.json"
    forged.write_text(
        json.dumps(
            {
                "package_id": "pkg-forged",
                "spec": {
                    "spec_id": "spec-trial-1",
                    "hypothesis": "declared before any result existed",
                    "book": "fx_scalp",
                    "horizon": "scalp",
                    "asset_classes": ["fx"],
                },
                "source_sha256": protocol.strategy_sha256,
                "trial_ledger_reference": _ledger(research_ledger_dsn).events()[-1].event_hash,
                "validation_report_sha256": research_passed,
                "signature_sha256": None,
                "stage": "paper",
                "authorization_ref": "AUTH-1",
                "capital_authorization_ref": None,
            }
        ),
        encoding="utf-8",
    )
    _assert_refused(_verify_package("--file", str(forged)), cli.ExitCode.PROMOTION_REFUSED)

    _open_cheaply(research_env, research_ledger_dsn)
    approved = _synthetic(research_ledger_dsn, research_env, Decision.PAPER_APPROVED, hour=14)
    assert _holdout()[0] == "consumed"  # a decision after the opening consumed it
    before = _state(research_ledger_dsn, research_env)
    head = _ledger(research_ledger_dsn).events()[-1].event_hash

    created = _create(*paper_args, str(paper_file))

    assert created.exit_code == cli.ExitCode.OK, created.stderr
    summary = _envelope(created)
    assert summary["stage"] == "paper"
    assert summary["out"] == str(paper_file)
    paper = _package_file(paper_file)
    assert paper == {
        "package_id": f"trial-1-paper-{approved[:12]}",
        "spec": {
            "spec_id": "spec-trial-1",
            "hypothesis": "declared before any result existed",
            "book": "fx_scalp",
            "horizon": "scalp",
            "asset_classes": ["fx"],
        },
        "source_sha256": protocol.strategy_sha256,
        "trial_ledger_reference": head,
        "validation_report_sha256": approved,
        "signature_sha256": None,
        "stage": "paper",
        "authorization_ref": "AUTH-1",
        "capital_authorization_ref": None,
    }
    assert summary["package_sha256"] == canonical_sha256(
        StrategyPackage.model_validate_json(paper_file.read_text(encoding="utf-8"))
    )
    # the command wrote its file and nothing else
    assert _state(research_ledger_dsn, research_env) == before
    assert _verify().exit_code == cli.ExitCode.OK

    verified = _verify_package("--file", str(paper_file))
    assert verified.exit_code == cli.ExitCode.OK, verified.stderr
    assert _envelope(verified) == {
        "valid": True,
        "trial_id": PACKAGE_TRIAL,
        "stage": "paper",
        "report_sha256": approved,
        "decision": "PAPER_APPROVED",
    }
    assert _state(research_ledger_dsn, research_env) == before

    # LIVE: the paper package, a signature reference and a capital authorization that is not the
    # paper one. Nothing signs anything: the references are carried as typed.
    live_args = [
        *BASE,
        "--stage",
        "live",
        "--authorization-ref",
        "AUTH-1",
        "--capital-authorization-ref",
        "CAP-1",
        "--signature-sha256",
        "SIG-1",
        "--paper-package",
        str(paper_file),
        "--out",
    ]
    live = _create(*live_args, str(live_file))
    assert live.exit_code == cli.ExitCode.OK, live.stderr
    package = _package_file(live_file)
    assert (package["stage"], package["authorization_ref"]) == ("live", "AUTH-1")
    assert (package["capital_authorization_ref"], package["signature_sha256"]) == ("CAP-1", "SIG-1")
    assert (
        _verify_package("--file", str(live_file), "--paper-package", str(paper_file)).exit_code == 0
    )
    assert _state(research_ledger_dsn, research_env) == before


def test_a_package_that_the_rules_do_not_license_is_never_written(
    research_env: Path, research_ledger_dsn: str, tmp_path: Path
) -> None:
    _package_chain(research_env, research_ledger_dsn, tmp_path)
    _open_cheaply(research_env, research_ledger_dsn)
    _synthetic(research_ledger_dsn, research_env, Decision.PAPER_APPROVED, hour=14)
    paper_file = tmp_path / "paper.json"
    paper = [*BASE, "--stage", "paper", "--authorization-ref", "AUTH-1", "--out", str(paper_file)]
    assert _create(*paper).exit_code == cli.ExitCode.OK
    original = paper_file.read_bytes()
    live_flags = [
        "--stage",
        "live",
        "--authorization-ref",
        "AUTH-1",
        "--capital-authorization-ref",
        "CAP-1",
        "--signature-sha256",
        "SIG-1",
        "--paper-package",
        str(paper_file),
    ]
    before = _state(research_ledger_dsn, research_env)

    def live_refused(code: int, *, drop: tuple[str, ...] = (), **replace: str) -> None:
        out = tmp_path / f"live-{len(list(tmp_path.glob('live-*.json')))}.json"
        flags = list(live_flags)
        for option in drop:
            at = flags.index(option)
            del flags[at : at + 2]
        for option, value in replace.items():
            flags[flags.index(option) + 1] = value
        _assert_refused(_create(*BASE, *flags, "--out", str(out)), code)
        assert not out.exists()
        assert _state(research_ledger_dsn, research_env) == before

    refused = cli.ExitCode.PROMOTION_REFUSED
    live_refused(refused, drop=("--paper-package",))  # a decision never reaches LIVE by itself
    live_refused(refused, drop=("--signature-sha256",))
    live_refused(refused, drop=("--capital-authorization-ref",))
    live_refused(refused, **{"--capital-authorization-ref": "AUTH-1"})  # the paper one again
    live_refused(refused, **{"--authorization-ref": "SOMEONE-ELSE"})  # not the one it continues
    live_refused(cli.ExitCode.CONFIGURATION, **{"--paper-package": str(tmp_path / "missing.json")})

    # a LIVE package is not a paper package
    live_file = tmp_path / "live.json"
    assert _create(*BASE, *live_flags, "--out", str(live_file)).exit_code == cli.ExitCode.OK
    live_refused(refused, **{"--paper-package": str(live_file)})

    # required options, an invalid enum, an invalid book and an existing output
    nothing = _state(research_ledger_dsn, research_env)
    no_book = [*BASE[:2], *BASE[4:]]
    no_asset = BASE[:-2]
    auth = ["--stage", "paper", "--authorization-ref", "A"]
    for argv in (
        [*BASE, "--stage", "paper", "--out", str(tmp_path / "a.json")],  # no authorization
        [
            *BASE,
            "--stage",
            "sandbox",
            "--authorization-ref",
            "A",
            "--out",
            str(tmp_path / "b.json"),
        ],
        [*no_asset, "--asset-class", "gold", *auth, "--out", str(tmp_path / "c.json")],
        [*no_book, "--book", "Not A Book", *auth, "--out", str(tmp_path / "d.json")],
    ):
        assert _create(*argv).exit_code == cli.ExitCode.CONFIGURATION, argv
    assert not any((tmp_path / name).exists() for name in ("a.json", "b.json", "c.json", "d.json"))
    assert _create(*paper).exit_code == cli.ExitCode.CONFIGURATION  # never overwrites
    assert paper_file.read_bytes() == original
    assert _state(research_ledger_dsn, research_env) == nothing


def test_verify_refuses_a_package_the_chain_does_not_support(
    research_env: Path, research_ledger_dsn: str, tmp_path: Path
) -> None:
    _package_chain(research_env, research_ledger_dsn, tmp_path)
    research_passed = _synthetic(research_ledger_dsn, research_env, Decision.RESEARCH_PASSED)
    _open_cheaply(research_env, research_ledger_dsn)
    approved = _synthetic(research_ledger_dsn, research_env, Decision.PAPER_APPROVED, hour=14)
    paper_file = tmp_path / "paper.json"
    paper_args = [*BASE, "--stage", "paper", "--authorization-ref", "AUTH-1", "--out"]
    assert _create(*paper_args, str(paper_file)).exit_code == cli.ExitCode.OK
    before = _state(research_ledger_dsn, research_env)
    refused = cli.ExitCode.PROMOTION_REFUSED

    def check(code: int, name: str, **change: Any) -> None:
        file = _edited(paper_file, tmp_path, name, **change)
        _assert_refused(_verify_package("--file", str(file)), code)
        assert _state(research_ledger_dsn, research_env) == before

    check(cli.ExitCode.EVIDENCE_INTEGRITY, "a.json", validation_report_sha256="0" * 64)
    check(cli.ExitCode.EVIDENCE_INTEGRITY, "b.json", validation_report_sha256="not-a-digest")
    # a real report of the trial, but not the one its latest decision names
    check(refused, "c.json", validation_report_sha256=research_passed)
    check(refused, "d.json", trial_ledger_reference="f" * 64)  # no such row in the chain
    check(refused, "e.json", source_sha256="e" * 64)  # not the protocol's strategy hash
    check(
        refused,
        "f.json",
        spec={**_package_file(paper_file)["spec"], "spec_id": "spec-another-candidate"},
    )
    check(
        refused,
        "g.json",
        spec={**_package_file(paper_file)["spec"], "hypothesis": "made up afterwards"},
    )
    # a stage the references do not support is a malformed package
    check(cli.ExitCode.CONFIGURATION, "h.json", authorization_ref=None)
    check(cli.ExitCode.CONFIGURATION, "i.json", stage="live")
    check(cli.ExitCode.CONFIGURATION, "j.json", stage="sandbox")
    # an unreadable file, and one that is not a package
    _assert_refused(
        _verify_package("--file", str(tmp_path / "missing.json")), cli.ExitCode.CONFIGURATION
    )
    (tmp_path / "junk.json").write_text("{}", encoding="utf-8")
    _assert_refused(
        _verify_package("--file", str(tmp_path / "junk.json")), cli.ExitCode.CONFIGURATION
    )
    # a LIVE package verifies only against the paper package it continues
    live_file = tmp_path / "live.json"
    assert (
        _create(
            *BASE,
            "--stage",
            "live",
            "--authorization-ref",
            "AUTH-1",
            "--capital-authorization-ref",
            "CAP-1",
            "--signature-sha256",
            "SIG-1",
            "--paper-package",
            str(paper_file),
            "--out",
            str(live_file),
        ).exit_code
        == cli.ExitCode.OK
    )
    _assert_refused(_verify_package("--file", str(live_file)), refused)
    _assert_refused(
        _verify_package("--file", str(live_file), "--paper-package", str(live_file)), refused
    )
    capital_is_paper = _edited(live_file, tmp_path, "k.json", capital_authorization_ref="AUTH-1")
    _assert_refused(
        _verify_package("--file", str(capital_is_paper), "--paper-package", str(paper_file)),
        refused,
    )
    assert _verify_package(
        "--file", str(live_file), "--paper-package", str(paper_file)
    ).exit_code == (cli.ExitCode.OK)
    assert approved != research_passed


def test_verify_refuses_a_decision_no_validated_event_names(
    research_env: Path, research_ledger_dsn: str, tmp_path: Path
) -> None:
    """The report digest must be named by a VALIDATED event AND by the latest GATE_DECIDED."""

    _package_chain(research_env, research_ledger_dsn, tmp_path)
    _open_cheaply(research_env, research_ledger_dsn)
    approved = _synthetic(research_ledger_dsn, research_env, Decision.PAPER_APPROVED, hour=14)
    paper_file = tmp_path / "paper.json"
    assert (
        _create(
            *BASE, "--stage", "paper", "--authorization-ref", "AUTH-1", "--out", str(paper_file)
        ).exit_code
        == cli.ExitCode.OK
    )
    assert _verify_package("--file", str(paper_file)).exit_code == cli.ExitCode.OK
    # a second, valid approval report of the same trial, and ONLY a GATE_DECIDED naming it
    ledger, store = _ledger(research_ledger_dsn), _store(research_env)
    report = store.read_report(approved)
    other = store.write_report(report.model_copy(update={"chain_head": "7" * 64})).sha256
    ledger.append(
        gate_decided_event(
            PACKAGE_TRIAL,
            report.attempt_id,
            report.spec_sha256,
            other,
            "PAPER_APPROVED",
            datetime(2026, 10, 1, 15, tzinfo=UTC),
        )
    )
    before = _state(research_ledger_dsn, research_env)
    twin = _edited(paper_file, tmp_path, "twin.json", validation_report_sha256=other)

    _assert_refused(_verify_package("--file", str(twin)), cli.ExitCode.PROMOTION_REFUSED)
    # and the package that named the earlier report is stale: the latest decision names another
    _assert_refused(_verify_package("--file", str(paper_file)), cli.ExitCode.PROMOTION_REFUSED)
    assert _state(research_ledger_dsn, research_env) == before


def test_an_opened_holdout_is_not_opened_again_even_while_the_last_decision_says_research_passed(
    opening_seeded: Fixture, research_env: Path, research_ledger_dsn: str, tmp_path: Path
) -> None:
    """Only the derived holdout state refuses here: the latest decision still allows an opening."""

    _package_chain(research_env, research_ledger_dsn, tmp_path)
    _synthetic(research_ledger_dsn, research_env, Decision.RESEARCH_PASSED)
    _open_cheaply(research_env, research_ledger_dsn)
    assert _holdout() == ("opened", 1)
    before = _state(research_ledger_dsn, research_env)

    _assert_refused(_open(opening_seeded, tmp_path / "locked.json"), cli.ExitCode.PROMOTION_REFUSED)

    assert _state(research_ledger_dsn, research_env) == before


def test_record_cannot_seal_an_opening(
    research_env: Path, research_ledger_dsn: str, tmp_path: Path
) -> None:
    _package_chain(research_env, research_ledger_dsn, tmp_path)
    _synthetic(research_ledger_dsn, research_env, Decision.RESEARCH_PASSED)
    before = _state(research_ledger_dsn, research_env)

    for state in (HoldoutState.OPENED, HoldoutState.CONSUMED, HoldoutState.LOCKED):
        bundle = _unit_bundle(PACKAGE_TRIAL, f"forged-{state.value}", _unit_result())
        forged = bundle.model_copy(
            update={"provenance": bundle.provenance.model_copy(update={"holdout_state": state})}
        )
        file = tmp_path / f"forged-{state.value}.json"
        file.write_text(forged.model_dump_json(), encoding="utf-8")

        refused = _trial(
            "record",
            "--trial-id",
            PACKAGE_TRIAL,
            "--attempt-id",
            f"forged-{state.value}",
            "--evidence",
            str(file),
        )

        _assert_refused(refused, cli.ExitCode.PROMOTION_REFUSED)
        assert _state(research_ledger_dsn, research_env) == before
    assert _holdout() == ("locked", 0)


def _locked_pair(tmp_path: Path, *ids: str, protocol_id: str = "pair", end_day: int = 1) -> Path:
    """A unit protocol declaring ``ids`` over a LOCKED holdout, written to disk (not registered)."""

    protocol = _unit_protocol(ids).model_copy(update={"protocol_id": protocol_id})
    holdout = HoldoutSpec(
        state=HoldoutState.LOCKED,
        start=datetime(2026, 1, 1, tzinfo=UTC),
        end=datetime(2026, 2, end_day, tzinfo=UTC),
        dataset_sha256="9" * 64,
    )
    path = tmp_path / f"{protocol_id}.json"
    path.write_text(protocol.model_copy(update={"holdout": holdout}).model_dump_json(), "utf-8")
    return path


def test_candidates_sharing_one_holdout_cannot_each_open_it_and_a_reregistration_cannot_reopen_it(
    opening_seeded: Fixture, research_env: Path, research_ledger_dsn: str, tmp_path: Path
) -> None:
    path = _locked_pair(tmp_path, "trial-1", "trial-2")
    assert _trial("register", "--protocol", str(path)).exit_code == cli.ExitCode.OK
    ledger, store = _ledger(research_ledger_dsn), _store(research_env)
    for trial in ("trial-1", "trial-2"):
        seal_bundle(
            _unit_bundle(trial, f"base-{trial}", _unit_result()), ledger=ledger, store=store
        )
        _synthetic(research_ledger_dsn, research_env, Decision.RESEARCH_PASSED, trial, hour=12)
    assert _holdout("trial-2") == ("locked", 0)

    _open_cheaply(research_env, research_ledger_dsn, "trial-1", "open-1")  # A opens it

    assert _holdout("trial-1") == ("opened", 1)  # not self-contaminated
    state = _envelope(_trial("holdout", "--trial-id", "trial-2"))
    assert state["state"] == "contaminated"
    assert state["reason"] == "this holdout was opened by trial trial-1"
    before = _state(research_ledger_dsn, research_env)
    refused = _open(opening_seeded, path, **{"--trial-id": "trial-2"})
    _assert_refused(refused, cli.ExitCode.PROMOTION_REFUSED)  # B's latest decision still allows it
    assert _state(research_ledger_dsn, research_env) == before

    # another protocol naming the same window and hash, with a new trial id
    again = _locked_pair(tmp_path, "trial-9", protocol_id="again")
    assert _trial("register", "--protocol", str(again)).exit_code == cli.ExitCode.OK
    assert _holdout("trial-9")[0] == "contaminated"
    # a different window, or a different hash, is a different holdout
    elsewhere = _locked_pair(tmp_path, "trial-8", protocol_id="elsewhere", end_day=2)
    assert _trial("register", "--protocol", str(elsewhere)).exit_code == cli.ExitCode.OK
    assert _holdout("trial-8") == ("locked", 0)


def test_a_package_is_refused_when_its_reference_is_stale_or_the_holdout_has_since_been_spent(
    research_env: Path, research_ledger_dsn: str, tmp_path: Path
) -> None:
    _package_chain(research_env, research_ledger_dsn, tmp_path)
    _open_cheaply(research_env, research_ledger_dsn)
    _synthetic(research_ledger_dsn, research_env, Decision.PAPER_APPROVED, hour=14)
    paper_file = tmp_path / "paper.json"
    paper = [*BASE, "--stage", "paper", "--authorization-ref", "AUTH-1"]
    assert _create(*paper, "--out", str(paper_file)).exit_code == cli.ExitCode.OK
    assert _verify_package("--file", str(paper_file)).exit_code == cli.ExitCode.OK
    refused = cli.ExitCode.PROMOTION_REFUSED

    # a ledger reference that precedes the latest decision licenses nothing
    first = _ledger(research_ledger_dsn).events()[0].event_hash
    stale = _edited(paper_file, tmp_path, "stale.json", trial_ledger_reference=first)
    _assert_refused(_verify_package("--file", str(stale)), refused)
    # a PAPER package carries no capital authorization and no signature
    for extra in (["--capital-authorization-ref", "CAP-1"], ["--signature-sha256", "SIG-1"]):
        out = tmp_path / "paper-extra.json"
        _assert_refused(_create(*paper, *extra, "--out", str(out)), refused)
        assert not out.exists()

    # a second opening at the same level spends the holdout: the old report no longer licenses it
    _open_cheaply(research_env, research_ledger_dsn, attempt_id="open-2")
    assert _holdout()[0] == "contaminated"
    before = _state(research_ledger_dsn, research_env)
    _assert_refused(_verify_package("--file", str(paper_file)), refused)
    fresh = tmp_path / "fresh.json"
    _assert_refused(_create(*paper, "--out", str(fresh)), refused)
    assert not fresh.exists()
    assert _state(research_ledger_dsn, research_env) == before


# --- 5. what the package commands are, and are not ----------------------------------------------

PACKAGE_COMMANDS = frozenset({"create", "verify"})
"""Every ``research package`` command. Both read the chain and write at most their ``--out``
file, and both speak in decisions in their help (they name the decision a package rests on), so
they are a NAMED set of their own and not a loosening of the trial commands' ban."""


def test_every_package_command_is_named_and_neither_can_append_sign_or_force() -> None:
    registered = {command.name for command in cli.package_app.registered_commands}
    assert registered == PACKAGE_COMMANDS

    for name in sorted(PACKAGE_COMMANDS):
        result = runner.invoke(cli.app, ["research", "package", name, "--help"])
        assert result.exit_code == 0, result.stderr
        text = " ".join(result.stdout.split()).lower()
        assert re.search(r"--(force|overwrite|sign|approve|decision|sandbox)", text) is None, name
        assert "signs nothing" in text, name
        assert "appends nothing" in text, name
    assert [stage.value for stage in cli.PackageStageName] == ["paper", "live"]


def test_no_command_can_reach_the_synthetic_helper() -> None:
    source = Path(cli.__file__).resolve().parents[1]
    offenders = [
        str(path)
        for path in source.rglob("*.py")
        if "synthetic_decision" in path.read_text(encoding="utf-8")
        or "append_synthetic_decision" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []
    assert "synthetic" not in " ".join(
        str(command.name) for command in cli.trial_app.registered_commands
    ) + " ".join(str(command.name) for command in cli.package_app.registered_commands)
