"""Environment settings must fail closed and never carry risk limits."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from trading_house.settings import RuntimeSettings

SECRET_DSN = "postgresql://runtime:super-secret-password@localhost/trading_house"  # noqa: S105
# The same host, a different database: a research DSN naming the database the
# application already uses is what ops.ledger.research_ledger_dsn refuses, so one
# DSN for both would configure a pair no deployment may run.
RESEARCH_DSN = "postgresql://runtime:super-secret-password@localhost/trading_house_research"

RISK_BEARING_NAMES = frozenset(
    {
        "risk_per_trade_pct",
        "daily_loss_stop_pct",
        "max_drawdown_halt_pct",
        "max_gross_leverage",
        "capital_fraction",
        "max_concurrent_positions",
        "max_orders_per_minute",
        "k_sigma",
        "k_spread",
    }
)


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Settings tests must not observe the developer's own environment."""

    for suffix in (
        "DATABASE_DSN",
        "RESEARCH_LEDGER_DSN",
        "EVIDENCE_ROOT",
        "CONSTITUTION_PATH",
        "CONSTITUTION_SIGNATURE_PATH",
        "CONSTITUTION_PUBLIC_KEY_PATH",
    ):
        monkeypatch.delenv(f"TRADING_HOUSE_{suffix}", raising=False)


def test_dsn_is_read_from_the_prefixed_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADING_HOUSE_DATABASE_DSN", SECRET_DSN)

    settings = RuntimeSettings()

    assert isinstance(settings.database_dsn, SecretStr)
    assert settings.database_dsn.get_secret_value() == SECRET_DSN


def test_dsn_is_redacted_in_rendered_output(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADING_HOUSE_DATABASE_DSN", SECRET_DSN)

    settings = RuntimeSettings()

    rendered = f"{settings!r} {settings} {settings.database_dsn!r}"
    assert "super-secret-password" not in rendered


def test_missing_dsn_fails_closed() -> None:
    with pytest.raises(ValidationError):
        RuntimeSettings()


def test_constitution_paths_default_to_checked_in_public_artifacts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRADING_HOUSE_DATABASE_DSN", SECRET_DSN)

    settings = RuntimeSettings()

    assert settings.constitution_path == Path("config/risk_constitution.yaml")
    assert settings.constitution_signature_path == Path("config/risk_constitution.yaml.sig")
    assert settings.constitution_public_key_path == Path("config/risk_constitution.public.pem")


def test_constitution_paths_are_overridable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADING_HOUSE_DATABASE_DSN", SECRET_DSN)
    monkeypatch.setenv("TRADING_HOUSE_CONSTITUTION_PATH", "other/limits.yaml")

    settings = RuntimeSettings()

    assert settings.constitution_path == Path("other/limits.yaml")


def test_unrelated_environment_variables_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADING_HOUSE_DATABASE_DSN", SECRET_DSN)
    monkeypatch.setenv("TRADING_HOUSE_NOT_A_SETTING", "surprise")

    assert RuntimeSettings().database_dsn.get_secret_value() == SECRET_DSN


def test_settings_never_auto_load_a_dotenv_file() -> None:
    """A checked-in .env must never become a source of runtime configuration."""

    assert RuntimeSettings.model_config.get("env_file") is None


def test_settings_use_the_project_prefix() -> None:
    assert RuntimeSettings.model_config.get("env_prefix") == "TRADING_HOUSE_"


def test_settings_carry_no_risk_limits() -> None:
    """Risk limits come from the signed constitution, never the environment."""

    assert RISK_BEARING_NAMES.isdisjoint(RuntimeSettings.model_fields)


def test_settings_are_frozen(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADING_HOUSE_DATABASE_DSN", SECRET_DSN)

    settings = RuntimeSettings()

    with pytest.raises(ValidationError):
        settings.database_dsn = SecretStr("replaced")


def test_research_dsn_is_optional_for_non_trial_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only a research-ledger operation may demand a second database.

    Required here, every command that is not a trial -- `order submit`,
    `audit verify`, `guard run` -- would fail to start without one.
    """

    monkeypatch.setenv("TRADING_HOUSE_DATABASE_DSN", SECRET_DSN)

    settings = RuntimeSettings()

    assert settings.research_ledger_dsn is None
    assert settings.evidence_root == Path(".local/evidence")


def test_research_dsn_and_evidence_root_are_read_from_the_prefixed_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRADING_HOUSE_DATABASE_DSN", SECRET_DSN)
    monkeypatch.setenv("TRADING_HOUSE_RESEARCH_LEDGER_DSN", RESEARCH_DSN)
    monkeypatch.setenv("TRADING_HOUSE_EVIDENCE_ROOT", "C:/evidence")

    settings = RuntimeSettings()

    assert settings.research_ledger_dsn is not None
    assert settings.research_ledger_dsn.get_secret_value() == RESEARCH_DSN
    assert settings.evidence_root == Path("C:/evidence")


def test_the_research_dsn_is_never_rendered_in_clear(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRADING_HOUSE_DATABASE_DSN", SECRET_DSN)
    monkeypatch.setenv("TRADING_HOUSE_RESEARCH_LEDGER_DSN", RESEARCH_DSN)

    settings = RuntimeSettings()

    assert (
        "super-secret-password" not in f"{settings!r} {settings} {settings.research_ledger_dsn!r}"
    )
