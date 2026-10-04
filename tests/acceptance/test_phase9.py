"""Phase 9 acceptance: a second strategy, registered with a complete spec, scoped
to EURUSD H1 by the registry, replayed end to end through ``backtest run``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests.unit.research.backtest.conftest import FakeBarReader, _breakout_h1, _contract
from trading_house import cli
from trading_house.marketdata.models import Timeframe
from trading_house.strategies.impl.vol_breakout import VOL_BREAKOUT_ID, VOL_BREAKOUT_SPEC
from trading_house.strategies.registry import REGISTERED_STRATEGY_IDS, strategy_scope

runner = CliRunner()


@pytest.fixture
def _dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    """The same fake DSN ``tests/unit/test_cli.py`` uses: ``backtest run`` reads
    settings, never connects. A local fixture because ``tests/conftest.py``
    already has a helper function called ``_dsn``."""

    from tests.unit.test_cli import FAKE_DSN

    monkeypatch.setenv("TRADING_HOUSE_DATABASE_DSN", FAKE_DSN)


def test_the_breakout_is_registered_with_a_complete_spec() -> None:
    assert VOL_BREAKOUT_ID in REGISTERED_STRATEGY_IDS
    assert VOL_BREAKOUT_SPEC.universe == ("fx.eurusd",)
    assert VOL_BREAKOUT_SPEC.trial_count == 3


def test_the_registry_scopes_it_to_eurusd_h1() -> None:
    scope = strategy_scope(VOL_BREAKOUT_ID)
    assert (scope.instrument_id, Timeframe(scope.timeframe)) == ("fx.eurusd", Timeframe.H1)


def _args(tmp_path: Path, arm: str) -> list[str]:
    contract = tmp_path / "contract.json"
    contract.write_text(_contract().model_dump_json(), encoding="utf-8")
    bars = _breakout_h1()
    return [
        "backtest",
        "run",
        "--strategy",
        VOL_BREAKOUT_ID,
        "--exit-policy",
        arm,
        "--start",
        bars[0].event_time.strftime("%Y-%m-%dT%H:%M:%S"),
        "--end",
        bars[-1].event_time.strftime("%Y-%m-%dT%H:%M:%S"),
        "--firm-equity",
        "100000",
        "--contract",
        str(contract),
        "--atr-period",
        "14",
        "--spread-window",
        "10",
        "--commission-per-lot-per-side",
        "0",
        "--slippage-points-per-side",
        "0.4",
        "--swap-long-points-per-day",
        "-7.7",
        "--swap-short-points-per-day",
        "2.0",
        "--triple-swap-weekday",
        "2",
    ]


@pytest.mark.parametrize("arm", ["none", "fixed_target", "chandelier"])
@pytest.mark.usefixtures("_dsn")
def test_every_arm_replays_the_synthetic_breakout_to_exactly_one_long(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arm: str
) -> None:
    monkeypatch.setattr(cli, "_bar_store", lambda: FakeBarReader(_breakout_h1()))

    result = runner.invoke(cli.app, _args(tmp_path, arm))

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)["result"]
    assert payload["timeframe"] == "H1"
    assert payload["strategy_id"] == VOL_BREAKOUT_ID
    assert len(payload["trades"]) == 1, payload["rejections"]
    assert payload["trades"][0]["side"] == "BUY"
