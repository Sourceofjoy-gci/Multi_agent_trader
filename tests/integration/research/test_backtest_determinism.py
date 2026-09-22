"""``backtest run`` must produce the same bytes in two different processes.

Phase 8 hashes this result into a trial ledger beside a Sharpe ratio, and a
Sharpe nobody can reproduce is not evidence.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

import psycopg
import pytest
from pydantic import SecretStr

from tests.conftest import printed_strings
from tests.integration.marketdata.conftest import seed
from tests.unit.research.backtest.conftest import _contract, _ramp
from trading_house.database.connection import open_runtime_connection
from trading_house.marketdata.store import PostgresBarStore

if TYPE_CHECKING:
    from ...conftest import DatabaseHarness

pytestmark = pytest.mark.integration

PROJECT_ROOT = Path(__file__).resolve().parents[3]
BARS = 60
SECRET_IN_THE_DSN = "integration-runtime-password"  # noqa: S105


class Fixture(NamedTuple):
    dsn: str
    contract: Path


@pytest.fixture(scope="module")
def seeded(
    database: DatabaseHarness, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[Fixture]:
    """A real store holding the same 60-bar ramp the engine tests replay.

    Truncated afterwards on purpose: the database is session-scoped and
    ``tests/integration/marketdata`` asserts on what the store holds, so bars
    left behind here would fail tests that have nothing to do with this one.
    """

    store = PostgresBarStore(lambda: open_runtime_connection(SecretStr(database.runtime_dsn)))
    seed(store, _ramp(BARS))
    contract = tmp_path_factory.mktemp("backtest") / "contract.json"
    contract.write_text(_contract().model_dump_json(), encoding="utf-8")
    try:
        yield Fixture(dsn=database.runtime_dsn, contract=contract)
    finally:
        with (
            psycopg.connect(database.test_superuser_dsn, autocommit=True) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute("TRUNCATE marketdata.bars, marketdata.ingest_runs")


def _run_cli(seeded: Fixture, *, hash_seed: str) -> str:
    """One ``backtest run`` in its own interpreter, under its own hash seed.

    ``cwd`` is the project root because ``RuntimeSettings`` defaults the three
    constitution paths to relative ones; the DSN is passed through the
    environment, as it is in production, so it never appears in a command line
    a process listing could show.
    """

    bars = _ramp(BARS)
    completed = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "trading_house.cli",
            "backtest",
            "run",
            "--strategy",
            "toy",
            "--toy-every-n",
            "20",
            "--instrument",
            "fx.eurusd",
            "--timeframe",
            "M1",
            "--start",
            bars[0].event_time.strftime("%Y-%m-%dT%H:%M:%S"),
            "--end",
            bars[-1].event_time.strftime("%Y-%m-%dT%H:%M:%S"),
            "--firm-equity",
            "100000",
            "--contract",
            str(seeded.contract),
            "--atr-period",
            "2",
            "--spread-window",
            "10",
            "--commission-per-lot-per-side",
            "3.50",
            "--slippage-points-per-side",
            "0",
            "--swap-long-points-per-day",
            "-0.80",
            "--swap-short-points-per-day",
            "0.30",
            "--triple-swap-weekday",
            "2",
        ],
        env={
            **os.environ,
            "PYTHONHASHSEED": hash_seed,
            "TRADING_HOUSE_DATABASE_DSN": seeded.dsn,
        },
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout


def test_two_runs_under_different_hash_seeds_agree_byte_for_byte(seeded: Fixture) -> None:
    """Phase 8 hashes this result into a trial, and a Sharpe nobody can
    reproduce is not evidence.

    Two SUBPROCESSES under different PYTHONHASHSEED, not two calls in one
    process. Calling a function twice in one interpreter proves almost nothing
    -- this project shipped exactly that hollow fixture in Phase 2 -- and set or
    dict ordering reaching the output is the realistic leak that only a
    different seed exposes.

    What this establishes: nothing hash-randomised reaches the bytes. Set
    iteration order, dict ordering from an unordered source, an id derived from
    a memory address -- all of those differ between these two processes and
    none of them reaches the output.

    What it does NOT establish, deliberately: that the digest is stable across
    a CHANGE. ``digest()`` hashes a ``Decimal``'s string form while the
    reconciliation validators compare by value, so ``Decimal("93")`` and
    ``Decimal("93.00")`` both validate and hash differently. Both runs here
    construct their figures identically, so a scale difference introduced by an
    unrelated edit later would move the digest and nothing in this file would
    notice. That question is open; this test does not close it.

    The trades assertion is not decoration. Two byte-identical results with no
    trades in them would satisfy every other assertion here while proving
    nothing about the replay -- and a backtest that silently produces no trades
    is precisely the failure this phase is most exposed to.
    """

    first = _run_cli(seeded, hash_seed="0")
    second = _run_cli(seeded, hash_seed="12345")

    assert first == second
    payload = json.loads(first)
    assert payload["digest"]
    assert payload["result"]["trades"]
    assert payload["result"]["bars_seen"] == BARS
    assert payload["result"]["rejections"] == []


def test_the_emitted_result_carries_no_credential_or_path(seeded: Fixture) -> None:
    """A ``BacktestResult`` has just become CLI output.

    No command in this system prints a path, a credential or a key, and this
    one is handed both: a DSN through the environment and a file path through
    ``--contract``. Walking the result's fields says none of them can carry
    one; this says the rendering agrees.

    Asserted against the decoded payload, not the raw stream. A path that
    leaks into the JSON arrives escaped -- doubled by ``json.dumps`` on the
    wire and doubled again by the ``str()`` that renders a nested object --
    so on Windows ``str(PROJECT_ROOT) not in stdout`` could not fail under
    any production change, and this file is the phase's reproducibility
    proof. ``printed_strings`` is the same helper ``tests/unit/test_cli.py``
    uses, shared rather than copied so the two cannot drift.

    The credential assertion needs none of that -- there are no backslashes in
    it -- and is kept on the decoded text anyway, because a helper that
    silently stopped decoding would then be caught by the two beside it.
    """

    printed = printed_strings(_run_cli(seeded, hash_seed="0"))

    assert SECRET_IN_THE_DSN not in printed
    assert str(seeded.contract) not in printed
    assert str(PROJECT_ROOT) not in printed
