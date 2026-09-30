"""``research trial compounding``, ``compounding-report`` and ``capacity``, and the
pre-flight that ``scenarios`` shares with ``compounding``, against real PostgreSQL.

Phase 8B3. The pre-flight tests all count chain rows and evidence files before and
after the refused command: a refusal that arrives after a write is exactly the
defect the pre-flight exists to close, and only a count can tell the two apart.

Fixtures and helpers are the ones ``test_scenarios`` already builds, imported and
not copied, so the protocol and the typed options cannot drift from that file's.
"""

# ruff: noqa: F811
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from tests.integration.research.test_backtest_evidence import Fixture, seeded  # noqa: F401
from tests.integration.research.test_scenarios import (
    _NOT_ON_THE_ORCHESTRATOR,
    STARTED_AT,
    _counters,
    _protocol,
    _register,
    _scenario_args,
    _scenario_report,
    _scenarios,
)
from tests.integration.research.test_trial_cli import _ledger
from tests.unit.research.backtest.conftest import _contract
from trading_house import cli
from trading_house.research.canonical import canonical_sha256

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("isolated_research_ledger"),
]

runner = CliRunner()

TRIAL_ID = "trial-1"
ATTEMPT_ID = "comp-1"


def _compounding_args(
    seeded: Fixture, protocol_path: Path, *, attempt_id: str = ATTEMPT_ID
) -> list[str]:
    """``scenarios``' options with ``--attempt-prefix`` turned into ``--attempt-id``."""

    argv = _scenario_args(seeded, protocol_path)
    argv[2] = "compounding"
    argv[argv.index("--attempt-prefix")] = "--attempt-id"
    argv[argv.index("grid")] = attempt_id
    return argv


def _compounding(seeded: Fixture, protocol_path: Path, **kwargs: Any) -> Any:
    return runner.invoke(cli.app, _compounding_args(seeded, protocol_path, **kwargs))


def _rows(dsn: str) -> int:
    return len(_ledger(dsn).events())


def _files(root: Path) -> int:
    return len(list(root.rglob("*.json")))


def _edited_protocol_file(seeded: Fixture, tmp_path: Path) -> Path:
    """The registered protocol with one field changed: the seed, one byte of content."""

    protocol = _protocol(seeded)
    edited = protocol.model_copy(
        update={"execution": protocol.execution.model_copy(update={"seed": "changed"})}
    )
    path = tmp_path / "edited.json"
    path.write_text(edited.model_dump_json(), encoding="utf-8")
    return path


def test_the_full_flow_seals_a_rerun_and_the_standalone_reads_agree(
    seeded: Fixture, research_env: Path, tmp_path: Path
) -> None:
    """Register, grid, rerun; the standalone report equals the command's own."""

    protocol_path = _register(tmp_path, seeded)
    _scenarios(seeded, protocol_path)

    result = _compounding(seeded, protocol_path)

    assert result.exit_code == cli.ExitCode.OK, result.stderr
    payload = json.loads(result.stdout)
    assert payload["attempt_id"] == ATTEMPT_ID
    assert payload["report"]["compounding"]["attempt_id"] == ATTEMPT_ID
    assert payload["report"]["constant_notional"]["attempt_id"] == "grid-1"
    assert payload["capacity"]["status"] == "unavailable"
    assert _files(research_env) == 4

    standalone = runner.invoke(
        cli.app, ["research", "trial", "compounding-report", "--trial-id", TRIAL_ID]
    )
    assert standalone.exit_code == cli.ExitCode.OK, standalone.stderr
    assert json.loads(standalone.stdout) == {"status": "ok", **payload["report"]}

    capacity = runner.invoke(cli.app, ["research", "trial", "capacity", "--trial-id", TRIAL_ID])
    assert capacity.exit_code == cli.ExitCode.OK, capacity.stderr
    assert json.loads(capacity.stdout) == {"status": "ok", "capacity": payload["capacity"]}


def test_a_compounding_rerun_is_a_sized_run_not_a_second_constant_one(
    seeded: Fixture, research_env: Path, tmp_path: Path
) -> None:
    """The sealed rerun is marked compounding, so the grid report ignores it and still reports."""

    protocol_path = _register(tmp_path, seeded)
    grid_report = _scenarios(seeded, protocol_path)["report"]
    assert _compounding(seeded, protocol_path).exit_code == cli.ExitCode.OK

    result = _scenario_report()

    assert result.exit_code == cli.ExitCode.OK, result.stderr
    assert json.loads(result.stdout) == {"status": "ok", **grid_report}


def test_rerunning_compounding_with_the_same_arguments_writes_nothing_new(
    seeded: Fixture, research_env: Path, research_ledger_dsn: str, tmp_path: Path
) -> None:
    protocol_path = _register(tmp_path, seeded)
    _scenarios(seeded, protocol_path)
    first = _compounding(seeded, protocol_path)
    assert first.exit_code == cli.ExitCode.OK, first.stderr
    rows, files = _rows(research_ledger_dsn), _files(research_env)

    second = _compounding(seeded, protocol_path)

    assert second.exit_code == cli.ExitCode.OK, second.stderr
    assert json.loads(second.stdout) == json.loads(first.stdout)
    assert (_rows(research_ledger_dsn), _files(research_env)) == (rows, files)


@pytest.mark.parametrize("command", ["scenarios", "compounding"])
def test_a_protocol_file_one_field_off_is_refused_before_any_write(
    seeded: Fixture,
    research_env: Path,
    research_ledger_dsn: str,
    tmp_path: Path,
    command: str,
) -> None:
    """Exit 19, and neither the chain nor the evidence root moved."""

    protocol_path = _register(tmp_path, seeded)
    if command == "compounding":
        _scenarios(seeded, protocol_path)
    edited = _edited_protocol_file(seeded, tmp_path)
    rows, files = _rows(research_ledger_dsn), _files(research_env)
    argv = (
        _scenario_args(seeded, edited)
        if command == "scenarios"
        else _compounding_args(seeded, edited)
    )

    result = runner.invoke(cli.app, argv)

    assert result.exit_code == cli.ExitCode.SCENARIO_EVIDENCE, result.stderr
    assert (_rows(research_ledger_dsn), _files(research_env)) == (rows, files)


def test_the_refusal_of_an_edited_file_names_both_digests(
    seeded: Fixture, research_env: Path, research_ledger_dsn: str, tmp_path: Path
) -> None:
    from trading_house.core.errors import ScenarioEvidenceError
    from trading_house.ops.scenarios import refuse_edited_protocol
    from trading_house.research.canonical import canonical_sha256

    registered = _protocol(seeded)
    edited = registered.model_copy(
        update={"execution": registered.execution.model_copy(update={"seed": "changed"})}
    )
    with pytest.raises(ScenarioEvidenceError) as refusal:
        refuse_edited_protocol(registered, edited)
    cause = str(refusal.value.__cause__)
    assert canonical_sha256(registered) in cause
    assert canonical_sha256(edited) in cause


def test_a_level_sealed_under_another_attempt_is_refused_before_any_write(
    seeded: Fixture, research_env: Path, research_ledger_dsn: str, tmp_path: Path
) -> None:
    protocol_path = _register(tmp_path, seeded)
    _scenarios(seeded, protocol_path)
    rows, files = _rows(research_ledger_dsn), _files(research_env)

    result = runner.invoke(cli.app, _scenario_args(seeded, protocol_path, attempt_prefix="other"))

    assert result.exit_code == cli.ExitCode.SCENARIO_EVIDENCE, result.stderr
    assert (_rows(research_ledger_dsn), _files(research_env)) == (rows, files)


def test_a_rerun_sealed_under_another_attempt_is_refused_before_any_write(
    seeded: Fixture, research_env: Path, research_ledger_dsn: str, tmp_path: Path
) -> None:
    protocol_path = _register(tmp_path, seeded)
    _scenarios(seeded, protocol_path)
    assert _compounding(seeded, protocol_path).exit_code == cli.ExitCode.OK
    rows, files = _rows(research_ledger_dsn), _files(research_env)

    result = _compounding(seeded, protocol_path, attempt_id="comp-2")

    assert result.exit_code == cli.ExitCode.SCENARIO_EVIDENCE, result.stderr
    assert (_rows(research_ledger_dsn), _files(research_env)) == (rows, files)


def test_a_rerun_with_no_sealed_baseline_is_refused_before_any_write(
    seeded: Fixture, research_env: Path, research_ledger_dsn: str, tmp_path: Path
) -> None:
    protocol_path = _register(tmp_path, seeded)
    rows, files = _rows(research_ledger_dsn), _files(research_env)

    result = _compounding(seeded, protocol_path)

    assert result.exit_code == cli.ExitCode.SCENARIO_EVIDENCE, result.stderr
    assert (_rows(research_ledger_dsn), _files(research_env)) == (rows, files)
    assert rows == 1  # the registration and nothing else


def test_the_rerun_is_sealed_as_compounding_sizing(
    seeded: Fixture, research_env: Path, research_ledger_dsn: str, tmp_path: Path
) -> None:
    """The bundle on disk says compounding, so the command did not seal a constant run."""

    from trading_house.ops.scenarios import sealed_bundles
    from trading_house.research.backtest.sizing import SizingMode
    from trading_house.research.evidence import EvidenceStore

    protocol_path = _register(tmp_path, seeded)
    _scenarios(seeded, protocol_path)
    assert _compounding(seeded, protocol_path).exit_code == cli.ExitCode.OK

    sealed = sealed_bundles(
        _ledger(research_ledger_dsn).events_for(TRIAL_ID), EvidenceStore(research_env).read
    )

    sizings = [bundle.sizing for _, bundle in sealed]
    assert sizings.count(SizingMode.COMPOUNDING) == 1
    assert sizings.count(SizingMode.CONSTANT_NOTIONAL) == 3
    assert {b.attempt_id for _, b in sealed if b.sizing is SizingMode.COMPOUNDING} == {ATTEMPT_ID}


def test_capacity_is_refused_for_an_unregistered_trial_as_the_grid_report_is(
    seeded: Fixture, research_env: Path, tmp_path: Path
) -> None:
    _register(tmp_path, seeded)

    grid = _scenario_report("trial-never-declared")
    capacity = runner.invoke(
        cli.app, ["research", "trial", "capacity", "--trial-id", "trial-never-declared"]
    )

    assert capacity.exit_code == grid.exit_code == cli.ExitCode.SCENARIO_EVIDENCE
    assert capacity.stdout == ""


def test_capacity_states_unavailable_for_a_registered_trial(
    seeded: Fixture, research_env: Path, tmp_path: Path
) -> None:
    _register(tmp_path, seeded)

    result = runner.invoke(cli.app, ["research", "trial", "capacity", "--trial-id", TRIAL_ID])

    assert result.exit_code == cli.ExitCode.OK, result.stderr
    assert json.loads(result.stdout)["capacity"]["status"] == "unavailable"


def test_compounding_report_refuses_when_the_rerun_is_absent(
    seeded: Fixture, research_env: Path, tmp_path: Path
) -> None:
    protocol_path = _register(tmp_path, seeded)
    _scenarios(seeded, protocol_path)

    result = runner.invoke(
        cli.app, ["research", "trial", "compounding-report", "--trial-id", TRIAL_ID]
    )

    assert result.exit_code == cli.ExitCode.SCENARIO_EVIDENCE


def test_the_shared_options_are_not_a_second_copy_of_the_protocol(
    seeded: Fixture, tmp_path: Path
) -> None:
    """Every protocol-owned option is still absent from ``compounding``."""

    for option in _NOT_ON_THE_ORCHESTRATOR:
        result = runner.invoke(
            cli.app, [*_compounding_args(seeded, tmp_path / "never-read.json"), option, "1"]
        )
        assert result.exit_code == cli.ExitCode.CONFIGURATION
        assert "No such option" in result.stderr


# --- pre-flight: replay inputs, attempt ids, options (8B3 fix wave) -------------


def _assert_nothing_written(dsn: str, root: Path, before: tuple[int, int]) -> None:
    assert (_rows(dsn), _files(root)) == before


def _contract_with_another_digest(tmp_path: Path) -> str:
    path = tmp_path / "other-contract.json"
    path.write_text(_contract(quantity_max=Decimal("50")).model_dump_json(), encoding="utf-8")
    return str(path)


def _start(seeded: Fixture, attempt_id: str) -> None:
    """An attempt started and never sealed: the row a failed run leaves behind."""

    result = runner.invoke(
        cli.app,
        [
            "research",
            "trial",
            "start",
            "--trial-id",
            TRIAL_ID,
            "--attempt-id",
            attempt_id,
            "--spec-sha256",
            canonical_sha256(_protocol(seeded).candidates[0]),
            "--started-at",
            STARTED_AT,
        ],
    )
    assert result.exit_code == cli.ExitCode.OK, result.stderr


_OTHER_REPLAY = [
    ("--firm-equity", "50000"),
    ("--exit-policy", "fixed_target"),
    ("--atr-period", "3"),
    ("--spread-window", "11"),
    ("--defective-bar-tolerance", "0.5"),
    ("--contract", None),
]


@pytest.mark.parametrize(("option", "value"), _OTHER_REPLAY)
def test_compounding_on_other_replay_inputs_is_refused_before_any_write(
    seeded: Fixture,
    research_env: Path,
    research_ledger_dsn: str,
    tmp_path: Path,
    option: str,
    value: str | None,
) -> None:
    """I-1/I-2: each replay input the protocol does not own is compared with the sealed
    baseline's before the first append, so the rerun cannot seal a bundle the report
    then refuses for good."""

    protocol_path = _register(tmp_path, seeded)
    _scenarios(seeded, protocol_path)
    before = (_rows(research_ledger_dsn), _files(research_env))
    argv = _compounding_args(seeded, protocol_path)
    argv[argv.index(option) + 1] = value or _contract_with_another_digest(tmp_path)

    result = runner.invoke(cli.app, argv)

    assert result.exit_code == cli.ExitCode.SCENARIO_EVIDENCE, result.stderr
    _assert_nothing_written(research_ledger_dsn, research_env, before)


@pytest.mark.parametrize(("option", "value"), _OTHER_REPLAY)
def test_scenarios_on_other_replay_inputs_than_a_sealed_level_are_refused(
    seeded: Fixture,
    research_env: Path,
    research_ledger_dsn: str,
    tmp_path: Path,
    option: str,
    value: str | None,
) -> None:
    """Every level is already sealed here, so without the comparison this command would
    succeed having done nothing; exit 19 is the comparison speaking."""

    protocol_path = _register(tmp_path, seeded)
    _scenarios(seeded, protocol_path)
    before = (_rows(research_ledger_dsn), _files(research_env))
    argv = _scenario_args(seeded, protocol_path)
    argv[argv.index(option) + 1] = value or _contract_with_another_digest(tmp_path)

    result = runner.invoke(cli.app, argv)

    assert result.exit_code == cli.ExitCode.SCENARIO_EVIDENCE, result.stderr
    _assert_nothing_written(research_ledger_dsn, research_env, before)


def test_the_first_scenarios_run_has_nothing_sealed_to_compare_with(
    seeded: Fixture, research_env: Path, tmp_path: Path
) -> None:
    protocol_path = _register(tmp_path, seeded)

    _scenarios(seeded, protocol_path, **{"--firm-equity": "50000"})

    assert _files(research_env) == 3


def test_retrying_compounding_with_another_provenance_option_is_a_true_no_op(
    seeded: Fixture, research_env: Path, research_ledger_dsn: str, tmp_path: Path
) -> None:
    """I-3: a different ``--occurred-at`` derives a different bundle under the same attempt
    id. Re-sealing it would put a second compounding document in the trial and leave the
    report refusing for good; the level is skipped instead."""

    protocol_path = _register(tmp_path, seeded)
    _scenarios(seeded, protocol_path)
    first = _compounding(seeded, protocol_path)
    assert first.exit_code == cli.ExitCode.OK, first.stderr
    before = (_rows(research_ledger_dsn), _files(research_env))
    argv = _compounding_args(seeded, protocol_path)
    argv[argv.index("--occurred-at") + 1] = "2026-03-02T12:00:00"

    second = runner.invoke(cli.app, argv)

    assert second.exit_code == cli.ExitCode.OK, second.stderr
    assert json.loads(second.stdout) == json.loads(first.stdout)
    _assert_nothing_written(research_ledger_dsn, research_env, before)
    report = runner.invoke(
        cli.app, ["research", "trial", "compounding-report", "--trial-id", TRIAL_ID]
    )
    assert report.exit_code == cli.ExitCode.OK, report.stderr


def test_retrying_scenarios_with_another_provenance_option_is_a_true_no_op(
    seeded: Fixture, research_env: Path, research_ledger_dsn: str, tmp_path: Path
) -> None:
    """The same, for a grid sealed before the stressed levels' run ids moved: the bundles
    a changed option would derive are not the sealed ones, and none is written."""

    protocol_path = _register(tmp_path, seeded)
    first = _scenarios(seeded, protocol_path)
    before = (_rows(research_ledger_dsn), _files(research_env))

    second = _scenarios(seeded, protocol_path, **{"--occurred-at": "2026-03-02T12:00:00"})

    assert second["scenarios"] == first["scenarios"]
    assert second["report"] == first["report"]
    _assert_nothing_written(research_ledger_dsn, research_env, before)


@pytest.mark.parametrize("attempt_id", ["grid-1", "grid-1.5"])
def test_an_attempt_id_the_trial_already_started_is_refused_before_any_write(
    seeded: Fixture,
    research_env: Path,
    research_ledger_dsn: str,
    tmp_path: Path,
    attempt_id: str,
) -> None:
    """I-4: a start event is derived from the trial and the attempt id alone, so reusing one
    appends nothing and the audit count would not rise for a run that happened."""

    protocol_path = _register(tmp_path, seeded)
    _scenarios(seeded, protocol_path)
    before = (_rows(research_ledger_dsn), _files(research_env))

    result = _compounding(seeded, protocol_path, attempt_id=attempt_id)

    assert result.exit_code == cli.ExitCode.SCENARIO_EVIDENCE, result.stderr
    _assert_nothing_written(research_ledger_dsn, research_env, before)


def test_an_orphaned_attempt_id_is_refused_by_compounding(
    seeded: Fixture, research_env: Path, research_ledger_dsn: str, tmp_path: Path
) -> None:
    """Started and never sealed: still started, so still refused here."""

    protocol_path = _register(tmp_path, seeded)
    _scenarios(seeded, protocol_path)
    _start(seeded, "comp-orphan")
    before = (_rows(research_ledger_dsn), _files(research_env))

    result = _compounding(seeded, protocol_path, attempt_id="comp-orphan")

    assert result.exit_code == cli.ExitCode.SCENARIO_EVIDENCE, result.stderr
    _assert_nothing_written(research_ledger_dsn, research_env, before)


def test_scenarios_completes_an_orphaned_level_under_its_own_attempt_id(
    seeded: Fixture, research_env: Path, tmp_path: Path
) -> None:
    """The retry path of a run that failed after its start: the same attempt id finishes the
    level, and the audit count is three, not four."""

    protocol_path = _register(tmp_path, seeded)
    _start(seeded, "grid-1.5")

    _scenarios(seeded, protocol_path)

    assert _files(research_env) == 3
    assert _counters()["audit_attempts"] == 3


def test_a_fresh_compounding_run_adds_one_audit_attempt_and_no_lottery_or_specification(
    seeded: Fixture, research_env: Path, tmp_path: Path
) -> None:
    protocol_path = _register(tmp_path, seeded)
    _scenarios(seeded, protocol_path)
    before = _counters()

    assert _compounding(seeded, protocol_path).exit_code == cli.ExitCode.OK
    after = _counters()

    assert after["audit_attempts"] == before["audit_attempts"] + 1
    assert after["selection_lotteries"] == before["selection_lotteries"]
    assert after["effective_specifications"] == before["effective_specifications"]


@pytest.mark.parametrize("command", ["scenarios", "compounding"])
@pytest.mark.parametrize(
    ("option", "value"),
    [("--firm-equity", "abc"), ("--defective-bar-tolerance", "2"), ("--agent-run-id", "")],
)
def test_a_mistyped_option_leaves_no_started_row_behind(
    seeded: Fixture,
    research_env: Path,
    research_ledger_dsn: str,
    tmp_path: Path,
    command: str,
    option: str,
    value: str,
) -> None:
    """M-5, I-1: the request and the provenance values are validated before the first append.

    Only the failures that need the bars (``BacktestRefused``, a ruined account) can still
    leave a start row."""

    protocol_path = _register(tmp_path, seeded)
    if command == "compounding":
        _scenarios(seeded, protocol_path)
        argv = _compounding_args(seeded, protocol_path)
        argv[argv.index(option) + 1] = value
    else:
        argv = _scenario_args(seeded, protocol_path, **{option: value})
    before = (_rows(research_ledger_dsn), _files(research_env))

    result = runner.invoke(cli.app, argv)

    assert result.exit_code == cli.ExitCode.CONFIGURATION, result.stderr
    _assert_nothing_written(research_ledger_dsn, research_env, before)


def test_a_blank_attempt_id_leaves_no_started_row_behind(
    seeded: Fixture, research_env: Path, research_ledger_dsn: str, tmp_path: Path
) -> None:
    """``scenarios`` derives its ids from a prefix and cannot be blank; ``compounding`` can."""

    protocol_path = _register(tmp_path, seeded)
    _scenarios(seeded, protocol_path)
    before = (_rows(research_ledger_dsn), _files(research_env))

    result = _compounding(seeded, protocol_path, attempt_id="")

    assert result.exit_code == cli.ExitCode.CONFIGURATION, result.stderr
    _assert_nothing_written(research_ledger_dsn, research_env, before)


@pytest.mark.parametrize("command", ["scenarios", "compounding"])
def test_a_code_strategy_that_is_not_the_registered_version_is_refused_before_any_write(
    seeded: Fixture,
    research_env: Path,
    research_ledger_dsn: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    """M-4: the registration names version 1; the code now says 2, so every result this run
    sealed would be refused by the report. Refused at 19 with nothing appended."""

    from trading_house.strategies.impl.session_momentum import SessionMomentum

    protocol_path = _register(tmp_path, seeded)
    argv = (
        _scenario_args(seeded, protocol_path)
        if command == "scenarios"
        else _compounding_args(seeded, protocol_path)
    )
    if command == "compounding":
        _scenarios(seeded, protocol_path)
    before = (_rows(research_ledger_dsn), _files(research_env))
    monkeypatch.setattr(SessionMomentum, "version", "2")

    result = runner.invoke(cli.app, argv)

    assert result.exit_code == cli.ExitCode.SCENARIO_EVIDENCE, result.stderr
    _assert_nothing_written(research_ledger_dsn, research_env, before)
