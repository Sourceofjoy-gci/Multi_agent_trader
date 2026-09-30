"""Phase 8B3 acceptance: the compounding rerun, the typed capacity state, and the
three 8B2 defects it closes, through the real CLI against PostgreSQL.

8B3 adds a second experiment beside the grid and three refusals in front of it.
What this file is for is the cross-cutting claims no single unit case can make:

1. **constant-notional identity did not move.** The one pinned digest a reader can
   re-derive is re-derived, and a 1.0x run keeps the run id Task 1 recorded before
   it changed anything.
2. **a rerun is a different experiment, everywhere it is read.** It has its own
   run id, its sealed bytes say so, and the grid report does not see it.
3. **the new reports state no verdict**, in their keys, their values and their
   ``--help`` text (the last is gated in ``test_phase8b2b.py``, which names all
   five commands).
4. **the three defects are closed where the operator meets them**: a refusal
   that used to come after nine writes now comes before the first, and a stressed
   baseline is named rather than silently reported.
5. **the README says what is now true.**

Fixtures are the real ones, imported from the suites that own them.
"""

# ruff: noqa: F811
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from tests.acceptance.test_phase8b1 import V1_BUNDLE_SHA256
from tests.acceptance.test_phase8b2b import (
    _NO_VERDICT_HELP,
    _assert_refused,
    _envelope,
    _names_and_text,
)
from tests.integration.research.conftest import research_env  # noqa: F401
from tests.integration.research.test_backtest_evidence import (
    Fixture,
    _bundle_of,
    _run,
    seeded,  # noqa: F401
)
from tests.integration.research.test_compounding import (
    _compounding,
    _compounding_args,
    _edited_protocol_file,
    _files,
    _rows,
)
from tests.integration.research.test_scenarios import (
    TRIAL_ID,
    _protocol,
    _register,
    _scenario_args,
    _scenario_report,
    _scenarios,
)
from tests.integration.research.test_trial_cli import _store
from tests.property.test_trial_evidence import _bundle
from tests.unit.ops.test_scenarios import _NO_VERDICT
from tests.unit.ops.test_scenarios import _protocol as _unit_protocol
from tests.unit.ops.test_scenarios import _sealed as _unit_sealed
from tests.unit.ops.test_scenarios import _trial_id as _unit_trial_id
from tests.unit.research.backtest.conftest import _ramp
from tests.unit.research.backtest.test_engine import _PRE_CHANGE_RUN_ID, _run_with_cost
from trading_house import cli
from trading_house.ops.compounding import CompoundingReport
from trading_house.ops.scenarios import scenario_report
from trading_house.research.backtest.sizing import SizingMode
from trading_house.research.canonical import canonical_sha256

runner = CliRunner()

README = (Path(__file__).resolve().parents[2] / "README.md").read_text(encoding="utf-8")

NEW_COMMANDS = ("compounding", "compounding-report", "capacity")


def _flow(seeded: Fixture, tmp_path: Path) -> dict[str, Any]:
    """Register, run the grid, run the rerun. Returns the compounding payload."""

    protocol_path = _register(tmp_path, seeded)
    _scenarios(seeded, protocol_path)
    result = _compounding(seeded, protocol_path)
    assert result.exit_code == cli.ExitCode.OK, result.stderr
    return json.loads(result.stdout)


def _document(research_env: Path, digest: str) -> dict[str, Any]:
    """The sealed file's own bytes, decoded, and not a model's view of them."""

    (path,) = research_env.rglob(f"{digest}.json")
    return json.loads(path.read_text(encoding="utf-8"))


# --- 1. constant-notional identity is untouched --------------------------------


def test_the_rederivable_digest_and_the_baseline_run_id_are_the_pinned_literals() -> None:
    """The two constants 8B3's own subject could most plausibly have moved.

    8B3 folds scenario identity into ``run_id``, which is exactly the change that
    would move every constant-notional id if the rule were written on the wrong
    side of a default. The v1 bundle digest is re-derived from a live model; the
    1.0x run id is the literal Task 1 recorded *before* touching the engine, and
    both spellings of one is asserted to land on it.
    """

    assert canonical_sha256(_bundle()) == V1_BUNDLE_SHA256
    bars = _ramp(60)
    assert _run_with_cost(bars, Decimal(1)).run_id == _PRE_CHANGE_RUN_ID
    assert _run_with_cost(bars, Decimal("1.0")).run_id == _PRE_CHANGE_RUN_ID
    assert _run_with_cost(bars, Decimal("1.5")).run_id != _PRE_CHANGE_RUN_ID


@pytest.mark.integration
@pytest.mark.usefixtures("isolated_research_ledger")
def test_a_stressed_run_and_a_baseline_run_of_one_request_have_different_run_ids(
    seeded: Fixture, research_env: Path
) -> None:
    """Defect 2, through the CLI: the same request, only the multiplier differs."""

    baseline = _bundle_of(_run(seeded, marked=True, **{"--stress-multiplier": "1"}))
    stressed = _bundle_of(_run(seeded, marked=True, **{"--stress-multiplier": "1.5"}))
    default = _bundle_of(_run(seeded, marked=True, **{"--stress-multiplier": "1.0"}))

    assert stressed.result.run_id != baseline.result.run_id
    assert baseline.result.run_id == default.result.run_id
    assert baseline.result.digest() != stressed.result.digest()


# --- 2. the rerun is its own experiment ----------------------------------------


@pytest.mark.integration
@pytest.mark.usefixtures("isolated_research_ledger")
def test_a_compounding_bundle_never_enters_the_scenario_grid(
    seeded: Fixture, research_env: Path, research_ledger_dsn: str, tmp_path: Path
) -> None:
    protocol_path = _register(tmp_path, seeded)
    _scenarios(seeded, protocol_path)
    before = _scenario_report()
    assert before.exit_code == cli.ExitCode.OK, before.stderr

    assert _compounding(seeded, protocol_path).exit_code == cli.ExitCode.OK
    after = _scenario_report()

    assert after.exit_code == cli.ExitCode.OK, after.stderr
    assert after.stdout == before.stdout
    # Four documents are sealed and the report still reads three: the filter is
    # what makes those two numbers differ.
    assert _files(research_env) == 4
    assert len(json.loads(after.stdout)["scenarios"]) == 3
    verified = runner.invoke(cli.app, ["research", "trial", "verify"])
    assert verified.exit_code == cli.ExitCode.OK, verified.stderr
    assert json.loads(verified.stdout)["valid"] is True


@pytest.mark.integration
@pytest.mark.usefixtures("isolated_research_ledger")
def test_the_rerun_seals_sizing_and_a_series_and_constant_bundles_carry_no_sizing_key(
    seeded: Fixture, research_env: Path, tmp_path: Path
) -> None:
    payload = _flow(seeded, tmp_path)
    report = payload["report"]

    rerun = _document(research_env, report["compounding"]["evidence_sha256"])
    constant = _document(research_env, report["constant_notional"]["evidence_sha256"])

    assert rerun["sizing"] == SizingMode.COMPOUNDING.value
    assert rerun["mark_to_market"]["observations"], "a compounding bundle without a series"
    assert "sizing" not in constant
    grid = {row.name for row in research_env.rglob("*.json")}
    others = [
        _document(research_env, name.removesuffix(".json"))
        for name in grid
        if name.removesuffix(".json") != report["compounding"]["evidence_sha256"]
    ]
    assert len(others) == 3
    assert all("sizing" not in document for document in others)
    # Read back through the model, the store agrees with the bytes.
    store = _store(research_env)
    assert store.read(report["compounding"]["evidence_sha256"]).sizing is SizingMode.COMPOUNDING
    assert store.read(report["constant_notional"]["evidence_sha256"]).sizing is (
        SizingMode.CONSTANT_NOTIONAL
    )
    assert (
        report["compounding"]["source_result_sha256"]
        != (report["constant_notional"]["source_result_sha256"])
    )


# --- 3. no verdict in the new reports ------------------------------------------


@pytest.mark.integration
@pytest.mark.usefixtures("isolated_research_ledger")
def test_the_compounding_report_and_the_capacity_state_carry_no_decision_vocabulary(
    seeded: Fixture, research_env: Path, tmp_path: Path
) -> None:
    payload = _flow(seeded, tmp_path)
    standalone = runner.invoke(
        cli.app, ["research", "trial", "compounding-report", "--trial-id", TRIAL_ID]
    )
    capacity = runner.invoke(cli.app, ["research", "trial", "capacity", "--trial-id", TRIAL_ID])
    assert standalone.exit_code == capacity.exit_code == cli.ExitCode.OK

    report = CompoundingReport.model_validate_json(json.dumps(_envelope(standalone)))
    documents = [
        report.model_dump(mode="json"),
        _envelope(capacity),
        payload["capacity"],
    ]
    for document in documents:
        names = _names_and_text(document)
        assert names, "a document with no keys or values would pass vacuously"
        keys = _keys(document)
        assert keys, "no keys swept"
        assert [
            key for key in keys for word in (*_NO_VERDICT_HELP, "total") if word in key.lower()
        ] == []
        assert sorted(text for text in names for word in _NO_VERDICT if word in text.lower()) == []
    # The one sign a reader needs is a delta, never a ratio or a judgment.
    assert set(report.model_dump()) >= {"same_trade_sequence", "final_equity_difference"}


def _keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return {str(k) for k in value} | set().union(*(_keys(v) for v in value.values()))
    if isinstance(value, list):
        return set().union(*(_keys(v) for v in value))
    return set()


# --- 4. capacity ---------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.usefixtures("isolated_research_ledger")
def test_capacity_is_unavailable_for_every_registered_candidate_and_refused_for_others(
    seeded: Fixture, research_env: Path, tmp_path: Path
) -> None:
    _register(tmp_path, seeded)
    candidates = [candidate.trial_id for candidate in _protocol(seeded).candidates]
    assert len(candidates) >= 2

    for trial_id in candidates:
        result = runner.invoke(cli.app, ["research", "trial", "capacity", "--trial-id", trial_id])
        assert result.exit_code == cli.ExitCode.OK, result.stderr
        capacity = json.loads(result.stdout)["capacity"]
        assert capacity["status"] == "unavailable"
        assert capacity["reason"]

    unknown = runner.invoke(cli.app, ["research", "trial", "capacity", "--trial-id", "nobody"])
    grid = _scenario_report("nobody")
    assert unknown.exit_code == grid.exit_code == cli.ExitCode.SCENARIO_EVIDENCE
    assert unknown.stdout == "" == grid.stdout


# --- 5. the three defects ------------------------------------------------------


@pytest.mark.integration
@pytest.mark.usefixtures("isolated_research_ledger")
def test_an_edited_protocol_file_is_refused_with_zero_writes(
    seeded: Fixture, research_env: Path, research_ledger_dsn: str, tmp_path: Path
) -> None:
    """Defect 3: the refusal precedes the first append, on both commands."""

    protocol_path = _register(tmp_path, seeded)
    _scenarios(seeded, protocol_path)
    edited = _edited_protocol_file(seeded, tmp_path)
    rows, files = _rows(research_ledger_dsn), _files(research_env)

    for argv in (_scenario_args(seeded, edited), _compounding_args(seeded, edited)):
        result = runner.invoke(cli.app, argv)
        _assert_refused(result, cli.ExitCode.SCENARIO_EVIDENCE)
        assert (_rows(research_ledger_dsn), _files(research_env)) == (rows, files)


@pytest.mark.integration
@pytest.mark.usefixtures("isolated_research_ledger")
def test_a_level_sealed_under_another_attempt_is_refused_with_zero_writes(
    seeded: Fixture, research_env: Path, research_ledger_dsn: str, tmp_path: Path
) -> None:
    protocol_path = _register(tmp_path, seeded)
    _scenarios(seeded, protocol_path)
    rows, files = _rows(research_ledger_dsn), _files(research_env)

    result = runner.invoke(cli.app, _scenario_args(seeded, protocol_path, attempt_prefix="other"))

    _assert_refused(result, cli.ExitCode.SCENARIO_EVIDENCE)
    assert (_rows(research_ledger_dsn), _files(research_env)) == (rows, files)


def test_a_stressed_baseline_protocol_reports_the_multiplier_it_declared() -> None:
    """Defect 4: reportable, and named."""

    protocol = _unit_protocol(baseline_multiplier="1.5")
    report = scenario_report(
        trial_id=_unit_trial_id(protocol), protocol=protocol, sealed=_unit_sealed(protocol)
    )
    plain = _unit_protocol()
    unstressed = scenario_report(
        trial_id=_unit_trial_id(plain), protocol=plain, sealed=_unit_sealed(plain)
    )

    assert report.declared_baseline_multiplier == Decimal("1.5")
    assert unstressed.declared_baseline_multiplier == Decimal(1)


# --- 6. the README -------------------------------------------------------------


def _section(heading: str) -> str:
    start = README.index(heading)
    end = README.find("\n## ", start + 1)
    return README[start : end if end != -1 else len(README)]


def test_the_readme_documents_each_new_command_in_the_table_and_the_section() -> None:
    section = _section("## Phase 8B3")
    for command in NEW_COMMANDS:
        full = f"trading-house research trial {command}`"
        rows = [line for line in README.splitlines() if line.startswith("| `") and full in line]
        assert len(rows) == 1, command
        assert f"research trial {command}" in section, command
    assert "equity_exhausted" in section
    assert "same_trade_sequence" in section
    assert "UNAVAILABLE" in section
    assert "declared_baseline_multiplier" in section


def _flat(text: str) -> str:
    return " ".join(text.split())


def test_the_readme_no_longer_states_the_closed_limits_as_open() -> None:
    assert "Known limit: a run at 1.5x and the same run at 1.0x share" not in README
    assert "The shared-`run_id` limit still holds" not in README
    assert "refused — after its writes" not in README
    # What is still true stays said.
    assert "non-multiplier cost terms" in _section("## Phase 8B3")
    assert "before any write" in README


def test_the_readme_states_what_8b3_changed_and_does_not_overclaim_it() -> None:
    """Each corrected sentence is asserted present, so restoring an overclaim fails here
    even if the old wording is never searched for."""

    text, section = _flat(README), _flat(_section("## Phase 8B3"))

    # The retry claim: a sealed level is skipped, and the migration hazard is named.
    assert "is skipped outright" in text
    assert "A level already sealed under the run's own attempt id is skipped" in section
    assert "true no-op" in section
    assert "Warning: a chain sealed before 8B3" in section
    assert "skips the levels already sealed" in section
    # Identity: only the 1.0x level is byte-identical; stressed run ids moved by design.
    assert "No **1.0x** constant-notional run id, result digest or bundle digest moves" in section
    assert "The stressed levels' `run_id`s **do** change, by design" in section
    assert "and no constant-notional run id, result digest or bundle digest moves." not in section
    # Sizing: the old constant-equity sentence is scoped to the default sizing.
    assert "Compounding does not happen under the default sizing" in text
    assert "**Compounding does not happen.** `--firm-equity` is constant" not in text
    # Compounding reports and their limits.
    assert "cannot reach a sealed bundle" in section
    assert "counts as an audit attempt" in section
    assert "`ends_flat`" in section
    assert "closed trades' `proposal_id`s only" in section
    assert "the same replay inputs the protocol does not own" in section
    assert "not one transaction" in section
    assert "refuses an `--attempt-id` the trial has started and not sealed" in section
    assert "An orphaned `compounding` attempt id is spent" in section
    assert "audit_attempts` then rises by one" in section


def test_the_scenarios_help_text_does_not_claim_the_late_refusal() -> None:
    result = runner.invoke(cli.app, ["research", "trial", "scenarios", "--help"])
    text = " ".join(result.stdout.split())

    assert "What this cannot prevent" not in text
    assert "before anything is written" in text
    # The retry claim as it is now true, and as it was not.
    assert "skipped entirely" in text
    assert "a true no-op" in text
    assert "second document lands" not in text
    assert "recognised as a no-op" not in text


def test_the_compounding_help_text_states_its_pre_flight_and_its_retry() -> None:
    result = runner.invoke(cli.app, ["research", "trial", "compounding", "--help"])
    text = " ".join(result.stdout.split())

    assert "checks before the first write" in text
    assert "started and not sealed as this run's own rerun" in text
    assert "is a no-op that reports the existing digest" in text
    assert "its attempt id is spent. Retry under a NEW" in text
