"""Phase 12 acceptance: a declared capacity model measured end to end, the unseen-holdout
rule over a real chain, and a prospective holdout locked before its data and opened after.

Built on the 8D2 opening fixture and helpers, so the protocol, the store and the commands are
the ones every earlier phase's acceptance already exercises.
"""

# ruff: noqa: F811
# The shared fixtures are imported by name; pyflakes reads each test parameter as a
# redefinition of the import.

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
import pytest
from pydantic import SecretStr

from tests.acceptance.test_phase8b2b import _envelope
from tests.acceptance.test_phase8d2 import (
    BARS_PER_DAY,
    RESEARCH_DAYS,
    _decide,
    _holdout,
    _locked_protocol,
    _open,
    _register,
    _synthetic,
    _trial,
    opening_seeded,  # noqa: F401
)
from tests.conftest import DatabaseHarness
from tests.dataset_digest import declared_digest
from tests.integration.marketdata.conftest import seed
from tests.integration.research.conftest import research_env  # noqa: F401
from tests.integration.research.test_backtest_evidence import Fixture, _ramp
from tests.integration.research.test_compounding import _compounding
from tests.integration.research.test_scenarios import TRIAL_ID, _scenarios
from tests.integration.research.test_trial_cli import _ledger, _store
from tests.unit.research.backtest.conftest import _contract
from trading_house import cli
from trading_house.database.connection import open_runtime_connection
from trading_house.marketdata.store import PostgresBarStore
from trading_house.ops.scenarios import sealed_bundles
from trading_house.research.promotion import Decision
from trading_house.research.trial_ledger import (
    CapacitySpec,
    HoldoutCollection,
    HoldoutSpec,
    HoldoutState,
    TrialProtocol,
)

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("isolated_research_ledger")]

BAR_DAYS = 45


def _capacity(**overrides: object) -> CapacitySpec:
    fields: dict[str, object] = {
        "model": "tick_volume_participation_v1",
        "lots_per_tick": Decimal(1),
        "target_equity": Decimal(100000),
        "max_participation": Decimal(1),
        "impact_points_at_full_participation": Decimal(0),
        "max_impact_fraction_of_edge": Decimal(1),
    }
    fields.update(overrides)
    return CapacitySpec(**fields)  # type: ignore[arg-type]


def _gate(payload: dict[str, Any], number: int) -> dict[str, Any]:
    return next(gate for gate in payload["gates"] if gate["gate"] == number)


# --- 1. capacity -------------------------------------------------------------------------


def test_a_declared_capacity_model_is_sealed_with_the_run_and_measured_by_gate_nine(
    opening_seeded: Fixture, research_env: Path, research_ledger_dsn: str, tmp_path: Path
) -> None:
    """Every ramp bar reports 100 ticks; at one lot per tick that is a 100-lot market. The
    runs trade a few lots, so participation is a few percent: inside a limit of 1, outside a
    limit of 0.001, and gate 9 says which."""

    protocol = _locked_protocol(opening_seeded).model_copy(update={"capacity": _capacity()})
    path = _register(tmp_path, protocol)
    _scenarios(opening_seeded, path)

    sealed = sealed_bundles(
        _ledger(research_ledger_dsn).events_for(TRIAL_ID), _store(research_env).read
    )
    assert sealed
    for _, bundle in sealed:
        assert bundle.liquidity is not None
        assert [t.proposal_id for t in bundle.liquidity.trades] == [
            t.proposal_id for t in bundle.result.trades
        ]
        assert {t.entry_tick_volume for t in bundle.liquidity.trades} == {100}

    capacity = _envelope(_trial("capacity", "--trial-id", TRIAL_ID))["capacity"]
    assert capacity["status"] == "measured"
    assert capacity["zero_volume_fills"] == 0
    assert Decimal(capacity["max_participation"]) < 1

    decided = _envelope(_decide())
    gate = _gate(decided, 9)
    edge_positive = Decimal(capacity["net_edge"]) > 0
    assert gate["status"] == ("PASS" if edge_positive else "FAIL"), gate["reason"]


def test_a_participation_limit_the_runs_exceed_fails_gate_nine(
    opening_seeded: Fixture, research_env: Path, tmp_path: Path
) -> None:
    protocol = _locked_protocol(opening_seeded).model_copy(
        update={"capacity": _capacity(max_participation=Decimal("0.001"))}
    )
    path = _register(tmp_path, protocol)
    _scenarios(opening_seeded, path)

    gate = _gate(_envelope(_decide()), 9)

    assert gate["status"] == "FAIL"
    assert "exceeds the declared 0.001" in gate["reason"]


def test_without_a_declared_model_gate_nine_stays_unavailable(
    opening_seeded: Fixture, research_env: Path, tmp_path: Path
) -> None:
    path = _register(tmp_path, _locked_protocol(opening_seeded))
    _scenarios(opening_seeded, path)

    gate = _gate(_envelope(_decide()), 9)

    assert gate["status"] == "UNAVAILABLE"
    assert "declares no volume-to-lots model" in gate["reason"]


# --- 2. unseen ---------------------------------------------------------------------------


def test_holdout_check_names_a_run_that_already_read_the_window(
    opening_seeded: Fixture, research_env: Path, tmp_path: Path
) -> None:
    """Trial 1's research runs read its research window. A second protocol that would lock
    that same span as its holdout is told so before it registers, and once registered its
    holdout derives as contaminated."""

    first = _locked_protocol(opening_seeded)
    _scenarios(opening_seeded, _register(tmp_path, first))
    research = first.data
    second = _second_protocol(
        first,
        holdout=HoldoutSpec(
            state=HoldoutState.LOCKED,
            start=research.start,
            end=research.end,
            dataset_sha256=research.dataset_sha256,
        ),
        data_start=research.start - timedelta(days=30),
        data_end=research.start - timedelta(minutes=15),
    )
    path = tmp_path / "second.json"
    path.write_text(second.model_dump_json(), encoding="utf-8")

    check = _envelope(_trial("holdout-check", "--protocol", str(path)))

    assert check["unseen"] is False
    assert any(f"trial {TRIAL_ID}'s sealed run read" in reason for reason in check["reasons"])
    _register(tmp_path, second, name="second-registered.json")
    assert _holdout(SECOND_TRIAL)[0] == "contaminated"


def test_holdout_check_passes_a_window_nothing_has_read(
    opening_seeded: Fixture, research_env: Path, tmp_path: Path
) -> None:
    path = tmp_path / "p.json"
    path.write_text(_locked_protocol(opening_seeded).model_dump_json(), encoding="utf-8")

    assert _envelope(_trial("holdout-check", "--protocol", str(path))) == {
        "unseen": True,
        "reasons": [],
    }


SECOND_TRIAL = "trial-second"
"""Not ``trial-2``: the 8D2 protocol already declares that id alongside ``trial-1``."""


def _second_protocol(
    first: TrialProtocol, *, holdout: HoldoutSpec, data_start: datetime, data_end: datetime
) -> TrialProtocol:
    candidate = first.candidates[0].model_copy(
        update={"trial_id": SECOND_TRIAL, "spec_id": "spec-second"}
    )
    return first.model_copy(
        update={
            "protocol_id": "protocol-second",
            "candidates": (candidate,),
            "holdout": holdout,
            "data": first.data.model_copy(update={"start": data_start, "end": data_end}),
        }
    )


# --- 3. prospective ------------------------------------------------------------------------

FUTURE = timedelta(days=3653)
"""Ten years on: far enough past any clock this suite runs under that registering now is
always before the holdout's first bar."""


@pytest.fixture(scope="module")
def future_seeded(
    database: DatabaseHarness, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[Fixture]:
    """The 8D2 ramp moved ten years forward, so a holdout over its last ten days starts after
    any registration this suite can make."""

    bars = tuple(
        bar.model_copy(
            update={
                "event_time": bar.event_time + FUTURE,
                "availability_time": bar.availability_time + FUTURE,
            }
        )
        for bar in _ramp(BAR_DAYS)
    )
    store = PostgresBarStore(lambda: open_runtime_connection(SecretStr(database.runtime_dsn)))
    seed(store, bars)
    contract = tmp_path_factory.mktemp("prospective") / "contract.json"
    contract.write_text(_contract().model_dump_json(), encoding="utf-8")
    try:
        yield Fixture(
            contract=contract,
            first_bar=bars[0].event_time,
            last_bar=bars[-1].event_time,
            mid_session_bar=bars[33].event_time,
            bars=bars,
        )
    finally:
        with (
            psycopg.connect(database.test_superuser_dsn, autocommit=True) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute("TRUNCATE marketdata.bars, marketdata.ingest_runs")


def _prospective(seeded: Fixture) -> TrialProtocol:
    research = seeded.bars[: RESEARCH_DAYS * BARS_PER_DAY]
    holdout = seeded.bars[RESEARCH_DAYS * BARS_PER_DAY :]
    base = _locked_protocol(seeded)
    return base.model_copy(
        update={
            "data": base.data.model_copy(
                update={
                    "start": research[0].event_time,
                    "end": research[-1].event_time,
                    "dataset_sha256": declared_digest(research),
                }
            ),
            "holdout": HoldoutSpec(
                state=HoldoutState.LOCKED,
                start=holdout[0].event_time,
                end=holdout[-1].event_time,
                collection=HoldoutCollection.PROSPECTIVE,
            ),
        }
    )


def test_a_prospective_holdout_is_locked_without_a_hash_and_opened_with_a_computed_one(
    future_seeded: Fixture, research_env: Path, research_ledger_dsn: str, tmp_path: Path
) -> None:
    protocol = _prospective(future_seeded)
    path = _register(tmp_path, protocol)
    assert _holdout() == ("locked", 0)
    _scenarios(future_seeded, path)
    assert _compounding(future_seeded, path).exit_code == cli.ExitCode.OK
    _synthetic(research_ledger_dsn, research_env, Decision.RESEARCH_PASSED)

    result = _open(future_seeded, path)

    assert result.exit_code == cli.ExitCode.OK, result.stderr
    store = _store(research_env)
    computed = declared_digest(future_seeded.bars[RESEARCH_DAYS * BARS_PER_DAY :])
    for entry in _envelope(result)["bundles"]:
        bundle = store.read(entry["evidence_sha256"])
        assert bundle.provenance.dataset_sha256 == computed
    decided = _envelope(_decide())
    assert _gate(decided, 2)["status"] in {"PASS", "FAIL"}
    assert _holdout() == ("consumed", 3)


def test_a_prospective_holdout_whose_window_already_began_is_contaminated(
    opening_seeded: Fixture, research_env: Path, tmp_path: Path
) -> None:
    """The 8D2 ramp starts on 2026-09-21: its holdout window began before any registration
    this suite makes, so locking it as prospective is a lock after the data existed."""

    locked = _locked_protocol(opening_seeded)
    protocol = locked.model_copy(
        update={
            "holdout": HoldoutSpec(
                state=HoldoutState.LOCKED,
                start=datetime(2026, 1, 1, tzinfo=UTC),
                end=opening_seeded.last_bar,
                collection=HoldoutCollection.PROSPECTIVE,
            ),
            "data": locked.data.model_copy(
                update={
                    "start": datetime(2025, 1, 1, tzinfo=UTC),
                    "end": datetime(2025, 6, 1, tzinfo=UTC),
                }
            ),
        }
    )
    _register(tmp_path, protocol)

    state, _ = _holdout()

    assert state == "contaminated"
