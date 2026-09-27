"""Shared environment for the research integration tests.

``research_env`` lives here rather than in either test module because two modules
now need it and a fixture cannot be defined twice. The same reason
``tests/integration/marketdata/conftest.py`` exists, and pytest discovers a
conftest by directory under the tree's ``--import-mode=importlib``, where a
sibling test module's fixture is only reachable by an absolute import that the
linter reads as an unused name.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tests.conftest import DatabaseHarness


@pytest.fixture
def research_env(
    monkeypatch: pytest.MonkeyPatch,
    research_ledger_dsn: str,
    research_evidence_root: Path,
    database: DatabaseHarness,
) -> Path:
    """The two DSNs and an emptied evidence root, read from the environment like production.

    The two DSNs name two *different* databases, which is the whole point of the
    phase: a test that pointed both at the research database would pass with the
    two-database boundary switched off, because a trial command cannot tell the
    difference once the connection is open. Only the composition boundary can, so
    only the composition boundary is exercised this way.

    The root is cleared here rather than merely pointed at, because half of these
    tests' assertions are an exact count of the files under it -- "one import, one
    file" and "a refused record wrote nothing" are only statements if the root
    started empty. ``research_evidence_root`` is already per-test, so this makes
    the isolation a property of the assertion rather than an accident of the
    fixture's scope.
    """

    shutil.rmtree(research_evidence_root, ignore_errors=True)
    research_evidence_root.mkdir(parents=True)
    monkeypatch.setenv("TRADING_HOUSE_DATABASE_DSN", database.runtime_dsn)
    monkeypatch.setenv("TRADING_HOUSE_RESEARCH_LEDGER_DSN", research_ledger_dsn)
    monkeypatch.setenv("TRADING_HOUSE_EVIDENCE_ROOT", str(research_evidence_root))
    return research_evidence_root
