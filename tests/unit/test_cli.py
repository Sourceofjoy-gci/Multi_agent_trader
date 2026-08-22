"""Operator commands must render deterministic JSON and stable exit codes."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

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
    DatabaseUnavailableError,
    MigrationMismatchError,
    SchemaValidationError,
    SignatureVerificationError,
    TradingHouseError,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = PROJECT_ROOT / "config"
FAKE_DSN = "postgresql://runtime:super-secret-password@localhost/trading_house"

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
    for group in ("constitution", "db", "audit", "health"):
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
