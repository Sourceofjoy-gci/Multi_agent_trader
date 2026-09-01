"""Operator commands with stable, typed exit codes.

Every command constructs its dependencies, calls exactly one public service,
and renders deterministic JSON. Typed domain errors map to a stable exit code;
anything unexpected is reported as a correlation id rather than a traceback, so
no path, credential, or key material can reach operator output.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, cast
from uuid import uuid4

import typer
from alembic.config import Config
from pydantic import JsonValue, ValidationError

from trading_house import __version__
from trading_house.audit.repository import PostgresAuditLedger
from trading_house.brokers.base import ReconciliationReport
from trading_house.brokers.mt5.adapter import Mt5BrokerAdapter
from trading_house.brokers.mt5.boundary import TerminalPort
from trading_house.brokers.mt5.gateway import Mt5Gateway
from trading_house.constitution.binding import load_venue_binding
from trading_house.constitution.loader import load_constitution
from trading_house.constitution.signing import load_private_key, sign_bytes
from trading_house.core.clock import SystemClock
from trading_house.core.errors import (
    AuditAppendError,
    AuditIntegrityError,
    BrokerUnavailableError,
    ConfigurationError,
    CoverageError,
    DatabaseUnavailableError,
    ExitCode,
    MigrationMismatchError,
    NonDemoAccountError,
    SchemaValidationError,
    SignatureVerificationError,
    TimestampError,
    TradingHouseError,
)
from trading_house.core.values import BookId
from trading_house.database.connection import open_runtime_connection
from trading_house.database.migrations import assert_at_head
from trading_house.ops.health import BookReconciler, HealthService, build_audit_event
from trading_house.settings import RuntimeSettings

DEFAULT_CONSTITUTION = Path("config/risk_constitution.yaml")
DEFAULT_SIGNATURE = Path("config/risk_constitution.yaml.sig")
DEFAULT_PUBLIC_KEY = Path("config/risk_constitution.public.pem")
DEFAULT_BINDING = Path("config/venue_binding.mt5.yaml")
DEFAULT_BINDING_SIGNATURE = Path("config/venue_binding.mt5.yaml.sig")
DEFAULT_ALEMBIC_CONFIG = Path("alembic.ini")

EXIT_CODES: dict[type[TradingHouseError], ExitCode] = {
    TimestampError: ExitCode.CONFIGURATION,
    ConfigurationError: ExitCode.CONFIGURATION,
    SchemaValidationError: ExitCode.CONFIGURATION,
    SignatureVerificationError: ExitCode.SIGNATURE,
    DatabaseUnavailableError: ExitCode.DATABASE,
    MigrationMismatchError: ExitCode.MIGRATION,
    AuditAppendError: ExitCode.AUDIT_APPEND,
    AuditIntegrityError: ExitCode.AUDIT_INTEGRITY,
    BrokerUnavailableError: ExitCode.BROKER,
    NonDemoAccountError: ExitCode.ACCOUNT_MODE,
    CoverageError: ExitCode.COVERAGE,
}


@dataclass
class _State:
    debug: bool = False


_STATE = _State()

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Trading-house operator tools.")
constitution_app = typer.Typer(no_args_is_help=True, help="Risk-constitution commands.")
db_app = typer.Typer(no_args_is_help=True, help="Database commands.")
audit_app = typer.Typer(no_args_is_help=True, help="Audit-ledger commands.")
app.add_typer(constitution_app, name="constitution")
app.add_typer(db_app, name="db")
app.add_typer(audit_app, name="audit")


@app.callback()
def main(
    debug: Annotated[
        bool, typer.Option("--debug", help="Re-raise unexpected errors instead of redacting them.")
    ] = False,
) -> None:
    _STATE.debug = debug


def _emit(payload: dict[str, JsonValue], *, status: str = "ok") -> None:
    typer.echo(json.dumps({"status": status, **payload}, sort_keys=True))


def _fail(detail: str, code: ExitCode | int, **extra: str) -> typer.Exit:
    typer.echo(json.dumps({"status": "error", "detail": detail, **extra}, sort_keys=True), err=True)
    return typer.Exit(code=int(code))


def _execute(operation: Callable[[], dict[str, JsonValue]]) -> dict[str, JsonValue]:
    """Run one operation, converting every failure into a stable exit code."""

    try:
        return operation()
    except TradingHouseError as error:
        raise _fail(
            error.public_message, EXIT_CODES.get(type(error), ExitCode.CONFIGURATION)
        ) from None
    except ValidationError:
        raise _fail(ConfigurationError.public_message, ExitCode.CONFIGURATION) from None
    except Exception:
        if _STATE.debug:
            raise
        raise _fail("unexpected failure", 1, correlation_id=str(uuid4())) from None


def _run(operation: Callable[[], dict[str, JsonValue]]) -> None:
    _emit(_execute(operation))


def _settings() -> RuntimeSettings:
    return RuntimeSettings()


def _atomic_write(destination: Path, contents: bytes) -> None:
    """Publish bytes through a same-directory temporary file and one rename."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(dir=destination.parent, prefix=".sig-", suffix=".tmp")
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


@constitution_app.command("verify")
def constitution_verify(
    constitution: Annotated[Path, typer.Option("--constitution")] = DEFAULT_CONSTITUTION,
    signature: Annotated[Path, typer.Option("--signature")] = DEFAULT_SIGNATURE,
    public_key: Annotated[Path, typer.Option("--public-key")] = DEFAULT_PUBLIC_KEY,
) -> None:
    """Verify the constitution signature and report its identity."""

    def operation() -> dict[str, JsonValue]:
        loaded = load_constitution(constitution, signature, public_key)
        return {
            "constitution_version": loaded.constitution.version,
            "constitution_sha256": loaded.constitution_sha256,
            "public_key_fingerprint": loaded.public_key_fingerprint,
        }

    _run(operation)


@constitution_app.command("binding")
def constitution_binding(
    binding: Annotated[Path, typer.Option("--binding")] = DEFAULT_BINDING,
    signature: Annotated[Path, typer.Option("--signature")] = DEFAULT_BINDING_SIGNATURE,
    public_key: Annotated[Path, typer.Option("--public-key")] = DEFAULT_PUBLIC_KEY,
) -> None:
    """Verify the signed venue binding and report its coverage."""

    def operation() -> dict[str, JsonValue]:
        loaded = load_venue_binding(binding, signature, public_key)
        return {
            "venue": loaded.venue,
            "books": cast(list[JsonValue], sorted(loaded.books)),
            "instruments": cast(list[JsonValue], sorted(loaded.instruments)),
        }

    _run(operation)


@constitution_app.command("sign")
def constitution_sign(
    constitution: Annotated[Path, typer.Option("--constitution")],
    private_key: Annotated[Path, typer.Option("--private-key")],
    signature_output: Annotated[Path, typer.Option("--signature-output")],
    force: Annotated[bool, typer.Option("--force", help="Replace an existing signature.")] = False,
) -> None:
    """Sign exact constitution bytes offline with an Ed25519 private key."""

    def operation() -> dict[str, JsonValue]:
        if signature_output.exists() and not force:
            raise ConfigurationError()
        try:
            key = load_private_key(private_key.read_bytes())
        except OSError as error:
            raise SignatureVerificationError() from error
        try:
            signature = sign_bytes(key, constitution.read_bytes())
        finally:
            del key
        encoded = base64.b64encode(signature) + b"\n"
        _atomic_write(signature_output, encoded)
        return {
            "signature_path": str(signature_output),
            "signature_sha256": hashlib.sha256(encoded).hexdigest(),
        }

    _run(operation)


@db_app.command("check")
def db_check(
    alembic_config: Annotated[Path, typer.Option("--alembic-config")] = DEFAULT_ALEMBIC_CONFIG,
) -> None:
    """Confirm the database is reachable and at the expected revision."""

    def operation() -> dict[str, JsonValue]:
        settings = _settings()
        connection = open_runtime_connection(settings.database_dsn)
        try:
            assert_at_head(connection, Config(str(alembic_config)))
        finally:
            connection.close()
        return {"revision": "head"}

    _run(operation)


@audit_app.command("verify")
def audit_verify() -> None:
    """Independently verify the audit hash chain."""

    def operation() -> dict[str, JsonValue]:
        settings = _settings()
        ledger = PostgresAuditLedger(lambda: open_runtime_connection(settings.database_dsn))
        report = ledger.verify()
        return {
            "valid": report.valid,
            "checked_entries": report.checked_entries,
            "first_invalid_sequence": report.first_invalid_sequence,
            "reason": report.reason,
        }

    payload = _execute(operation)
    _emit(payload, status="ok" if payload["valid"] else "invalid")
    if not payload["valid"]:
        raise typer.Exit(code=int(ExitCode.AUDIT_INTEGRITY))


def _mt5_terminal_factory() -> Callable[[str], TerminalPort] | None:
    """Look up a MetaTrader 5 terminal constructor, or report it unavailable.

    ``MetaTrader5`` is a ``sys_platform == 'win32'`` dependency and
    ``terminal.py`` -- the one module allowed to import it -- cannot even be
    imported on Linux, where the coverage-gated CI job runs. The import stays
    inside this function, called only when the health gate actually reaches
    the venue-reconciliation step, so every other platform and every other
    command stays unaffected.
    """

    try:
        from trading_house.brokers.mt5.terminal import Mt5Terminal
    except ImportError:
        return None
    return Mt5Terminal


def _empty_reconciliation() -> Mapping[BookId, ReconciliationReport]:
    return {}


def _book_reconciler(
    venue_binding: Path,
    venue_binding_signature: Path,
    venue_binding_public_key: Path,
    ledger: PostgresAuditLedger,
) -> BookReconciler:
    """Build the health gate's venue-reconciliation step.

    Reconciliation reports; it does not fail readiness (see
    ``ops.health.HealthService.run``), and that includes the step's own
    absence: an unavailable MetaTrader5 module, an absent venue binding, or a
    terminal that will not initialise all degrade to "the venue step was not
    performed" -- ``books_reconciled`` stays empty and ``open_positions``
    stays zero -- rather than raising. A live account is the one exception:
    ``NonDemoAccountError`` is never caught here, because there is no state
    in which we are connected to a live account and merely not trading yet.
    """

    def _record_skip(reason: str) -> None:
        """Put the reason the venue step was skipped into the hash chain.

        Without this the gate reports ready with an empty ``books_reconciled``
        and nothing distinguishes "no positions" from "never reached the
        broker" -- which is exactly how a binding naming a symbol the broker
        does not have goes unnoticed run after run.
        """

        ledger.append(
            build_audit_event(
                "venue.skipped",
                SystemClock().now(),
                {"venue": "mt5", "reason": reason},
                source_component="brokers.mt5",
            )
        )

    if not venue_binding.exists():
        return _empty_reconciliation

    def reconcile_books() -> Mapping[BookId, ReconciliationReport]:
        terminal_factory = _mt5_terminal_factory()
        if terminal_factory is None:
            _record_skip("metatrader5 unavailable on this platform")
            return {}

        binding = load_venue_binding(
            venue_binding, venue_binding_signature, venue_binding_public_key
        )
        clock = SystemClock()

        def on_gateway_event(event_type: str, payload: Mapping[str, JsonValue]) -> None:
            ledger.append(
                build_audit_event(
                    event_type, clock.now(), dict(payload), source_component="brokers.mt5"
                )
            )

        # The server-clock probe reads a tick from one symbol. Take it from
        # the signed binding rather than assuming a plain "EURUSD" exists --
        # brokers that suffix their symbols (EURUSD.m) have no such symbol,
        # and the probe would fail the whole venue step on them.
        probe_symbol = next(iter(binding.instruments.values())).server_symbol
        gateway = Mt5Gateway(terminal_factory(probe_symbol), clock=clock, on_event=on_gateway_event)
        try:
            gateway.start()
        except BrokerUnavailableError:
            _record_skip("terminal unavailable, or its clock could not be read")
            return {}
        try:
            adapter = Mt5BrokerAdapter(gateway, binding, clock=clock)
            reports = {book: adapter.reconcile(book) for book in binding.books}
            gateway.mark_reconciled()
            return reports
        finally:
            gateway.stop()

    return reconcile_books


@app.command("health")
def health(
    alembic_config: Annotated[Path, typer.Option("--alembic-config")] = DEFAULT_ALEMBIC_CONFIG,
    venue_binding: Annotated[Path, typer.Option("--venue-binding")] = DEFAULT_BINDING,
    venue_binding_signature: Annotated[
        Path, typer.Option("--venue-binding-signature")
    ] = DEFAULT_BINDING_SIGNATURE,
    venue_binding_public_key: Annotated[
        Path, typer.Option("--venue-binding-public-key")
    ] = DEFAULT_PUBLIC_KEY,
) -> None:
    """Run the full readiness gate and report ready or a typed failure."""

    def operation() -> dict[str, JsonValue]:
        settings = _settings()
        config = Config(str(alembic_config))

        def connect() -> Any:
            return open_runtime_connection(settings.database_dsn)

        def revision(connection: Any) -> None:
            assert_at_head(connection, config)

        ledger = PostgresAuditLedger(connect)
        report = HealthService(
            load_constitution=lambda: load_constitution(
                settings.constitution_path,
                settings.constitution_signature_path,
                settings.constitution_public_key_path,
            ),
            open_connection=connect,
            assert_revision=revision,
            ledger=ledger,
            reconcile_books=_book_reconciler(
                venue_binding, venue_binding_signature, venue_binding_public_key, ledger
            ),
            clock=SystemClock(),
            application_version=__version__,
        ).run()
        return {
            "ready": report.ready,
            "application_version": report.application_version,
            "constitution_version": report.constitution_version,
            "constitution_sha256": report.constitution_sha256,
            "public_key_fingerprint": report.public_key_fingerprint,
            "audit_entries_verified": report.audit_entries_verified,
            "books_reconciled": cast(list[JsonValue], sorted(report.books_reconciled)),
            "open_positions": report.open_positions,
        }

    _run(operation)
