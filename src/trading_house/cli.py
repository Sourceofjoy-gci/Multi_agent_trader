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
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
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
from trading_house.brokers.mt5.magic import derive_magic
from trading_house.constitution.binding import VenueBinding, load_venue_binding
from trading_house.constitution.loader import load_constitution
from trading_house.constitution.signing import load_private_key, sign_bytes
from trading_house.core.clock import SystemClock
from trading_house.core.errors import (
    AuditAppendError,
    AuditIntegrityError,
    BrokerError,
    BrokerUnavailableError,
    ConfigurationError,
    CoverageError,
    DatabaseUnavailableError,
    ExitCode,
    InsufficientHistoryError,
    IntentAlreadySubmittedError,
    MigrationMismatchError,
    NonDemoAccountError,
    SchemaValidationError,
    SignatureVerificationError,
    TimestampError,
    TradingHouseError,
    UnresolvedIntentsError,
)
from trading_house.core.schemas import OrderIntent, Side
from trading_house.core.values import (
    BookId,
    IntentState,
    PositiveQuantity,
    QuantityUnit,
    TimeInForce,
)
from trading_house.core.venue import DealRecord, Mt5VenueRef, Venue
from trading_house.database.connection import open_runtime_connection
from trading_house.database.migrations import assert_at_head
from trading_house.execution.ledger import PostgresIntentLedger
from trading_house.execution.manager import OrderManager
from trading_house.execution.reconciler import reconcile_all, require_clean_ledger
from trading_house.marketdata.ingest import backfill, update
from trading_house.marketdata.models import Coverage, IngestRun, Timeframe
from trading_house.marketdata.provider import HistoryProvider
from trading_house.marketdata.store import PostgresBarStore
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
    BrokerError: ExitCode.BROKER,
    NonDemoAccountError: ExitCode.ACCOUNT_MODE,
    CoverageError: ExitCode.COVERAGE,
    InsufficientHistoryError: ExitCode.INSUFFICIENT_HISTORY,
    IntentAlreadySubmittedError: ExitCode.DUPLICATE_INTENT,
    UnresolvedIntentsError: ExitCode.UNRESOLVED_INTENTS,
}


@dataclass
class _State:
    debug: bool = False


_STATE = _State()

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Trading-house operator tools.")
constitution_app = typer.Typer(no_args_is_help=True, help="Risk-constitution commands.")
db_app = typer.Typer(no_args_is_help=True, help="Database commands.")
audit_app = typer.Typer(no_args_is_help=True, help="Audit-ledger commands.")
data_app = typer.Typer(no_args_is_help=True, help="Market-data commands.")
order_app = typer.Typer(no_args_is_help=True, help="Order commands.")
app.add_typer(constitution_app, name="constitution")
app.add_typer(db_app, name="db")
app.add_typer(audit_app, name="audit")
app.add_typer(data_app, name="data")
app.add_typer(order_app, name="order")


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
    except UnresolvedIntentsError as error:
        # str(error) names the offending intent ids; the class-level
        # public_message alone does not, and an operator who cannot see
        # which intent is stuck cannot clear it.
        raise _fail(str(error), EXIT_CODES[UnresolvedIntentsError]) from None
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


_GENESIS = datetime(1970, 1, 1, tzinfo=UTC)
"""``update``'s fallback start for an instrument/timeframe with no stored
coverage yet -- the same role ``--from`` plays for an explicit ``backfill``,
just never left to a default there. A fresh key still needs to know how far
back the window is meant to reach, and going all the way back lets the
broker's own depth wall (not a guess made here) decide where it actually
starts."""


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


@contextmanager
def _history_provider(
    venue_binding: Path,
    venue_binding_signature: Path,
    venue_binding_public_key: Path,
) -> Iterator[tuple[HistoryProvider, VenueBinding, int]]:
    """Build a live ``HistoryProvider`` over one MetaTrader 5 terminal.

    Unlike ``_book_reconciler``, an absent or unreachable terminal is never
    degraded to an empty result here: backfill and update exist to fetch
    bars, so a broker that cannot be reached is ``BrokerUnavailableError``,
    a typed and already-mapped failure, rather than a silent no-op run.
    """

    terminal_factory = _mt5_terminal_factory()
    if terminal_factory is None:
        raise BrokerUnavailableError()

    binding = load_venue_binding(venue_binding, venue_binding_signature, venue_binding_public_key)
    clock = SystemClock()
    probe_symbol = next(iter(binding.instruments.values())).server_symbol
    gateway = Mt5Gateway(terminal_factory(probe_symbol), clock=clock)
    gateway.start()
    try:
        adapter = Mt5BrokerAdapter(gateway, binding, clock=clock)
        yield adapter, binding, gateway.server_utc_offset_seconds
    finally:
        gateway.stop()


def _bar_store() -> PostgresBarStore:
    settings = _settings()
    return PostgresBarStore(lambda: open_runtime_connection(settings.database_dsn))


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _ingest_run_payload(run: IngestRun) -> dict[str, JsonValue]:
    return {
        "run_id": str(run.run_id),
        "instrument_id": run.instrument_id,
        "timeframe": run.timeframe.value,
        "requested_from": _iso(run.requested_from),
        "requested_to": _iso(run.requested_to),
        "started_at": _iso(run.started_at),
        "finished_at": _iso(run.finished_at),
        "earliest_event_time": _iso(run.earliest_event_time),
        "bars_returned": run.bars_returned,
        "bars_stored": run.bars_stored,
        "bars_rejected": run.bars_rejected,
        "bars_conflicting": run.bars_conflicting,
        "expected_bars": run.expected_bars,
        "coverage_ratio": str(run.coverage_ratio),
        "outcome": run.outcome.value,
        "detail": run.detail,
    }


def _coverage_payload(coverage: Coverage) -> dict[str, JsonValue]:
    return {
        "earliest_event_time": _iso(coverage.earliest_event_time),
        "latest_event_time": _iso(coverage.latest_event_time),
        "latest_availability_time": _iso(coverage.latest_availability_time),
        "clean_bars": coverage.clean_bars,
        "defective_bars": coverage.defective_bars,
    }


@data_app.command("backfill")
def data_backfill(
    instrument: Annotated[str, typer.Option("--instrument", help="Instrument id.")],
    timeframe: Annotated[Timeframe, typer.Option("--timeframe", help="Bar timeframe.")],
    from_: Annotated[datetime, typer.Option("--from", help="Requested start (UTC).")],
    venue_binding: Annotated[Path, typer.Option("--venue-binding")] = DEFAULT_BINDING,
    venue_binding_signature: Annotated[
        Path, typer.Option("--venue-binding-signature")
    ] = DEFAULT_BINDING_SIGNATURE,
    venue_binding_public_key: Annotated[
        Path, typer.Option("--venue-binding-public-key")
    ] = DEFAULT_PUBLIC_KEY,
) -> None:
    """Backfill one instrument/timeframe pair back to ``--from``.

    ``--instrument`` and ``--timeframe`` carry no default: a backfill is a
    deliberate, long-running act, and defaulting either invites one nobody
    meant to start.
    """

    def operation() -> dict[str, JsonValue]:
        with _history_provider(
            venue_binding, venue_binding_signature, venue_binding_public_key
        ) as (provider, _binding, server_offset_seconds):
            run = backfill(
                provider,
                _bar_store(),
                SystemClock(),
                instrument_id=instrument,
                timeframe=timeframe,
                until=_as_utc(from_),
                server_offset_seconds=server_offset_seconds,
            )
        return _ingest_run_payload(run)

    _run(operation)


@data_app.command("update")
def data_update(
    venue_binding: Annotated[Path, typer.Option("--venue-binding")] = DEFAULT_BINDING,
    venue_binding_signature: Annotated[
        Path, typer.Option("--venue-binding-signature")
    ] = DEFAULT_BINDING_SIGNATURE,
    venue_binding_public_key: Annotated[
        Path, typer.Option("--venue-binding-public-key")
    ] = DEFAULT_PUBLIC_KEY,
) -> None:
    """Update every instrument in the signed binding across all six timeframes.

    History depth varies enough per timeframe that each must be fetched on
    its own -- there is no single cursor shared across timeframes.
    """

    def operation() -> dict[str, JsonValue]:
        with _history_provider(
            venue_binding, venue_binding_signature, venue_binding_public_key
        ) as (provider, binding, server_offset_seconds):
            store = _bar_store()
            clock = SystemClock()
            runs = [
                update(
                    provider,
                    store,
                    clock,
                    instrument_id=instrument_id,
                    timeframe=timeframe,
                    until=_GENESIS,
                    server_offset_seconds=server_offset_seconds,
                )
                for instrument_id in sorted(binding.instruments)
                for timeframe in Timeframe
            ]
        return {"runs": cast(list[JsonValue], [_ingest_run_payload(run) for run in runs])}

    _run(operation)


@data_app.command("coverage")
def data_coverage(
    venue_binding: Annotated[Path, typer.Option("--venue-binding")] = DEFAULT_BINDING,
    venue_binding_signature: Annotated[
        Path, typer.Option("--venue-binding-signature")
    ] = DEFAULT_BINDING_SIGNATURE,
    venue_binding_public_key: Annotated[
        Path, typer.Option("--venue-binding-public-key")
    ] = DEFAULT_PUBLIC_KEY,
) -> None:
    """Report what the store holds, per instrument and timeframe.

    Reads the store only -- never the broker -- so this never touches
    MetaTrader5.
    """

    def operation() -> dict[str, JsonValue]:
        binding = load_venue_binding(
            venue_binding, venue_binding_signature, venue_binding_public_key
        )
        store = _bar_store()
        coverage: dict[str, JsonValue] = {}
        for instrument_id in binding.instruments:
            per_timeframe: dict[str, JsonValue] = {
                timeframe.value: _coverage_payload(store.coverage(instrument_id, timeframe))
                for timeframe in Timeframe
            }
            coverage[instrument_id] = per_timeframe
        return {"coverage": coverage}

    _run(operation)


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


def _intent_ledger() -> PostgresIntentLedger:
    settings = _settings()
    return PostgresIntentLedger(lambda: open_runtime_connection(settings.database_dsn))


@contextmanager
def _order_adapter(
    venue_binding: Path,
    venue_binding_signature: Path,
    venue_binding_public_key: Path,
) -> Iterator[tuple[Mt5BrokerAdapter, VenueBinding, SystemClock]]:
    """Build a live ``Mt5BrokerAdapter`` for one order command.

    Structured like ``_history_provider``: an absent or unreachable terminal
    is ``BrokerUnavailableError``, never a silent no-op -- an order command
    that swallowed this would look like it did nothing when it actually
    never tried to reach the venue at all.
    """

    terminal_factory = _mt5_terminal_factory()
    if terminal_factory is None:
        raise BrokerUnavailableError()

    binding = load_venue_binding(venue_binding, venue_binding_signature, venue_binding_public_key)
    clock = SystemClock()
    probe_symbol = next(iter(binding.instruments.values())).server_symbol
    gateway = Mt5Gateway(terminal_factory(probe_symbol), clock=clock)
    gateway.start()
    try:
        yield Mt5BrokerAdapter(gateway, binding, clock=clock), binding, clock
    finally:
        gateway.stop()


@dataclass
class _AdapterDealSource:
    """Adapts ``Mt5BrokerAdapter`` to the reconciler's own ``DealSource``
    port: the adapter's connectivity check is named ``health()``, not
    ``terminal_healthy()``, so this is the one place that gap is bridged."""

    adapter: Mt5BrokerAdapter

    def deals_since(self, start: datetime) -> Sequence[DealRecord]:
        return self.adapter.deals_since(start)

    def terminal_healthy(self) -> bool:
        return self.adapter.health().connected


@order_app.command("submit")
def order_submit(
    intent_id: Annotated[str, typer.Option("--intent-id")],
    proposal_id: Annotated[str, typer.Option("--proposal-id")],
    strategy_id: Annotated[str, typer.Option("--strategy-id")],
    book: Annotated[str, typer.Option("--book")],
    instrument: Annotated[str, typer.Option("--instrument")],
    side: Annotated[Side, typer.Option("--side")],
    quantity: Annotated[str, typer.Option("--quantity")],
    stop_loss: Annotated[str, typer.Option("--stop-loss")],
    take_profit: Annotated[str | None, typer.Option("--take-profit")] = None,
    quantity_unit: Annotated[QuantityUnit, typer.Option("--quantity-unit")] = "lots",
    time_in_force: Annotated[TimeInForce, typer.Option("--time-in-force")] = TimeInForce.GTC,
    max_slippage_bps: Annotated[str, typer.Option("--max-slippage-bps")] = "5",
    venue_binding: Annotated[Path, typer.Option("--venue-binding")] = DEFAULT_BINDING,
    venue_binding_signature: Annotated[
        Path, typer.Option("--venue-binding-signature")
    ] = DEFAULT_BINDING_SIGNATURE,
    venue_binding_public_key: Annotated[
        Path, typer.Option("--venue-binding-public-key")
    ] = DEFAULT_PUBLIC_KEY,
) -> None:
    """Submit one order intent to the venue.

    Refuses before touching the venue at all while any earlier intent is
    still unresolved (I-20) -- ``require_clean_ledger`` runs first, and the
    intent is built and sent only once it has passed.
    """

    def operation() -> dict[str, JsonValue]:
        ledger = _intent_ledger()
        require_clean_ledger(ledger)

        with _order_adapter(venue_binding, venue_binding_signature, venue_binding_public_key) as (
            adapter,
            binding,
            clock,
        ):
            book_binding = binding.books.get(book)
            instrument_binding = binding.instruments.get(instrument)
            if book_binding is None or instrument_binding is None:
                raise ConfigurationError()
            venue_ref = Mt5VenueRef(
                venue=Venue.MT5,
                magic=derive_magic(intent_id, book_binding.magic_range),
                server_symbol=instrument_binding.server_symbol,
            )
            intent = OrderIntent(
                intent_id=intent_id,
                proposal_id=proposal_id,
                book=book,
                instrument_id=instrument,
                side=side,
                quantity=PositiveQuantity(amount=Decimal(quantity), unit=quantity_unit),
                stop_loss=Decimal(stop_loss),
                take_profit=Decimal(take_profit) if take_profit is not None else None,
                time_in_force=time_in_force,
                max_slippage_bps=Decimal(max_slippage_bps),
                state=IntentState.SUBMITTING,
                t_submit_utc=clock.now(),
                venue_ref=venue_ref,
            )
            state = OrderManager(ledger, adapter, clock).submit(intent, strategy_id)
        return {"intent_id": intent_id, "state": state.value}

    _run(operation)


@order_app.command("reconcile")
def order_reconcile(
    venue_binding: Annotated[Path, typer.Option("--venue-binding")] = DEFAULT_BINDING,
    venue_binding_signature: Annotated[
        Path, typer.Option("--venue-binding-signature")
    ] = DEFAULT_BINDING_SIGNATURE,
    venue_binding_public_key: Annotated[
        Path, typer.Option("--venue-binding-public-key")
    ] = DEFAULT_PUBLIC_KEY,
) -> None:
    """Resolve every unresolved intent against the venue's own deal history.

    The one order command that does NOT run ``require_clean_ledger`` first:
    this is the command that clears the condition, and gating it would
    deadlock the system against itself.
    """

    def operation() -> dict[str, JsonValue]:
        ledger = _intent_ledger()
        with _order_adapter(venue_binding, venue_binding_signature, venue_binding_public_key) as (
            adapter,
            _binding,
            clock,
        ):
            results = reconcile_all(ledger, _AdapterDealSource(adapter), clock)
        return {
            "results": cast(
                dict[str, JsonValue],
                {intent_id: verdict.value for intent_id, verdict in results.items()},
            )
        }

    _run(operation)


@order_app.command("status")
def order_status(
    intent_id: Annotated[str, typer.Option("--intent-id")],
) -> None:
    """Report one intent's full ledger history.

    Gated like every other order-placing command: an operator cannot trust
    a status report while an earlier intent is still unresolved.
    """

    def operation() -> dict[str, JsonValue]:
        ledger = _intent_ledger()
        require_clean_ledger(ledger)
        events = ledger.events_for(intent_id)
        return {
            "intent_id": intent_id,
            "current_state": events[-1].state.value if events else None,
            "events": cast(
                list[JsonValue],
                [
                    {
                        "seq": event.seq,
                        "state": event.state.value,
                        "event_time": event.event_time.isoformat(),
                    }
                    for event in events
                ],
            ),
        }

    _run(operation)
