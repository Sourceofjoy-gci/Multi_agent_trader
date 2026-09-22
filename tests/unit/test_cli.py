"""Operator commands must render deterministic JSON and stable exit codes."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from trading_house import cli
from trading_house.constitution.signing import (
    decode_signature,
    generate_key_pair,
    load_public_key,
    verify_signature,
)
from trading_house.core.errors import (
    AuditAppendError,
    AuditIntegrityError,
    ConfigurationError,
    CoverageError,
    DatabaseUnavailableError,
    InsufficientHistoryError,
    MigrationMismatchError,
    SchemaValidationError,
    SignatureVerificationError,
    TradingHouseError,
)
from trading_house.marketdata.models import IngestOutcome, IngestRun

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = PROJECT_ROOT / "config"
FAKE_DSN = "postgresql://runtime:super-secret-password@localhost/trading_house"
_NINE = datetime(2026, 8, 26, 9, 0, tzinfo=UTC)

runner = CliRunner()


@pytest.fixture(autouse=True)
def _reset_debug_state() -> None:
    cli._STATE.debug = False


@pytest.fixture
def _dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADING_HOUSE_DATABASE_DSN", FAKE_DSN)


def _verify_args() -> list[str]:
    return [
        "constitution",
        "verify",
        "--constitution",
        str(CONFIG_DIR / "risk_constitution.yaml"),
        "--signature",
        str(CONFIG_DIR / "risk_constitution.yaml.sig"),
        "--public-key",
        str(CONFIG_DIR / "risk_constitution.public.pem"),
    ]


def _binding_args() -> list[str]:
    return [
        "constitution",
        "binding",
        "--binding",
        str(CONFIG_DIR / "venue_binding.mt5.yaml"),
        "--signature",
        str(CONFIG_DIR / "venue_binding.mt5.yaml.sig"),
        "--public-key",
        str(CONFIG_DIR / "risk_constitution.public.pem"),
    ]


def _raiser(error: Exception):
    def _raise(*_: object, **__: object) -> Any:
        raise error

    return _raise


def _concrete_error_types() -> set[type[TradingHouseError]]:
    pending = [TradingHouseError]
    found: set[type[TradingHouseError]] = set()
    while pending:
        for subclass in pending.pop().__subclasses__():
            found.add(subclass)
            pending.append(subclass)
    return found


def test_app_help_lists_every_command_group() -> None:
    result = runner.invoke(cli.app, ["--help"])

    assert result.exit_code == 0
    for group in ("constitution", "db", "audit", "data", "order", "guard", "health"):
        assert group in result.stdout


def test_constitution_verify_reports_version_and_hashes() -> None:
    result = runner.invoke(cli.app, _verify_args())

    assert result.exit_code == cli.ExitCode.OK
    payload = json.loads(result.stdout)
    assert payload["status"] == "ok"
    assert payload["constitution_version"] == 1
    assert len(payload["constitution_sha256"]) == 64
    assert len(payload["public_key_fingerprint"]) == 64


def test_constitution_verify_output_is_deterministic() -> None:
    first = runner.invoke(cli.app, _verify_args()).stdout
    second = runner.invoke(cli.app, _verify_args()).stdout

    assert first == second
    assert list(json.loads(first)) == sorted(json.loads(first))


def test_signature_failure_uses_stable_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "load_constitution", _raiser(SignatureVerificationError()))

    result = runner.invoke(cli.app, _verify_args())

    assert result.exit_code == cli.ExitCode.SIGNATURE
    assert "PRIVATE" not in result.stdout
    assert "signature verification failed" in result.stderr


def test_constitution_binding_reports_venue_books_and_instruments() -> None:
    result = runner.invoke(cli.app, _binding_args())

    assert result.exit_code == cli.ExitCode.OK
    payload = json.loads(result.stdout)
    assert payload["status"] == "ok"
    assert payload["venue"] == "mt5"
    assert "fx_scalp" in payload["books"]
    assert "fx.eurusd" in payload["instruments"]


def test_constitution_binding_signature_failure_uses_stable_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "load_venue_binding", _raiser(SignatureVerificationError()))

    result = runner.invoke(cli.app, _binding_args())

    assert result.exit_code == cli.ExitCode.SIGNATURE


def test_configuration_failure_uses_stable_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "load_constitution", _raiser(ConfigurationError()))

    result = runner.invoke(cli.app, _verify_args())

    assert result.exit_code == cli.ExitCode.CONFIGURATION


@pytest.mark.usefixtures("_dsn")
def test_database_failure_uses_stable_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "open_runtime_connection", _raiser(DatabaseUnavailableError()))

    result = runner.invoke(cli.app, ["db", "check"])

    assert result.exit_code == cli.ExitCode.DATABASE
    assert "super-secret-password" not in result.stdout + result.stderr


@pytest.mark.usefixtures("_dsn")
def test_migration_mismatch_uses_stable_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "open_runtime_connection", lambda _: _FakeConnection())
    monkeypatch.setattr(cli, "assert_at_head", _raiser(MigrationMismatchError()))

    result = runner.invoke(cli.app, ["db", "check"])

    assert result.exit_code == cli.ExitCode.MIGRATION


@pytest.mark.usefixtures("_dsn")
def test_db_check_reports_head_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "open_runtime_connection", lambda _: _FakeConnection())
    monkeypatch.setattr(cli, "assert_at_head", lambda *_: None)

    result = runner.invoke(cli.app, ["db", "check"])

    assert result.exit_code == cli.ExitCode.OK
    assert json.loads(result.stdout)["revision"] == "head"


class _FakeConnection:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _FakeLedger:
    def __init__(self, report: Any) -> None:
        self._report = report

    def verify(self) -> Any:
        if isinstance(self._report, Exception):
            raise self._report
        return self._report


@pytest.mark.usefixtures("_dsn")
def test_audit_verify_reports_a_valid_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    from trading_house.audit.models import IntegrityReport

    monkeypatch.setattr(
        cli,
        "PostgresAuditLedger",
        lambda _: _FakeLedger(IntegrityReport(valid=True, checked_entries=4)),
    )

    result = runner.invoke(cli.app, ["audit", "verify"])

    assert result.exit_code == cli.ExitCode.OK
    payload = json.loads(result.stdout)
    assert payload["valid"] is True
    assert payload["checked_entries"] == 4


@pytest.mark.usefixtures("_dsn")
def test_audit_verify_exits_with_integrity_code_when_broken(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from trading_house.audit.models import IntegrityReport

    broken = IntegrityReport(
        valid=False, checked_entries=2, first_invalid_sequence=3, reason="entry_hash_mismatch"
    )
    monkeypatch.setattr(cli, "PostgresAuditLedger", lambda _: _FakeLedger(broken))

    result = runner.invoke(cli.app, ["audit", "verify"])

    assert result.exit_code == cli.ExitCode.AUDIT_INTEGRITY
    payload = json.loads(result.stdout)
    assert payload["valid"] is False
    assert payload["first_invalid_sequence"] == 3
    assert payload["reason"] == "entry_hash_mismatch"


@pytest.mark.usefixtures("_dsn")
def test_audit_append_failure_uses_stable_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "PostgresAuditLedger", lambda _: _FakeLedger(AuditAppendError()))

    result = runner.invoke(cli.app, ["audit", "verify"])

    assert result.exit_code == cli.ExitCode.AUDIT_APPEND


@pytest.mark.usefixtures("_dsn")
def test_health_failure_uses_stable_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "load_constitution", _raiser(AuditIntegrityError()))

    result = runner.invoke(cli.app, ["health"])

    assert result.exit_code == cli.ExitCode.AUDIT_INTEGRITY


def test_missing_dsn_is_a_configuration_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRADING_HOUSE_DATABASE_DSN", raising=False)

    result = runner.invoke(cli.app, ["db", "check"])

    assert result.exit_code == cli.ExitCode.CONFIGURATION


def test_unexpected_error_is_redacted_with_a_correlation_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "load_constitution", _raiser(RuntimeError("boom: /secret/path")))

    result = runner.invoke(cli.app, _verify_args())

    assert result.exit_code == 1
    assert "boom" not in result.stderr
    assert "/secret/path" not in result.stderr
    assert "Traceback" not in result.stderr
    payload = json.loads(result.stderr)
    assert payload["detail"] == "unexpected failure"
    assert len(payload["correlation_id"]) == 36


def test_debug_flag_reraises_unexpected_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "load_constitution", _raiser(RuntimeError("boom")))

    result = runner.invoke(cli.app, ["--debug", *_verify_args()])

    assert result.exit_code != 0
    assert isinstance(result.exception, RuntimeError)


def test_every_typed_error_has_a_stable_exit_code() -> None:
    """A new typed error must not silently inherit someone else's exit code."""

    assert _concrete_error_types() <= set(cli.EXIT_CODES)


def test_exit_codes_are_distinct_per_failure_domain() -> None:
    assert cli.EXIT_CODES[SignatureVerificationError] == cli.ExitCode.SIGNATURE
    assert cli.EXIT_CODES[DatabaseUnavailableError] == cli.ExitCode.DATABASE
    assert cli.EXIT_CODES[MigrationMismatchError] == cli.ExitCode.MIGRATION
    assert cli.EXIT_CODES[AuditIntegrityError] == cli.ExitCode.AUDIT_INTEGRITY
    assert cli.EXIT_CODES[AuditAppendError] == cli.ExitCode.AUDIT_APPEND
    assert cli.EXIT_CODES[SchemaValidationError] == cli.ExitCode.CONFIGURATION
    assert cli.EXIT_CODES[CoverageError] == cli.ExitCode.COVERAGE


def test_insufficient_history_is_distinguishable_from_missing_coverage() -> None:
    """Different remedies: one wants a backfill, the other wants patience or
    a shorter period. A script branching on exit code has to tell them apart."""

    assert cli.EXIT_CODES[InsufficientHistoryError] == cli.ExitCode.INSUFFICIENT_HISTORY
    assert cli.EXIT_CODES[CoverageError] != cli.EXIT_CODES[InsufficientHistoryError]


def _keypair(tmp_path: Path) -> tuple[Path, Path]:
    private_path = tmp_path / "keys" / "signing.private.pem"
    public_path = tmp_path / "keys" / "signing.public.pem"
    generate_key_pair(private_path, public_path)
    return private_path, public_path


def test_sign_requires_explicit_paths() -> None:
    result = runner.invoke(cli.app, ["constitution", "sign"])

    assert result.exit_code != 0


def test_sign_writes_a_verifiable_signature(tmp_path: Path) -> None:
    private_path, public_path = _keypair(tmp_path)
    constitution = CONFIG_DIR / "risk_constitution.yaml"
    signature_output = tmp_path / "out" / "constitution.sig"

    result = runner.invoke(
        cli.app,
        [
            "constitution",
            "sign",
            "--constitution",
            str(constitution),
            "--private-key",
            str(private_path),
            "--signature-output",
            str(signature_output),
        ],
    )

    assert result.exit_code == cli.ExitCode.OK
    encoded = signature_output.read_bytes()
    assert encoded.endswith(b"\n")
    assert base64.b64encode(base64.b64decode(encoded.strip(), validate=True)) == encoded.strip()
    verify_signature(
        load_public_key(public_path.read_bytes()),
        decode_signature(encoded),
        constitution.read_bytes(),
    )


def test_sign_refuses_to_overwrite_without_force(tmp_path: Path) -> None:
    private_path, _ = _keypair(tmp_path)
    signature_output = tmp_path / "existing.sig"
    signature_output.write_bytes(b"original\n")

    result = runner.invoke(
        cli.app,
        [
            "constitution",
            "sign",
            "--constitution",
            str(CONFIG_DIR / "risk_constitution.yaml"),
            "--private-key",
            str(private_path),
            "--signature-output",
            str(signature_output),
        ],
    )

    assert result.exit_code == cli.ExitCode.CONFIGURATION
    assert signature_output.read_bytes() == b"original\n"


def test_sign_overwrites_with_force(tmp_path: Path) -> None:
    private_path, public_path = _keypair(tmp_path)
    constitution = CONFIG_DIR / "risk_constitution.yaml"
    signature_output = tmp_path / "existing.sig"
    signature_output.write_bytes(b"original\n")

    result = runner.invoke(
        cli.app,
        [
            "constitution",
            "sign",
            "--constitution",
            str(constitution),
            "--private-key",
            str(private_path),
            "--signature-output",
            str(signature_output),
            "--force",
        ],
    )

    assert result.exit_code == cli.ExitCode.OK
    verify_signature(
        load_public_key(public_path.read_bytes()),
        decode_signature(signature_output.read_bytes()),
        constitution.read_bytes(),
    )


def test_sign_never_echoes_private_material(tmp_path: Path) -> None:
    private_path, _ = _keypair(tmp_path)
    signature_output = tmp_path / "out.sig"

    result = runner.invoke(
        cli.app,
        [
            "constitution",
            "sign",
            "--constitution",
            str(CONFIG_DIR / "risk_constitution.yaml"),
            "--private-key",
            str(private_path),
            "--signature-output",
            str(signature_output),
        ],
    )

    rendered = result.stdout + result.stderr
    assert "PRIVATE" not in rendered
    assert "BEGIN" not in rendered


def test_sign_with_an_unreadable_key_stays_typed(tmp_path: Path) -> None:
    signature_output = tmp_path / "out.sig"

    result = runner.invoke(
        cli.app,
        [
            "constitution",
            "sign",
            "--constitution",
            str(CONFIG_DIR / "risk_constitution.yaml"),
            "--private-key",
            str(tmp_path / "absent.pem"),
            "--signature-output",
            str(signature_output),
        ],
    )

    assert result.exit_code == cli.ExitCode.SIGNATURE
    assert not signature_output.exists()


def test_failed_sign_leaves_no_temporary_files(tmp_path: Path) -> None:
    output_dir = tmp_path / "out"
    output_dir.mkdir()

    runner.invoke(
        cli.app,
        [
            "constitution",
            "sign",
            "--constitution",
            str(CONFIG_DIR / "risk_constitution.yaml"),
            "--private-key",
            str(tmp_path / "absent.pem"),
            "--signature-output",
            str(output_dir / "out.sig"),
        ],
    )

    assert list(output_dir.iterdir()) == []


def test_broker_errors_have_stable_exit_codes() -> None:
    from trading_house.core.errors import BrokerUnavailableError, NonDemoAccountError

    assert cli.EXIT_CODES[BrokerUnavailableError] == cli.ExitCode.BROKER
    assert cli.EXIT_CODES[NonDemoAccountError] == cli.ExitCode.ACCOUNT_MODE


# --- the health gate's venue-reconciliation step ----------------------------
#
# Reconciliation reports; it does not fail readiness, and that tolerance
# extends to the step being unavailable entirely. The one thing it must never
# tolerate is a live account.

_BINDING_YAML = b"""
venue: mt5
books:
  fx_scalp: {magic_range: [110000, 119999]}
  fx_swing: {magic_range: [120000, 129999]}
instruments:
  fx.eurusd: {server_symbol: "EURUSD"}
"""


class _RecordingLedger:
    def __init__(self) -> None:
        self.appended: list[Any] = []

    def append(self, event: Any) -> None:
        self.appended.append(event)


class _StubTerminal:
    """A TerminalPort needing neither MetaTrader 5 nor Windows."""

    def __init__(self, *, trade_mode: int = 0, initialises: bool = True) -> None:
        self.trade_mode = trade_mode
        self.initialises = initialises
        self.shutdown_calls = 0

    def initialize(self) -> bool:
        return self.initialises

    def shutdown(self) -> None:
        self.shutdown_calls += 1

    def account_trade_mode(self) -> int:
        return self.trade_mode

    def terminal_connected(self) -> bool:
        return True

    def server_utc_offset_seconds(self) -> int:
        return 0

    def symbol_info(self, server_symbol: str) -> Any:
        return None

    def symbol_tick(self, server_symbol: str) -> Any:
        return None

    def copy_rates_range(
        self, server_symbol: str, timeframe_minutes: int, start: object, end: object
    ) -> tuple[Any, ...]:
        return ()

    def positions(self) -> tuple[Any, ...]:
        return ()

    def order_check(self, request: Any) -> Any:
        return None

    def last_error(self) -> tuple[int, str]:
        return 0, "ok"


def _reconciler(tmp_path: Path, ledger: Any) -> Any:
    binding = tmp_path / "venue_binding.mt5.yaml"
    binding.write_bytes(_BINDING_YAML)
    return cli._book_reconciler(binding, tmp_path / "sig", tmp_path / "key", ledger)


def test_a_missing_venue_binding_skips_reconciliation_rather_than_failing(
    tmp_path: Path,
) -> None:
    reconcile = cli._book_reconciler(
        tmp_path / "absent.yaml", tmp_path / "sig", tmp_path / "key", _RecordingLedger()
    )

    assert reconcile() == {}


def test_an_unavailable_metatrader5_skips_reconciliation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The coverage-gated CI job runs on Linux, where MetaTrader5 cannot even
    be imported. Health must still report on its other four steps."""

    monkeypatch.setattr(cli, "_mt5_terminal_factory", lambda: None)

    assert _reconciler(tmp_path, _RecordingLedger())() == {}


def test_a_terminal_that_will_not_initialise_skips_reconciliation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from trading_house.constitution.binding import parse_venue_binding

    monkeypatch.setattr(
        cli, "_mt5_terminal_factory", lambda: lambda _s: _StubTerminal(initialises=False)
    )
    monkeypatch.setattr(cli, "load_venue_binding", lambda *a: parse_venue_binding(_BINDING_YAML))

    assert _reconciler(tmp_path, _RecordingLedger())() == {}


def test_a_live_account_is_never_degraded_away(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The demo guard is the whole safety story of this phase. Every other
    venue failure degrades to 'step not performed'; this one must not, or a
    live account becomes indistinguishable from an absent terminal."""

    from trading_house.constitution.binding import parse_venue_binding
    from trading_house.core.errors import NonDemoAccountError

    terminal = _StubTerminal(trade_mode=2)  # ACCOUNT_TRADE_MODE_REAL
    monkeypatch.setattr(cli, "_mt5_terminal_factory", lambda: lambda _s: terminal)
    monkeypatch.setattr(cli, "load_venue_binding", lambda *a: parse_venue_binding(_BINDING_YAML))

    with pytest.raises(NonDemoAccountError):
        _reconciler(tmp_path, _RecordingLedger())()

    assert terminal.shutdown_calls == 1


def test_a_refused_live_account_still_leaves_an_audit_trail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The connection attempt happened. It belongs in the hash chain even
    though the gateway refused to serve a single request over it."""

    from trading_house.constitution.binding import parse_venue_binding
    from trading_house.core.errors import NonDemoAccountError

    ledger = _RecordingLedger()
    monkeypatch.setattr(
        cli, "_mt5_terminal_factory", lambda: lambda _s: _StubTerminal(trade_mode=2)
    )
    monkeypatch.setattr(cli, "load_venue_binding", lambda *a: parse_venue_binding(_BINDING_YAML))

    with pytest.raises(NonDemoAccountError):
        _reconciler(tmp_path, ledger)()

    assert [event.event_type for event in ledger.appended] == ["gateway.connected"]
    assert all("account" not in str(event.payload).lower() for event in ledger.appended)


def test_a_demo_account_reconciles_every_declared_book(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from trading_house.constitution.binding import parse_venue_binding

    ledger = _RecordingLedger()
    monkeypatch.setattr(cli, "_mt5_terminal_factory", lambda: lambda _s: _StubTerminal())
    monkeypatch.setattr(cli, "load_venue_binding", lambda *a: parse_venue_binding(_BINDING_YAML))

    reports = _reconciler(tmp_path, ledger)()

    assert set(reports) == {"fx_scalp", "fx_swing"}
    assert [event.event_type for event in ledger.appended] == [
        "gateway.connected",
        "gateway.demo_verified",
        "gateway.reconciled",
        "gateway.disconnected",
    ]


def test_an_unverifiable_venue_binding_fails_the_gate_rather_than_degrading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A binding that is present but whose signature does not verify is a
    tampering signal, not an absent venue. Degrading it to 'step not
    performed' would let an edited symbol map pass unnoticed."""

    monkeypatch.setattr(cli, "_mt5_terminal_factory", lambda: lambda _s: _StubTerminal())

    with pytest.raises(SignatureVerificationError):
        _reconciler(tmp_path, _RecordingLedger())()


def test_an_unreachable_venue_leaves_a_reason_in_the_hash_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty books_reconciled otherwise reads identically whether the
    account held no positions or the broker was never reached. That is how a
    binding naming a symbol the broker does not have goes unnoticed."""

    from trading_house.constitution.binding import parse_venue_binding

    ledger = _RecordingLedger()
    monkeypatch.setattr(
        cli, "_mt5_terminal_factory", lambda: lambda _s: _StubTerminal(initialises=False)
    )
    monkeypatch.setattr(cli, "load_venue_binding", lambda *a: parse_venue_binding(_BINDING_YAML))

    assert _reconciler(tmp_path, ledger)() == {}

    assert [event.event_type for event in ledger.appended] == ["venue.skipped"]
    assert "reason" in ledger.appended[0].payload
    assert all("account" not in str(event.payload).lower() for event in ledger.appended)


def test_an_absent_metatrader5_is_recorded_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = _RecordingLedger()
    monkeypatch.setattr(cli, "_mt5_terminal_factory", lambda: None)

    assert _reconciler(tmp_path, ledger)() == {}

    assert [event.event_type for event in ledger.appended] == ["venue.skipped"]


# --- data backfill / update / coverage --------------------------------------
#
# backfill and update need a live provider; unlike the health gate's venue
# step, an absent terminal here must fail rather than degrade to an empty
# success. coverage needs only the store and the signed binding's instrument
# list -- never the broker.

_DATA_BINDING_YAML = b"""
venue: mt5
books:
  fx_scalp: {magic_range: [110000, 119999]}
instruments:
  fx.eurusd: {server_symbol: "EURUSD"}
  fx.gbpusd: {server_symbol: "GBPUSD"}
"""


def _data_binding(tmp_path: Path) -> Path:
    binding = tmp_path / "venue_binding.mt5.yaml"
    binding.write_bytes(_DATA_BINDING_YAML)
    return binding


def _data_args(tmp_path: Path, *rest: str) -> list[str]:
    return [
        "data",
        *rest,
        "--venue-binding",
        str(_data_binding(tmp_path)),
        "--venue-binding-signature",
        str(tmp_path / "sig"),
        "--venue-binding-public-key",
        str(tmp_path / "key"),
    ]


def _fake_ingest_run(instrument_id: str, timeframe: Any) -> IngestRun:
    instant = _NINE
    return IngestRun(
        run_id=uuid4(),
        instrument_id=instrument_id,
        timeframe=timeframe,
        requested_from=instant,
        requested_to=instant,
        started_at=instant,
        finished_at=instant,
        earliest_event_time=None,
        bars_returned=0,
        bars_stored=0,
        bars_rejected=0,
        bars_conflicting=0,
        expected_bars=0,
        coverage_ratio=Decimal("1"),
        outcome=IngestOutcome.EMPTY,
        detail=None,
    )


def test_backfill_without_instrument_exits_nonzero() -> None:
    result = runner.invoke(
        cli.app,
        ["data", "backfill", "--timeframe", "H1", "--from", "2015-01-01"],
    )

    assert result.exit_code != 0


def test_backfill_without_timeframe_exits_nonzero() -> None:
    result = runner.invoke(
        cli.app,
        ["data", "backfill", "--instrument", "fx.eurusd", "--from", "2015-01-01"],
    )

    assert result.exit_code != 0


def test_backfill_rejects_an_unsupported_timeframe() -> None:
    result = runner.invoke(
        cli.app,
        [
            "data",
            "backfill",
            "--instrument",
            "fx.eurusd",
            "--timeframe",
            "M2",
            "--from",
            "2015-01-01",
        ],
    )

    assert result.exit_code != 0


@pytest.mark.usefixtures("_dsn")
def test_update_iterates_the_signed_binding_across_every_timeframe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from trading_house.constitution.binding import parse_venue_binding
    from trading_house.marketdata.models import Timeframe

    monkeypatch.setattr(cli, "_mt5_terminal_factory", lambda: lambda _s: _StubTerminal())
    monkeypatch.setattr(
        cli, "load_venue_binding", lambda *a: parse_venue_binding(_DATA_BINDING_YAML)
    )

    calls: list[tuple[str, Timeframe]] = []

    def _fake_update(*_args: Any, **kwargs: Any) -> Any:
        calls.append((kwargs["instrument_id"], kwargs["timeframe"]))
        return _fake_ingest_run(kwargs["instrument_id"], kwargs["timeframe"])

    monkeypatch.setattr(cli, "update", _fake_update)

    result = runner.invoke(cli.app, _data_args(tmp_path, "update"))

    assert result.exit_code == cli.ExitCode.OK
    assert len(calls) == 2 * len(Timeframe)
    assert {instrument for instrument, _ in calls} == {"fx.eurusd", "fx.gbpusd"}
    assert {timeframe for _, timeframe in calls} == set(Timeframe)


@pytest.mark.usefixtures("_dsn")
def test_backfill_calls_ingest_with_the_requested_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from trading_house.constitution.binding import parse_venue_binding
    from trading_house.marketdata.models import Timeframe

    monkeypatch.setattr(cli, "_mt5_terminal_factory", lambda: lambda _s: _StubTerminal())
    monkeypatch.setattr(
        cli, "load_venue_binding", lambda *a: parse_venue_binding(_DATA_BINDING_YAML)
    )

    calls: list[dict[str, Any]] = []

    def _fake_backfill(*_args: Any, **kwargs: Any) -> Any:
        calls.append(kwargs)
        return _fake_ingest_run(kwargs["instrument_id"], kwargs["timeframe"])

    monkeypatch.setattr(cli, "backfill", _fake_backfill)

    result = runner.invoke(
        cli.app,
        _data_args(
            tmp_path,
            "backfill",
            "--instrument",
            "fx.eurusd",
            "--timeframe",
            "H1",
            "--from",
            "2015-01-01",
        ),
    )

    assert result.exit_code == cli.ExitCode.OK
    assert len(calls) == 1
    assert calls[0]["instrument_id"] == "fx.eurusd"
    assert calls[0]["timeframe"] == Timeframe.H1
    assert calls[0]["until"] == datetime(2015, 1, 1, tzinfo=UTC)


@pytest.mark.usefixtures("_dsn")
def test_coverage_renders_deterministic_json_with_sorted_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from trading_house.constitution.binding import parse_venue_binding
    from trading_house.marketdata.models import Coverage, Timeframe

    monkeypatch.setattr(
        cli, "load_venue_binding", lambda *a: parse_venue_binding(_DATA_BINDING_YAML)
    )

    def _fake_coverage(_self: Any, instrument_id: str, timeframe: Timeframe) -> Coverage:
        return Coverage(
            instrument_id=instrument_id,
            timeframe=timeframe,
            earliest_event_time=None,
            latest_event_time=None,
            latest_availability_time=None,
            clean_bars=0,
            defective_bars=0,
        )

    monkeypatch.setattr(cli.PostgresBarStore, "coverage", _fake_coverage)

    first = runner.invoke(cli.app, _data_args(tmp_path, "coverage"))
    second = runner.invoke(cli.app, _data_args(tmp_path, "coverage"))

    assert first.exit_code == cli.ExitCode.OK
    assert first.stdout == second.stdout
    payload = json.loads(first.stdout)
    assert payload["status"] == "ok"
    assert set(payload["coverage"]) == {"fx.eurusd", "fx.gbpusd"}
    assert set(payload["coverage"]["fx.eurusd"]) == {tf.value for tf in Timeframe}
    assert json.dumps(payload, sort_keys=True) == first.stdout.strip()


@pytest.mark.usefixtures("_dsn")
def test_data_commands_never_print_a_dsn_on_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from trading_house.constitution.binding import parse_venue_binding
    from trading_house.marketdata.models import Coverage

    monkeypatch.setattr(cli, "_mt5_terminal_factory", lambda: lambda _s: _StubTerminal())
    monkeypatch.setattr(
        cli, "load_venue_binding", lambda *a: parse_venue_binding(_DATA_BINDING_YAML)
    )
    monkeypatch.setattr(
        cli,
        "backfill",
        lambda *_a, **k: _fake_ingest_run(k["instrument_id"], k["timeframe"]),
    )
    monkeypatch.setattr(
        cli,
        "update",
        lambda *_a, **k: _fake_ingest_run(k["instrument_id"], k["timeframe"]),
    )
    monkeypatch.setattr(
        cli.PostgresBarStore,
        "coverage",
        lambda _self, instrument_id, timeframe: Coverage(
            instrument_id=instrument_id,
            timeframe=timeframe,
            earliest_event_time=None,
            latest_event_time=None,
            latest_availability_time=None,
            clean_bars=0,
            defective_bars=0,
        ),
    )

    results = [
        runner.invoke(
            cli.app,
            _data_args(
                tmp_path,
                "backfill",
                "--instrument",
                "fx.eurusd",
                "--timeframe",
                "H1",
                "--from",
                "2015-01-01",
            ),
        ),
        runner.invoke(cli.app, _data_args(tmp_path, "update")),
        runner.invoke(cli.app, _data_args(tmp_path, "coverage")),
    ]

    for result in results:
        assert result.exit_code == cli.ExitCode.OK
        combined = result.stdout + result.stderr
        assert "super-secret-password" not in combined
        assert FAKE_DSN not in combined


@pytest.mark.usefixtures("_dsn")
def test_data_coverage_failure_never_prints_a_dsn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from trading_house.constitution.binding import parse_venue_binding

    monkeypatch.setattr(
        cli, "load_venue_binding", lambda *a: parse_venue_binding(_DATA_BINDING_YAML)
    )
    monkeypatch.setattr(cli, "open_runtime_connection", _raiser(DatabaseUnavailableError()))

    result = runner.invoke(cli.app, _data_args(tmp_path, "coverage"))

    assert result.exit_code == cli.ExitCode.DATABASE
    combined = result.stdout + result.stderr
    assert "super-secret-password" not in combined
    assert FAKE_DSN not in combined


def test_an_unreachable_broker_fails_backfill_rather_than_degrading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unlike the health gate's venue step, backfill exists to fetch bars: an
    absent terminal must be a typed failure, never a silent empty success."""

    monkeypatch.setattr(cli, "_mt5_terminal_factory", lambda: None)

    result = runner.invoke(
        cli.app,
        _data_args(
            tmp_path,
            "backfill",
            "--instrument",
            "fx.eurusd",
            "--timeframe",
            "H1",
            "--from",
            "2015-01-01",
        ),
    )

    assert result.exit_code == cli.ExitCode.BROKER


def test_the_daemon_stop_event_starts_unset() -> None:
    """The seam a guard-run test replaces. In production it must be a plain,
    unset event: one that arrived already set would exit the daemon on its
    first check, and every position would go unguarded with nothing to say
    why."""

    stop = cli._stop_event()

    assert not stop.is_set()


# --- backtest run: the input boundary's two error shapes -------------------
#
# ``BacktestRequest`` raises a bare ``ValueError`` on a non-positive equity at
# CONSTRUCTION, while a bad date range raises ``BacktestRefused`` from ``run``.
# A caller wrapping only one of those catches one and not the other, so both
# are pinned here: neither may reach an operator as "unexpected failure" with a
# correlation id, which is what the CLI's catch-all does to anything untyped.


def _contract_file(tmp_path: Path) -> Path:
    from tests.unit.research.backtest.conftest import _contract

    path = tmp_path / "contract.json"
    path.write_text(_contract().model_dump_json(), encoding="utf-8")
    return path


def _printed_strings(result: Any) -> str:
    """What the command actually printed, with both layers of escaping undone.

    Searching a raw stream for a leaked Windows path is escape-blind, and
    twice over. ``OSError.__str__`` reprs the filename, which doubles every
    backslash; ``json.dumps`` then doubles them again on the wire. So a
    ``FileNotFoundError`` that prints the operator's whole path arrives as
    four backslashes per separator and matches no path any test holds.
    ``json.loads`` undoes the outer layer and collapsing the doubles undoes
    the inner one. On POSIX every spelling already agrees, which is exactly
    how a check that asserts nothing here would have shipped unnoticed.

    Two narrow false negatives are left standing rather than coded around: a
    leak escaped a third time collapses to two backslashes and stops matching,
    and a ``tmp_path`` on a UNC share would have its own leading ``\\``
    collapsed. Neither is reachable in this repository's layout. What is not
    left standing is the empty case -- a command that exits without printing
    would satisfy every ``not in`` below while proving nothing, so this refuses
    to return nothing at all.
    """

    streams = [stream for stream in (result.stdout, result.stderr) if stream.strip()]
    printed = " ".join(str(value) for stream in streams for value in json.loads(stream).values())
    assert printed, "the command printed nothing, so a leak assertion would be vacuous"
    return printed.replace("\\\\", "\\")


def _backtest_args(tmp_path: Path, **overrides: str) -> list[str]:
    options: dict[str, str] = {
        "--strategy": "toy",
        "--toy-every-n": "20",
        "--instrument": "fx.eurusd",
        "--timeframe": "M1",
        "--start": "2026-09-21T09:00:00",
        "--end": "2026-09-21T09:59:00",
        "--firm-equity": "100000",
        "--contract": str(_contract_file(tmp_path)),
        "--atr-period": "2",
        "--spread-window": "10",
        "--commission-per-lot-per-side": "3.50",
        "--slippage-points-per-side": "0",
        "--swap-long-points-per-day": "-0.80",
        "--swap-short-points-per-day": "0.30",
        "--triple-swap-weekday": "2",
    }
    options.update(overrides)
    return ["backtest", "run", *[value for pair in options.items() for value in pair]]


@pytest.mark.usefixtures("_dsn")
def test_backtest_rejects_an_unregistered_strategy(tmp_path: Path) -> None:
    """Phase 6 has one registrable strategy. An unknown id is a typed
    configuration failure rather than a silent run of the toy."""

    result = runner.invoke(cli.app, _backtest_args(tmp_path, **{"--strategy": "momentum"}))

    assert result.exit_code == cli.ExitCode.CONFIGURATION
    assert json.loads(result.stderr)["status"] == "error"


@pytest.mark.usefixtures("_dsn")
def test_backtest_surfaces_a_non_positive_equity_as_a_configuration_failure(
    tmp_path: Path,
) -> None:
    """Shape one: a bare ``ValueError`` from ``BacktestRequest.__post_init__``,
    raised before ``run`` is ever called. Untranslated it lands in the CLI's
    catch-all and an operator gets a correlation id for a typo."""

    result = runner.invoke(cli.app, _backtest_args(tmp_path, **{"--firm-equity": "0"}))

    assert result.exit_code == cli.ExitCode.CONFIGURATION
    payload = json.loads(result.stderr)
    assert payload["status"] == "error"
    assert "correlation_id" not in payload


@pytest.mark.usefixtures("_dsn")
def test_backtest_surfaces_an_unparseable_equity_as_a_configuration_failure(
    tmp_path: Path,
) -> None:
    """``Decimal("abc")`` raises ``InvalidOperation``, which is an
    ``ArithmeticError`` and not a ``ValueError`` -- so it escapes any handler
    written for the shape above."""

    result = runner.invoke(cli.app, _backtest_args(tmp_path, **{"--firm-equity": "abc"}))

    assert result.exit_code == cli.ExitCode.CONFIGURATION
    assert "correlation_id" not in json.loads(result.stderr)


@pytest.mark.usefixtures("_dsn")
def test_backtest_surfaces_a_refusal_with_its_kind_and_nothing_else(tmp_path: Path) -> None:
    """Shape two: ``BacktestRefused`` out of ``run``. A backwards range reaches
    no bar in any store, so it is refused before the store is touched -- which
    is why this needs no database.

    The refusal's kind is a closed enum and is the whole diagnosis; the message
    carries no free text, so nothing from a DSN or a broker message can ride
    out on it.
    """

    args = _backtest_args(
        tmp_path, **{"--start": "2026-09-21T09:59:00", "--end": "2026-09-21T09:00:00"}
    )

    result = runner.invoke(cli.app, args)

    assert result.exit_code == cli.ExitCode.CONFIGURATION
    payload = json.loads(result.stderr)
    assert payload == {
        "status": "error",
        "detail": "backtest refused",
        "refusal": "coverage",
    }


@pytest.mark.usefixtures("_dsn")
def test_backtest_errors_never_echo_a_path_or_a_credential(tmp_path: Path) -> None:
    """No command in this system prints a path, a credential or a key, and the
    backtest command is handed both a DSN (through the environment) and a file
    path (through ``--contract``).

    A MISSING contract, not a bad equity. ``--firm-equity 0`` cannot leak
    either string under any single production change: its handler answers with
    ``ConfigurationError``'s fixed class string, and rewriting that handler to
    echo ``str(error)`` still yields only "firm_equity must be positive". A
    missing file is the one error path here that genuinely has something to
    lose -- ``FileNotFoundError``'s message embeds the path the operator typed,
    and the fixed string is the only thing between it and stderr. A future "let
    us give a more helpful error" edit is exactly how that would leak.
    """

    args = _backtest_args(tmp_path, **{"--contract": str(tmp_path / "absent" / "contract.json")})

    result = runner.invoke(cli.app, args)

    assert result.exit_code == cli.ExitCode.CONFIGURATION
    output = _printed_strings(result)
    assert "super-secret-password" not in output
    assert str(tmp_path) not in output


@pytest.mark.usefixtures("_dsn")
def test_backtest_surfaces_a_malformed_contract_file_as_a_configuration_failure(
    tmp_path: Path,
) -> None:
    """``json.JSONDecodeError`` is a ``ValueError``, not an ``OSError``.

    ``--contract`` is the one input to this command with no producer anywhere
    in the repo -- every operator hand-writes that file -- so a trailing comma
    in it is the likeliest mistake the command ever sees. A handler written for
    the unreadable-file case alone lets it through to the catch-all, where a
    typo comes back as a correlation id.
    """

    malformed = tmp_path / "malformed.json"
    malformed.write_text('{"instrument_id": "fx.eurusd",}', encoding="utf-8")

    result = runner.invoke(cli.app, _backtest_args(tmp_path, **{"--contract": str(malformed)}))

    assert result.exit_code == cli.ExitCode.CONFIGURATION
    assert "correlation_id" not in json.loads(result.stderr)


@pytest.mark.parametrize("equity", ["NaN", "Infinity"])
@pytest.mark.usefixtures("_dsn")
def test_backtest_surfaces_a_non_finite_equity_as_a_configuration_failure(
    tmp_path: Path, equity: str
) -> None:
    """Shape three, which ``Decimal(value)`` alone cannot see.

    Both CONSTRUCT cleanly, so catching the construction is not enough.
    ``NaN <= 0`` raises ``InvalidOperation`` from inside ``BacktestRequest`` --
    an ``ArithmeticError``, so it slips past the ``ValueError`` handler wrapping
    that construction and lands in the catch-all. ``Infinity <= 0`` is worse:
    it is simply ``False``, so an infinite equity is accepted as a positive one
    and the command carries on to open a database connection with it.

    ``-Infinity`` is deliberately NOT parametrized here. The existing
    non-positive check already refuses it, so the case would pass with the
    finiteness guard removed -- a parametrization that cannot fail, which is
    the defect this phase keeps finding.
    """

    result = runner.invoke(cli.app, _backtest_args(tmp_path, **{"--firm-equity": equity}))

    assert result.exit_code == cli.ExitCode.CONFIGURATION
    assert "correlation_id" not in json.loads(result.stderr)


@pytest.mark.usefixtures("_dsn")
def test_backtest_prints_the_result_its_digest_and_the_margin_disclosure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The success path, which otherwise runs only under Docker.

    Every other case here is an error path, so a default ``uv run pytest -q``
    that deselects ``integration`` never executes the payload assembly at all --
    and that payload is the one piece of Phase 6 output shaping nothing outside
    Docker touches, and the shape Phase 8 will hash. The bar store is the only
    fake: the constitution, the risk engine and the simulator are real.

    The digest is re-derived from the emitted result rather than compared to a
    literal, so the assertion catches a payload whose digest belongs to some
    other object without needing an update on every legitimate change.
    """

    from tests.unit.research.backtest.conftest import FakeBarReader, _ramp
    from trading_house.research.backtest.result import BacktestResult

    monkeypatch.setattr(cli, "_bar_store", lambda: FakeBarReader(_ramp(60)))

    result = runner.invoke(cli.app, _backtest_args(tmp_path))

    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "ok"
    # Section 8.1's headroom gate is off in a replay; the JSON has to say so,
    # because ``BacktestResult`` is frozen and cannot.
    assert payload["margin_modelled"] is False
    emitted = BacktestResult.model_validate_json(json.dumps(payload["result"]))
    assert payload["digest"] == emitted.digest()
    assert emitted.trades


@pytest.mark.usefixtures("_dsn")
def test_backtest_refuses_a_contract_for_a_different_instrument(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The silent-empty-run shape, closed at the composition root.

    Nothing downstream compares ``--contract``'s ``instrument_id`` to
    ``--instrument``. The risk engine does reject the mismatch -- but as a
    ``RejectedRiskDecision``, which the replay loop records and continues past,
    so the command exits 0 with zero trades, N rejections and a payload that
    looks like a strategy that simply never fired. ``ops/backtest.py`` calls
    that "the worst failure shape available" and closes it for the clock; this
    closes the same shape reached one argument over.

    The bar store is faked so the assertion is about the mismatch rather than
    about an absent database -- without it this would exit CONFIGURATION for
    the wrong reason, which is exactly the kind of pass this fix exists to
    prevent.
    """

    from tests.unit.research.backtest.conftest import FakeBarReader, _ramp

    monkeypatch.setattr(cli, "_bar_store", lambda: FakeBarReader(_ramp(60)))

    result = runner.invoke(cli.app, _backtest_args(tmp_path, **{"--instrument": "fx.gbpusd"}))

    assert result.exit_code == cli.ExitCode.CONFIGURATION
    payload = json.loads(result.stderr)
    assert payload["status"] == "error"
    assert "correlation_id" not in payload
    # Guard the guard: the very same invocation with the matching instrument
    # succeeds, so the refusal above is the mismatch and not the fixture.
    assert runner.invoke(cli.app, _backtest_args(tmp_path)).exit_code == 0
