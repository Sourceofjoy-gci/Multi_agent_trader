"""Phase 8A.1 acceptance: the ledger vouches for a start's specification digest.

Through the real CLI against PostgreSQL, because the claim is about the boundary
an operator meets rather than about one function:

1. a start carrying its trial's declared digest is appended and counted;
2. a start carrying any other digest -- a real one, the protocol's own, the other
   candidate's -- is refused with the ledger's existing exit code and writes nothing;
3. a trial declared only by a legacy import is not checked;
4. a retry of an appended start still succeeds;
5. the README says exactly what is now true, no more.
"""

# ruff: noqa: F811
from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests.integration.research.conftest import research_env  # noqa: F401
from tests.integration.research.test_trial_cli import (
    _import_legacy,
    _ledger,
    _protocol,
    _register,
    _result,
    _start,
    _verify,
    _write_phase7_artifact,
    declared_spec_sha256,
)
from trading_house import cli
from trading_house.research.canonical import canonical_sha256
from trading_house.research.trial_ledger import LedgerEventType

runner = CliRunner()

README = (Path(__file__).resolve().parents[2] / "README.md").read_text(encoding="utf-8")


def _count() -> dict[str, int]:
    result = runner.invoke(cli.app, ["research", "trial", "count"])
    assert result.exit_code == cli.ExitCode.OK, result.stderr
    return json.loads(result.stdout)


@pytest.mark.integration
@pytest.mark.usefixtures("isolated_research_ledger")
def test_a_declared_digest_is_appended_and_counted(tmp_path: Path, research_env: Path) -> None:
    _register(tmp_path)

    started = _start(spec_sha256=declared_spec_sha256("trial-1"))

    assert started.exit_code == cli.ExitCode.OK, started.stderr
    assert _count()["effective_specifications"] == 1
    assert _verify().exit_code == cli.ExitCode.OK


@pytest.mark.integration
@pytest.mark.usefixtures("isolated_research_ledger")
def test_any_other_digest_is_refused_with_the_ledgers_exit_code_and_writes_nothing(
    tmp_path: Path, research_env: Path, research_ledger_dsn: str
) -> None:
    _register(tmp_path)
    before = _ledger(research_ledger_dsn).events()

    for digest in (canonical_sha256(_protocol()), declared_spec_sha256("trial-2"), "f" * 64):
        refused = _start(spec_sha256=digest)
        assert refused.exit_code == cli.ExitCode.TRIAL_LEDGER_APPEND, refused.stderr
        assert digest not in refused.stderr
        assert refused.stdout == ""

    assert _ledger(research_ledger_dsn).events() == before
    assert _count() == {
        "status": "ok",
        "audit_attempts": 0,
        "selection_lotteries": 0,
        "effective_specifications": 0,
    }


@pytest.mark.integration
@pytest.mark.usefixtures("isolated_research_ledger")
def test_a_legacy_only_trial_is_not_checked(
    tmp_path: Path, research_env: Path, research_ledger_dsn: str
) -> None:
    imported = _import_legacy(_write_phase7_artifact(tmp_path / "phase7.json", _result()))

    started = _start(trial_id=imported["trial_id"], spec_sha256="f" * 64)

    assert started.exit_code == cli.ExitCode.OK, started.stderr
    assert [record.event_type for record in _ledger(research_ledger_dsn).events()] == [
        LedgerEventType.LEGACY_IMPORTED,
        LedgerEventType.EXECUTION_STARTED,
    ]


@pytest.mark.integration
@pytest.mark.usefixtures("isolated_research_ledger")
def test_a_retried_start_still_succeeds(tmp_path: Path, research_env: Path) -> None:
    _register(tmp_path)
    clock = "2026-03-01T12:00:00"

    first = _start(started_at=clock)
    second = _start(started_at=clock)

    assert first.exit_code == second.exit_code == cli.ExitCode.OK
    assert _count()["audit_attempts"] == 1


def _flat(text: str) -> str:
    return " ".join(text.split())


def _section(heading: str) -> str:
    start = README.index(heading)
    end = README.find("\n## ", start + 1)
    return README[start : end if end != -1 else len(README)]


def test_the_readme_states_what_8a1_vouches_for_and_what_it_does_not() -> None:
    """Corrected sentences asserted present, so restoring an overclaim fails here."""

    text, section = _flat(README), _flat(_section("## Phase 8A.1"))

    assert (
        "**`effective_specifications` is vouched for at append time for one event type only.**"
        in text
    )
    assert "refuses an `EXECUTION_STARTED` of a preregistered trial" in text
    assert "does **not** vouch for `LEGACY_IMPORTED` events" in text
    assert "digests remain client-asserted" in text
    assert "past drift is history, counted as written" in text
    assert "accepts either declared digest, and refuses a third" in section
    assert "A trial declared only by `LEGACY_IMPORTED` has no registration" in section
    assert "No migration and no new event type were added." in section
    assert "`replay`, `verify` and `count` read what is in the chain" in section
    # The two superseded claims are gone, and what replaced them is present above.
    assert "is a count of supplied digests, not a verified match" not in text
    assert "`spec_sha256` is still unvouched" not in text
