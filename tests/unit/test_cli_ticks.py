from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from tests.unit.marketdata.tick_fakes import FakeTickProvider, InMemoryTickDayStore, weekdays_only
from tests.unit.test_cli import FAKE_DSN
from trading_house import cli
from trading_house.core.clock import FixedClock

runner = CliRunner()
NOW = datetime(2026, 10, 7, 1, tzinfo=UTC)


class _FakeAdapter(FakeTickProvider):
    def describe_instrument(self, instrument_id: str) -> Any:
        return SimpleNamespace(point_size=Decimal("0.00001"))


@pytest.fixture
def wired(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.setenv("TRADING_HOUSE_DATABASE_DSN", FAKE_DSN)
    monkeypatch.setenv("TRADING_HOUSE_TICK_ROOT", str(tmp_path))
    adapter = _FakeAdapter(weekdays_only)
    store = InMemoryTickDayStore()
    timeouts: list[float] = []

    @contextmanager
    def provider(*_args: Any, request_timeout_seconds: float = 10.0) -> Iterator[Any]:
        timeouts.append(request_timeout_seconds)
        yield adapter, None, 0

    monkeypatch.setattr(cli, "_history_provider", provider)
    monkeypatch.setattr(cli, "_tick_store", lambda: store)
    monkeypatch.setattr(cli, "SystemClock", lambda: FixedClock(NOW))
    return {"adapter": adapter, "store": store, "timeouts": timeouts, "root": tmp_path}


def test_backfill_records_days_and_uses_the_long_timeout(wired: dict[str, Any]) -> None:
    result = runner.invoke(
        cli.app, ["data", "ticks", "backfill", "--instrument", "fx.eurusd", "--from", "2026-10-05"]
    )
    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["days_complete"] == 2  # Mon 5 and Tue 6
    assert wired["timeouts"] == [120.0]


def test_update_after_backfill_fetches_nothing_new_on_the_same_day(wired: dict[str, Any]) -> None:
    runner.invoke(
        cli.app, ["data", "ticks", "backfill", "--instrument", "fx.eurusd", "--from", "2026-10-05"]
    )
    result = runner.invoke(cli.app, ["data", "ticks", "update", "--instrument", "fx.eurusd"])
    assert result.exit_code == 0, result.stderr
    assert json.loads(result.stdout)["days_complete"] == 0


def test_update_before_backfill_is_a_configuration_error(wired: dict[str, Any]) -> None:
    result = runner.invoke(cli.app, ["data", "ticks", "update", "--instrument", "fx.eurusd"])
    assert result.exit_code == cli.ExitCode.CONFIGURATION


def test_tick_digest_prints_a_window_digest(wired: dict[str, Any]) -> None:
    runner.invoke(
        cli.app, ["data", "ticks", "backfill", "--instrument", "fx.eurusd", "--from", "2026-10-05"]
    )
    result = runner.invoke(
        cli.app,
        [
            "research",
            "dataset",
            "tick-digest",
            "--instrument",
            "fx.eurusd",
            "--start",
            "2026-10-05T00:00:00",
            "--end",
            "2026-10-07T00:00:00",
        ],
    )
    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["days"] == 2
    assert payload["ticks"] == 96
    assert len(payload["sha256"]) == 64
