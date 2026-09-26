"""The trial ledger's composition shim, without a database.

Three things can go wrong here and none of them needs PostgreSQL to detect: a
missing research DSN turning into a confusing error much later, a DSN handed to
the driver in the wrong shape, and an evidence root that is not the configured
one. The last of those is the easiest to get wrong by accident --
``EvidenceStore`` resolves its root, so a test comparing against a resolved path
is the only kind that would notice.
"""

from __future__ import annotations

import traceback
from pathlib import Path

import psycopg
import pytest
from pydantic import SecretStr

import trading_house.ops.ledger as ledger_ops
from trading_house.core.errors import ConfigurationError, TrialLedgerAppendError
from trading_house.database.connection import open_runtime_connection
from trading_house.research.ledger_store import PostgresTrialLedger
from trading_house.settings import RuntimeSettings

SECRET_DSN = "postgresql://runtime:super-secret-password@localhost/trading_house_research"  # noqa: S105
# Malformed rather than merely unreachable: psycopg rejects it while parsing the
# conninfo, so the test costs no network round trip and no container.
UNROUTABLE_DSN = "postgresql://runtime:unroutable-secret@[/malformed"


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for suffix in ("DATABASE_DSN", "RESEARCH_LEDGER_DSN", "EVIDENCE_ROOT"):
        monkeypatch.delenv(f"TRADING_HOUSE_{suffix}", raising=False)


def _settings(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> RuntimeSettings:
    """Settings built from arguments, not from the process environment.

    ``RuntimeSettings`` reads the environment, so the prefix is written out here
    the same way ``tests/unit/test_settings.py`` writes it, and the fixture owns
    the undo.
    """

    monkeypatch.setenv("TRADING_HOUSE_DATABASE_DSN", SECRET_DSN)
    for key, value in overrides.items():
        monkeypatch.setenv(f"TRADING_HOUSE_{key}", value)
    return RuntimeSettings()


def test_a_missing_research_dsn_is_a_configuration_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The boundary that cannot work without a second database says so, and only there.

    Unrelated commands start fine, which is why the setting is optional at all;
    the refusal belongs to the research-ledger operation rather than to settings
    that every other command also reads.
    """

    settings = _settings(monkeypatch)

    with pytest.raises(ConfigurationError) as raised:
        ledger_ops.research_ledger_dsn(settings)

    assert str(raised.value) == "configuration invalid"


def test_the_configured_secret_is_what_a_connection_factory_is_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What the CLI composes is ``lambda: open_runtime_connection(dsn(settings))``.

    ``open_runtime_connection`` takes a ``SecretStr`` and calls
    ``get_secret_value()`` on it, so this asserts the shim hands back the very
    object the driver needs -- not a plain string, and not a second copy that
    could drift from the one settings holds.
    """

    settings = _settings(monkeypatch, RESEARCH_LEDGER_DSN=SECRET_DSN)

    dsn = ledger_ops.research_ledger_dsn(settings)

    assert isinstance(dsn, SecretStr)
    assert dsn is settings.research_ledger_dsn
    assert dsn.get_secret_value() == SECRET_DSN


def test_the_dsn_reaches_the_driver_and_stays_out_of_the_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole composition, as far as it can be exercised without a database.

    A ``SecretStr`` the connection layer would reject is the failure this
    catches, and the driver rejects it before anything is sent. The store then
    reports its own one error for a read it could not perform -- which is the
    repository's pattern, and is why this asserts the redaction rather than the
    driver's exception type: a caller catches ``TrialLedgerAppendError`` and
    nothing else.
    """

    settings = _settings(monkeypatch, RESEARCH_LEDGER_DSN=UNROUTABLE_DSN)
    ledger = PostgresTrialLedger(
        lambda: open_runtime_connection(ledger_ops.research_ledger_dsn(settings))
    )

    with pytest.raises(TrialLedgerAppendError) as raised:
        ledger.events()

    error = raised.value
    assert error.args == ("trial ledger append failed",)
    assert not isinstance(error.__cause__, psycopg.Error)
    assert error.__context__ is None
    rendered = "".join(traceback.format_exception(error))
    assert UNROUTABLE_DSN not in rendered
    assert "unroutable-secret" not in rendered


def test_the_evidence_store_is_rooted_at_the_configured_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(monkeypatch, EVIDENCE_ROOT="C:/evidence")

    store = ledger_ops.build_evidence_store(settings)

    assert store.root == Path("C:/evidence").resolve()
    assert store.root != Path(".local/evidence").resolve()


def test_the_evidence_store_falls_back_to_the_documented_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ledger_ops.build_evidence_store(_settings(monkeypatch))

    assert store.root == Path(".local/evidence").resolve()
