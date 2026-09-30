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
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from tests.integration.research.test_backtest_evidence import Fixture, seeded  # noqa: F401
from tests.integration.research.test_scenarios import (
    _NOT_ON_THE_ORCHESTRATOR,
    _protocol,
    _register,
    _scenario_args,
    _scenario_report,
    _scenarios,
)
from tests.integration.research.test_trial_cli import _ledger
from trading_house import cli

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
