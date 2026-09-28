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
import signal
import tempfile
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, cast
from uuid import uuid4

import typer
from alembic.config import Config
from pydantic import JsonValue, ValidationError
from typer._click.exceptions import UsageError
from typer.core import TyperCommand

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
    ConcurrentSubmissionError,
    ConfigurationError,
    CoverageError,
    DatabaseUnavailableError,
    EquityEvidenceError,
    EvidenceIntegrityError,
    ExitCode,
    InsufficientHistoryError,
    IntentAlreadySubmittedError,
    MigrationMismatchError,
    NonDemoAccountError,
    SchemaValidationError,
    SignatureVerificationError,
    TimestampError,
    TradingHouseError,
    TrialLedgerAppendError,
    TrialLedgerIntegrityError,
    UnresolvedIntentsError,
)
from trading_house.core.exits import (
    ChandelierPolicy,
    ExitPolicy,
    FixedTargetPolicy,
    NoExitPolicy,
)
from trading_house.core.instruments import InstrumentContract
from trading_house.core.schemas import (
    RISK_DECISION_ADAPTER,
    OrderIntent,
    RejectedRiskDecision,
    Side,
)
from trading_house.core.values import (
    BookId,
    CanonicalModel,
    IntentState,
    TimeInForce,
)
from trading_house.core.venue import DealRecord, Mt5VenueRef, PositionRecord, Venue
from trading_house.database.connection import open_runtime_connection
from trading_house.database.migrations import assert_at_head
from trading_house.execution.ledger import (
    ConnectionFactory,
    PostgresIntentLedger,
    submission_lock,
)
from trading_house.execution.loop import PositionGuard
from trading_house.execution.manager import OrderManager
from trading_house.execution.positions import PostgresPositionStore
from trading_house.execution.reconciler import reconcile_all, require_clean_ledger
from trading_house.marketdata.ingest import backfill, update
from trading_house.marketdata.models import Coverage, IngestRun, Timeframe
from trading_house.marketdata.provider import HistoryProvider
from trading_house.marketdata.store import PostgresBarStore
from trading_house.ops.backtest import build_backtester, build_strategy, mark_to_market_bundle
from trading_house.ops.guard import LedgerEscalator, Mt5ProtectionPort
from trading_house.ops.health import BookReconciler, HealthService, build_audit_event
from trading_house.ops.ledger import (
    build_evidence_store,
    evidence_sealed_event,
    execution_started_event,
    research_ledger_dsn,
    result_recorded_event,
)
from trading_house.research.backtest.costs import CostModel
from trading_house.research.backtest.engine import BacktestRefused, BacktestRequest
from trading_house.research.canonical import canonical_sha256
from trading_house.research.evidence import EvidenceBundle, EvidenceStore
from trading_house.research.ledger_store import PostgresTrialLedger
from trading_house.research.legacy_import import import_phase7_artifact
from trading_house.research.trial_ledger import (
    EvidenceSealedPayload,
    LedgerRecord,
    LegacyImportedPayload,
    TrialProtocol,
)
from trading_house.settings import RuntimeSettings

DEFAULT_CONSTITUTION = Path("config/risk_constitution.yaml")
DEFAULT_SIGNATURE = Path("config/risk_constitution.yaml.sig")
DEFAULT_PUBLIC_KEY = Path("config/risk_constitution.public.pem")
DEFAULT_BINDING = Path("config/venue_binding.mt5.yaml")
DEFAULT_BINDING_SIGNATURE = Path("config/venue_binding.mt5.yaml.sig")
DEFAULT_ALEMBIC_CONFIG = Path("alembic.ini")
_BACKTEST_INSTRUMENT = "fx.eurusd"
_BACKTEST_TIMEFRAME = Timeframe.M15


class ExitPolicyName(StrEnum):
    NONE = "none"
    FIXED_TARGET = "fixed_target"
    CHANDELIER = "chandelier"


_EXIT_POLICIES: dict[ExitPolicyName, ExitPolicy] = {
    ExitPolicyName.NONE: NoExitPolicy(kind="none"),
    ExitPolicyName.FIXED_TARGET: FixedTargetPolicy(kind="fixed_target", r_multiple=Decimal("1.0")),
    ExitPolicyName.CHANDELIER: ChandelierPolicy(
        kind="chandelier", atr_multiple=Decimal("3.0"), min_step_points=Decimal(10)
    ),
}


def _exit_policy(arm: ExitPolicyName) -> ExitPolicy:
    return _EXIT_POLICIES[arm]


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
    ConcurrentSubmissionError: ExitCode.CONCURRENT_SUBMISSION,
    TrialLedgerAppendError: ExitCode.TRIAL_LEDGER_APPEND,
    TrialLedgerIntegrityError: ExitCode.TRIAL_LEDGER_INTEGRITY,
    EvidenceIntegrityError: ExitCode.EVIDENCE_INTEGRITY,
    EquityEvidenceError: ExitCode.EQUITY_EVIDENCE,
    # One code for all five refusal kinds. They have different remedies --
    # backfill, repair the bars, fix the arm or strategy, widen the horizon --
    # but they are all "the run you asked for cannot be simulated honestly", and
    # the kind itself travels as the ``refusal`` key on the error payload,
    # which is what a script actually branches on.
    BacktestRefused: ExitCode.CONFIGURATION,
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
guard_app = typer.Typer(no_args_is_help=True, help="Position-guard commands.")
backtest_app = typer.Typer(no_args_is_help=True, help="Backtest commands.")
research_app = typer.Typer(no_args_is_help=True, help="Research commands.")
trial_app = typer.Typer(no_args_is_help=True, help="Trial-ledger commands.")
app.add_typer(constitution_app, name="constitution")
app.add_typer(db_app, name="db")
app.add_typer(audit_app, name="audit")
app.add_typer(data_app, name="data")
app.add_typer(order_app, name="order")
app.add_typer(guard_app, name="guard")
app.add_typer(backtest_app, name="backtest")
app.add_typer(research_app, name="research")
research_app.add_typer(trial_app, name="trial")


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


class _BacktestCommand(TyperCommand):
    def make_context(
        self,
        info_name: str | None,
        args: list[str],
        parent: Any | None = None,
        **extra: Any,
    ) -> Any:
        try:
            return super().make_context(info_name, args, parent=parent, **extra)
        except UsageError:
            raise _fail(ConfigurationError.public_message, EXIT_CODES[ConfigurationError]) from None


def _execute(operation: Callable[[], dict[str, JsonValue]]) -> dict[str, JsonValue]:
    """Run one operation, converting every failure into a stable exit code."""

    try:
        return operation()
    except BacktestRefused as error:
        # A refusal's kind IS the diagnosis, and the class-level
        # public_message alone does not carry it: an operator told only
        # "backtest refused" cannot tell a defective bar from a range the
        # store does not cover. The kind is a closed enum, so naming it adds
        # no free text -- nothing from a DSN, a path or a broker message can
        # ride out on this key.
        raise _fail(
            BacktestRefused.public_message,
            EXIT_CODES[BacktestRefused],
            refusal=error.kind.value,
        ) from None
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


_DECIMAL_MAGNITUDE_CEILING: Decimal = Decimal("1e30")
"""The largest magnitude any ``--`` money or multiplier option may carry.

Not a modelled limit -- a sanity bound. No firm equity, commission, swap rate
or stress multiplier in this system is within many orders of magnitude of
1e30: the largest of them is an account balance, and 1e30 is past the notional
value of every asset there is. Anything beyond it is a typo or a probe, and
without this bound it survives every guard below and reaches the risk engine's
sizing arithmetic as a ``decimal.Overflow`` -- an ``ArithmeticError``, so the
catch-all answers a mistyped number with a correlation id.
"""


def _decimal(value: str) -> Decimal:
    """Money off the command line, typed at the boundary it entered.

    ``Decimal("abc")`` raises ``InvalidOperation``, which is an
    ``ArithmeticError`` and not a ``ValueError``, so it slips past every
    handler written for the latter and lands in ``_execute``'s catch-all --
    where a typo becomes "unexpected failure" and a correlation id.

    Rejects a non-finite result for the same reason, one step later.
    ``Decimal("NaN")`` and ``Decimal("Infinity")`` both CONSTRUCT cleanly, so
    catching the construction is not enough: ``NaN <= 0`` raises
    ``InvalidOperation`` from inside a later comparison -- again an
    ``ArithmeticError``, again the catch-all -- and ``Infinity <= 0`` is simply
    ``False``, so an infinite equity is accepted as a positive one and the run
    proceeds. Every caller here wants money or a multiplier and none of them
    legitimately wants either value, so the guard lives once in the shared
    helper rather than at each call site.

    Rejects a value beyond ``_DECIMAL_MAGNITUDE_CEILING`` for the third time
    in the same shape. ``Decimal("1e1000000")`` is finite, clears both guards
    above and clears ``BacktestRequest.__post_init__``'s positivity check --
    and then raises ``decimal.Overflow``, an ``ArithmeticError``, from inside
    the risk engine's sizing arithmetic, landing in the catch-all with a
    correlation id. A bound here turns that back into the typed input error it
    is.
    """

    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise ConfigurationError() from error
    if not parsed.is_finite():
        raise ConfigurationError()
    # ``copy_abs()``, not ``abs()``. ``abs`` is a context operation and
    # ``abs(Decimal("1e1000000"))`` raises the very ``decimal.Overflow`` this
    # line exists to prevent -- measured, not assumed: written with ``abs`` the
    # command exited 1 with a correlation id instead of 2. ``copy_abs`` is
    # context-free and cannot raise.
    if parsed.copy_abs() > _DECIMAL_MAGNITUDE_CEILING:
        raise ConfigurationError()
    return parsed


def _unit_interval_decimal(value: str) -> Decimal:
    parsed = _decimal(value)
    if parsed < 0 or parsed > 1:
        raise ConfigurationError()
    return parsed


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


def _connection_factory() -> ConnectionFactory:
    """One factory, shared by the ledger and the submission lock -- they must
    reach the same database, and the lock is worthless if they do not."""

    settings = _settings()
    return lambda: open_runtime_connection(settings.database_dsn)


def _intent_ledger() -> PostgresIntentLedger:
    return PostgresIntentLedger(_connection_factory())


@contextmanager
def _order_adapter(
    venue_binding: Path,
    venue_binding_signature: Path,
    venue_binding_public_key: Path,
) -> Iterator[tuple[Mt5BrokerAdapter, Mt5Gateway, VenueBinding, SystemClock]]:
    """Build a live ``Mt5BrokerAdapter`` for one order command, or for
    ``guard run``, which shares it as its own composition root's adapter.

    Structured like ``_history_provider``: an absent or unreachable terminal
    is ``BrokerUnavailableError``, never a silent no-op -- a command that
    swallowed this would look like it did nothing when it actually never
    tried to reach the venue at all.
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
        yield Mt5BrokerAdapter(gateway, binding, clock=clock), gateway, binding, clock
    finally:
        gateway.stop()


@dataclass
class _AdapterDealSource:
    """Adapts ``Mt5BrokerAdapter`` to the reconciler's own ``DealSource``
    port: the adapter's connectivity check is named ``health()``, not
    ``terminal_healthy()``, so this is the one place that gap is bridged."""

    adapter: Mt5BrokerAdapter

    def deals_since(self, start: datetime) -> Sequence[DealRecord] | None:
        return self.adapter.deals_since(start)

    def positions_now(self) -> Sequence[PositionRecord] | None:
        return self.adapter.positions_now()

    def terminal_healthy(self) -> bool:
        return self.adapter.health().connected


@order_app.command("submit")
def order_submit(
    intent_id: Annotated[str, typer.Option("--intent-id")],
    strategy_id: Annotated[str, typer.Option("--strategy-id")],
    book: Annotated[str, typer.Option("--book")],
    instrument: Annotated[str, typer.Option("--instrument")],
    side: Annotated[Side, typer.Option("--side")],
    decision: Annotated[Path, typer.Option("--decision")],
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
    """Turn one approved risk decision into a position.

    Size and stop come from the decision file, never from the command line.
    Phase 3 produces a decision -- a lot size and a protective stop, both of
    them the output of every risk gate including
    ``max_spread_fraction_of_stop``; this command turns *that* into a
    position. A ``--quantity``/``--stop-loss`` pair would be an order nothing
    had ever risked, constructible by anyone who can reach the CLI.

    Two guards run before anything is sent: the submission lock, so two
    invocations serialise instead of both reading a clean ledger, and the
    gate, which reconciles every unresolved intent and refuses if any
    survives that (I-20).
    """

    def operation() -> dict[str, JsonValue]:
        # strict=False only here: JSON has no Decimal, so a serialised
        # decision necessarily carries its money as strings. Every constraint
        # on the model -- positive quantity, positive risk, the REJECTED
        # variant pinned to zero -- still applies.
        approved = RISK_DECISION_ADAPTER.validate_python(
            json.loads(decision.read_text(encoding="utf-8")), strict=False
        )
        if isinstance(approved, RejectedRiskDecision):
            # A rejected decision has no approved quantity and no stop. There
            # is nothing here to submit, and inventing either would be the
            # unrisked order this command exists to make impossible.
            raise ConfigurationError()

        factory = _connection_factory()
        ledger = PostgresIntentLedger(factory)
        with (
            submission_lock(factory),
            _order_adapter(venue_binding, venue_binding_signature, venue_binding_public_key) as (
                adapter,
                gateway,
                binding,
                clock,
            ),
        ):
            require_clean_ledger(ledger, _AdapterDealSource(adapter), clock, gateway.mark_stale)
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
                proposal_id=approved.proposal_id,
                book=book,
                instrument_id=instrument,
                side=side,
                quantity=approved.approved_quantity,
                stop_loss=approved.stop_loss_price,
                take_profit=approved.take_profit_price,
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
    """Resolve every unresolved intent against the venue's own deal history
    and open positions.

    Ungated on purpose: this is the command that clears the condition the
    gate refuses on, and gating it would deadlock the system against itself.
    """

    def operation() -> dict[str, JsonValue]:
        ledger = _intent_ledger()
        with _order_adapter(venue_binding, venue_binding_signature, venue_binding_public_key) as (
            adapter,
            gateway,
            _binding,
            clock,
        ):
            results = reconcile_all(ledger, _AdapterDealSource(adapter), clock, gateway.mark_stale)
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

    Deliberately ungated. Spec 6.1 gates order-*placing* commands; gating a
    read-only diagnostic would make the one command that shows why an intent
    is stuck refuse exactly when an intent is stuck.
    """

    def operation() -> dict[str, JsonValue]:
        ledger = _intent_ledger()
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


def _stop_event() -> threading.Event:
    """The daemon's shutdown flag.

    A function rather than a literal so a test can hand ``run`` an event that
    sets itself after the first cycle. The alternative -- a ``--cycles`` cap
    on the command -- would be a second loop nothing in production ever
    takes, running outside the error containment ``PositionGuard.run`` owns.
    """

    return threading.Event()


@guard_app.command("run")
def guard_run(
    interval_seconds: Annotated[float, typer.Option("--interval-seconds")] = 1.0,
    venue_binding: Annotated[Path, typer.Option("--venue-binding")] = DEFAULT_BINDING,
    venue_binding_signature: Annotated[
        Path, typer.Option("--venue-binding-signature")
    ] = DEFAULT_BINDING_SIGNATURE,
    venue_binding_public_key: Annotated[
        Path, typer.Option("--venue-binding-public-key")
    ] = DEFAULT_PUBLIC_KEY,
) -> None:
    """Keep every open position carrying the stop the ledger says it carries.

    Runs until interrupted. SIGINT sets the stop event rather than killing
    the process, so the daemon leaves a recorded shutdown behind instead of a
    gap nobody can date.

    Deliberately NOT gated by ``require_clean_ledger`` (D-6). Running the gate
    everywhere is the plausible-looking mistake: this command opens nothing,
    and a guard that stopped protecting live positions because an unrelated
    intent was stuck would abandon money at the worst possible moment.
    """

    def operation() -> dict[str, JsonValue]:
        stop = _stop_event()
        factory = _connection_factory()
        with _order_adapter(venue_binding, venue_binding_signature, venue_binding_public_key) as (
            adapter,
            gateway,
            binding,
            clock,
        ):
            # The stops level is a property of the contract, so it is read per
            # instrument from the venue itself. A symbol whose contract can't
            # be read is skipped rather than aborting the whole daemon
            # (Finding 8): the alternative -- letting describe_instrument's
            # ConfigurationError propagate -- would mean one instrument the
            # terminal does not know leaves EVERY position unguarded, not
            # just that symbol's, which is the worse I-21 hazard. A skipped
            # symbol falls back to loop.py's documented floor of zero for an
            # unconfigured symbol (it simply does not bind); it is never
            # silent -- see skipped_instruments in this command's output.
            min_stop_distances: dict[str, Decimal] = {}
            skipped_instruments: list[JsonValue] = []
            for instrument_id, bound in binding.instruments.items():
                try:
                    min_stop_distances[bound.server_symbol] = adapter.describe_instrument(
                        instrument_id
                    ).min_stop_distance
                except TradingHouseError:
                    skipped_instruments.append(bound.server_symbol)

            guard = PositionGuard(
                store=PostgresPositionStore(factory),
                venue=Mt5ProtectionPort(adapter, binding),
                escalator=LedgerEscalator(PostgresAuditLedger(factory), gateway.mark_stale, clock),
                clock=clock,
                owned_magic_ranges=[book.magic_range for book in binding.books.values()],
                min_stop_distances=min_stop_distances,
                # Empty on purpose. The default distance is the book's signed
                # k_sigma against the instrument's CURRENT ATR, and nothing
                # feeds an ATR to this daemon; one computed at startup and
                # reused for days would be as invented as another symbol's.
                # So an orphan with no broker stop escalates instead of being
                # adopted at a fabricated distance (D-5), which is the outcome
                # this system prefers. Fill this in when the risk engine's
                # volatility read reaches the guard.
                default_stop_distances={},
            )
            previous = signal.signal(signal.SIGINT, lambda *_: stop.set())
            try:
                guard.run(stop, interval_seconds)
            finally:
                signal.signal(signal.SIGINT, previous)
        return {"stopped": True, "skipped_instruments": skipped_instruments}

    _run(operation)


@guard_app.command("status")
def guard_status() -> None:
    """Report what the guard believes about every position it has a record
    of, flagging any that escalated.

    Reads the position store and nothing else: no terminal, no gate. This is
    the command an operator reaches for when the guard has escalated, and an
    escalation is exactly when the broker may be the thing that is broken.
    """

    def operation() -> dict[str, JsonValue]:
        rows = PostgresPositionStore(_connection_factory()).open_positions()
        positions: list[JsonValue] = [
            {
                "position_ticket": int(row["position_ticket"]),
                "lifecycle": str(row["lifecycle"]),
                "stop_loss": None if row.get("stop_loss") is None else str(row["stop_loss"]),
                "escalated": row.get("escalated") == "true",
                "escalation_reason": (
                    None if row.get("escalation_reason") is None else str(row["escalation_reason"])
                ),
            }
            for row in sorted(rows, key=lambda row: int(row["position_ticket"]))
        ]
        escalated = sum(1 for row in rows if row.get("escalated") == "true")
        return {"open_positions": len(positions), "escalated": escalated, "positions": positions}

    _run(operation)


@backtest_app.command("run", cls=_BacktestCommand)
def backtest_run(
    strategy: Annotated[str, typer.Option("--strategy", help="Registered strategy id.")],
    exit_policy: Annotated[ExitPolicyName, typer.Option("--exit-policy")],
    start: Annotated[datetime, typer.Option("--start", help="First bar open (UTC).")],
    end: Annotated[datetime, typer.Option("--end", help="Last bar open, inclusive (UTC).")],
    firm_equity: Annotated[str, typer.Option("--firm-equity")],
    contract: Annotated[Path, typer.Option("--contract", help="Instrument contract JSON.")],
    atr_period: Annotated[int, typer.Option("--atr-period", min=1)],
    spread_window: Annotated[int, typer.Option("--spread-window", min=1)],
    commission_per_lot_per_side: Annotated[str, typer.Option("--commission-per-lot-per-side")],
    slippage_points_per_side: Annotated[str, typer.Option("--slippage-points-per-side")],
    swap_long_points_per_day: Annotated[str, typer.Option("--swap-long-points-per-day")],
    swap_short_points_per_day: Annotated[str, typer.Option("--swap-short-points-per-day")],
    triple_swap_weekday: Annotated[int, typer.Option("--triple-swap-weekday")],
    defective_bar_tolerance: Annotated[str, typer.Option("--defective-bar-tolerance")] = "0",
    stress_multiplier: Annotated[str, typer.Option("--stress-multiplier")] = "1",
    mark_to_market: Annotated[
        bool,
        typer.Option(
            "--mark-to-market",
            help=(
                "Emit a sealable mark-to-market evidence bundle instead of the bare "
                "result artifact."
            ),
        ),
    ] = False,
    trial_id: Annotated[
        str | None,
        typer.Option(
            "--trial-id",
            help="Declared candidate this run belongs to. Required with --mark-to-market.",
        ),
    ] = None,
    attempt_id: Annotated[
        str | None,
        typer.Option(
            "--attempt-id",
            help="Started attempt this run belongs to. Required with --mark-to-market.",
        ),
    ] = None,
    spec_sha256: Annotated[
        str | None,
        typer.Option(
            "--spec-sha256",
            help="Preregistered specification digest. Required with --mark-to-market.",
        ),
    ] = None,
    agent_run_id: Annotated[
        str | None,
        typer.Option(
            "--agent-run-id",
            help="Agent run that produced the candidate. Required with --mark-to-market.",
        ),
    ] = None,
    occurred_at: Annotated[
        datetime | None,
        typer.Option("--occurred-at", help="The run's own UTC timestamp, as declared provenance."),
    ] = None,
    registered_at: Annotated[
        datetime | None,
        typer.Option(
            "--registered-at", help="When the attempt was registered, as declared provenance."
        ),
    ] = None,
) -> None:
    """Replay one registered strategy over stored EURUSD M15 bars.

    Every cost is a required option. ``CostModel`` defaults exactly one field --
    ``stress_multiplier``, the 1.5x-2x sensitivity knob section 12 asks for --
    and defaults no cost at all, because the classic flattering backtest is one
    that silently assumed zero commission (D-5). This command keeps that
    property: omit a cost and the command refuses rather than charging nothing.

    ``--exit-policy`` is required because an unchosen arm is not a result. The
    three arm parameters are fixed in code and absent from the command line so
    one invocation can add trials only by a code change that says so.

    ``--defective-bar-tolerance`` is a decimal fraction in ``[0, 1]``. Zero
    preserves strict refusal; a real-data run may state its allowance exactly.

    ``--mark-to-market`` emits the evidence bundle ``research trial record``
    seals, in place of the bare result. The six options beside it are what name
    the attempt a bundle belongs to, and all six are required with the flag: a
    bundle that cannot be traced to one declared candidate is not evidence of
    anything in particular. The pair is all-or-nothing in both directions -- all
    six with the flag, none of them without it -- so identity typed without the
    flag is refused rather than silently dropped. The declared timestamps are
    provenance rather than evidence of order -- the ledger's own ``recorded_at``
    is the only registration-order authority -- for the reason ``trial start``
    gives.
    """

    def operation() -> dict[str, JsonValue]:
        identity = (trial_id, attempt_id, spec_sha256, agent_run_id, occurred_at, registered_at)
        supplied = tuple(value is not None for value in identity)
        if (mark_to_market and not all(supplied)) or (not mark_to_market and any(supplied)):
            # The rule, and the whole rule: the flag and the six options are
            # all-or-nothing in both directions. With the flag every one of them
            # is required, because a bundle that cannot name its attempt is not
            # evidence of anything. Without it none may be given, because a
            # partial set is otherwise accepted and dropped -- an operator who
            # typed ``--trial-id`` and forgot ``--mark-to-market`` is told
            # nothing, and the Phase 7 artifact they get carries no identity at
            # all. Configuration rather than an equity failure either way: an
            # operator who left a flag off has made a mistake, not produced
            # evidence that cannot be trusted.
            raise ConfigurationError()
        policy = _exit_policy(exit_policy)
        settings = _settings()
        cost_model = CostModel(
            commission_per_lot_per_side=_decimal(commission_per_lot_per_side),
            slippage_points_per_side=_decimal(slippage_points_per_side),
            swap_long_points_per_day=_decimal(swap_long_points_per_day),
            swap_short_points_per_day=_decimal(swap_short_points_per_day),
            triple_swap_weekday=triple_swap_weekday,
            stress_multiplier=_decimal(stress_multiplier),
        )
        try:
            instrument_contract = InstrumentContract.model_validate(
                json.loads(contract.read_text(encoding="utf-8")), strict=False
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            # Typed here rather than left to the catch-all: a contract file the
            # operator cannot read, cannot decode, or mistyped is input, not an
            # internal failure, and the catch-all would answer it with a
            # correlation id. ``--contract`` has no producer anywhere in this
            # repo -- every operator hand-writes that file -- so a trailing
            # comma in it is the likeliest mistake this command sees, and
            # ``json.JSONDecodeError`` is a ``ValueError``, not an ``OSError``.
            raise ConfigurationError() from error
        if instrument_contract.instrument_id != _BACKTEST_INSTRUMENT:
            raise ConfigurationError()
        try:
            # BacktestRequest validates firm_equity in __post_init__ and raises
            # a bare ValueError, while a bad range raises BacktestRefused from
            # run() further down. Two shapes out of one boundary: wrapping only
            # the second would let a mistyped equity escape as "unexpected
            # failure".
            request = BacktestRequest(
                strategy=build_strategy(strategy, exit_policy=policy),
                instrument_id=_BACKTEST_INSTRUMENT,
                timeframe=_BACKTEST_TIMEFRAME,
                start=_as_utc(start),
                end=_as_utc(end),
                firm_equity=_decimal(firm_equity),
                cost_model=cost_model,
                atr_period=atr_period,
                spread_window=spread_window,
                defective_bar_tolerance=_unit_interval_decimal(defective_bar_tolerance),
            )
        except ValueError as error:
            raise ConfigurationError() from error
        loaded_constitution = load_constitution(
            settings.constitution_path,
            settings.constitution_signature_path,
            settings.constitution_public_key_path,
        )
        outcome = build_backtester(
            bars=_bar_store(),
            contract=instrument_contract,
            constitution=loaded_constitution,
        ).run(request)
        if not mark_to_market:
            # The result is re-parsed rather than embedded as a string so the whole
            # payload is one key-sorted JSON document, like every other command's.
            # The digest is still taken over the model's own declaration-ordered
            # serialisation, which is what Phase 8 will hash.
            return {
                "result": cast(JsonValue, json.loads(outcome.result.model_dump_json())),
                "digest": outcome.result.digest(),
                # Section 8.1's free-margin headroom gate is switched off in a
                # replay (``NeverBindingMargin``), and a reader of the JSON --
                # Phase 8's trial ledger included -- cannot see that from the
                # result alone. ``result.py`` is frozen, so the disclosure rides on
                # the payload beside the digest rather than inside the model.
                "margin_modelled": False,
            }
        bundle = mark_to_market_bundle(
            outcome,
            trial_id=cast(str, trial_id),
            attempt_id=cast(str, attempt_id),
            spec_sha256=cast(str, spec_sha256),
            agent_run_id=cast(str, agent_run_id),
            # ``_as_utc`` because a timestamp typed on a command line is naive and
            # the provenance validator refuses a naive one -- the same convention
            # every other UTC option in this file uses.
            occurred_at=_as_utc(cast(datetime, occurred_at)),
            registered_at=_as_utc(cast(datetime, registered_at)),
        )
        return {
            "bundle": cast(JsonValue, json.loads(bundle.model_dump_json())),
            "digest": canonical_sha256(bundle),
            # Reported rather than enforced: a non-flat run is honest evidence
            # that a later gate will refuse, and hiding it here would only move
            # the surprise.
            "mark_to_market_flat": outcome.equity.is_flat,
        }

    _run(operation)


def _load_json_model[T: CanonicalModel](path: Path, model: type[T]) -> T:
    """One frozen research document, read from a file an operator names.

    ``model_validate_json`` rather than ``model_validate``: every research
    contract is strict, so the decimals, timestamps and UUIDs a serialized
    document carries as strings are only coerced on the JSON path. And the read
    and decode are typed here rather than left to ``_execute``'s catch-all --
    a mistyped path or a file that is not UTF-8 is input, not an internal
    failure, and an ``OSError`` would answer it with a correlation id.
    """

    try:
        data = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise ConfigurationError() from error
    return model.model_validate_json(data)


def _trial_ledger() -> PostgresTrialLedger:
    """The research ledger, behind the migration check every trial command shares.

    The check lives here rather than in a constructor because "the database is at
    the wrong revision" is a command-level answer with its own exit code. A
    ledger constructed first would report "the trial was not recorded" for a
    database that has never had the ledger tables at all, which is a different
    problem with a different remedy.

    Both DSN questions -- which DSN, and whether there is one -- belong to
    ``ops.ledger``, so that a missing second database stays one refusal at one
    boundary rather than a second copy of the rule in the composition root.
    """

    dsn = research_ledger_dsn(_settings())
    connection = open_runtime_connection(dsn)
    try:
        assert_at_head(connection, Config(str(DEFAULT_ALEMBIC_CONFIG)))
    finally:
        connection.close()
    return PostgresTrialLedger(lambda: open_runtime_connection(dsn))


def _evidence_store() -> EvidenceStore:
    return build_evidence_store(_settings())


def _event_payload(record: LedgerRecord) -> dict[str, JsonValue]:
    """One chain row as JSON, carrying the sealed payload inside it.

    The row rather than a re-parsed event, because the row is what the ledger
    holds: it carries the database's own ``recorded_at``, its two digests, and --
    through ``event_json`` -- the evidence reference a reader needs. No
    connection and no DSN is reachable from any of it.
    """

    return cast(dict[str, JsonValue], record.model_dump(mode="json"))


@trial_app.command("register")
def research_trial_register(
    protocol: Annotated[Path, typer.Option("--protocol", help="Frozen TrialProtocol JSON.")],
) -> None:
    """Seal a frozen trial protocol and its whole candidate family.

    One event, not one per candidate: the candidates are declared together or
    not at all, and the event's id is derived from the protocol's canonical
    digest, so re-running this on the same file is a recognised retry rather than
    a second registration.
    """

    def operation() -> dict[str, JsonValue]:
        parsed = _load_json_model(protocol, TrialProtocol)
        trials = _trial_ledger().register(parsed)
        return {
            "protocol_id": parsed.protocol_id,
            "trial_count": len(trials),
            "trial_ids": [trial.trial_id for trial in trials],
        }

    _run(operation)


@trial_app.command("start")
def research_trial_start(
    trial_id: Annotated[str, typer.Option("--trial-id", help="Declared trial id to run.")],
    attempt_id: Annotated[
        str, typer.Option("--attempt-id", help="Id of this execution of that trial.")
    ],
    spec_sha256: Annotated[
        str,
        typer.Option(
            "--spec-sha256",
            help="Digest of the preregistered specification being run, as the trial declares it.",
        ),
    ],
    started_at: Annotated[
        datetime | None,
        typer.Option(
            "--started-at",
            help=(
                "Declared start time, UTC (e.g. 2026-03-01T12:00:00). A retry "
                "passing the same value is recognised and appends nothing, while "
                "a different value is refused as a conflict, so this is only "
                "needed when a run crashed mid-start and it is not knowable "
                "whether its event landed: pass that run's value."
            ),
        ),
    ] = None,
) -> None:
    """Record that one execution of a declared trial began.

    This is the event the three deflation denominators are counted from, so a
    trial that is run without it counts as though it never ran. It is refused
    (exit 15) against a trial no protocol declared, for the same reason ``record``
    is: an execution nobody declared is still a draw from the search space, and
    admitting one would let the denominator be widened by whoever cares to.

    ``--spec-sha256`` is the operator's word, not a lookup. The preregistration
    seals a whole protocol and does not publish a per-candidate digest, so the
    chain preserves what it is given and ``count`` counts distinct digests. That
    is stated in the README rather than left to be discovered.

    ``--started-at`` is the operator's declared start time, for the same reason
    and with the same caveat as a bundle's ``registered_at``: it is provenance,
    not evidence of order, and the database row's ``recorded_at`` is the only
    registration-order authority. The chain cannot observe when a backtest
    actually began.
    """

    def operation() -> dict[str, JsonValue]:
        record = _trial_ledger().append(
            execution_started_event(
                trial_id,
                attempt_id,
                spec_sha256,
                # Read once, and the same value on every call within this process,
                # so a retry that reaches the event again derives identical bytes.
                # ``_as_utc`` because a timestamp typed on a command line is naive
                # and the payload refuses a naive one -- the same convention every
                # other UTC option in this file uses.
                started_at=_as_utc(started_at) if started_at is not None else datetime.now(UTC),
            )
        )
        return {
            "trial_id": trial_id,
            "attempt_id": attempt_id,
            "sequence": record.sequence,
        }

    _run(operation)


@trial_app.command("record")
def research_trial_record(
    trial_id: Annotated[str, typer.Option("--trial-id")],
    attempt_id: Annotated[str, typer.Option("--attempt-id")],
    evidence: Annotated[Path, typer.Option("--evidence", help="Evidence bundle JSON.")],
) -> None:
    """Seal one attempt's evidence and record that it was written.

    The bundle is the single source of identity, so ``--trial-id`` and
    ``--attempt-id`` are checked against it rather than trusted: a row whose
    ``trial_id`` column disagreed with the payload it seals is exactly what
    nothing else in this system would notice.

    The evidence is written before the events are appended, so a failed append
    leaves a document nothing points at rather than a ledger row whose document
    is missing -- the unreferenced side of that pair is the recoverable one. Both
    events are derived from the bundle, and both ids are deterministic, so a
    retried ``record`` is one event read back.
    """

    def operation() -> dict[str, JsonValue]:
        bundle = _load_json_model(evidence, EvidenceBundle)
        if bundle.trial_id != trial_id or bundle.attempt_id != attempt_id:
            raise SchemaValidationError()
        # The ledger is resolved -- and the migration head checked -- before the
        # first byte is written. A database that has never had the ledger tables
        # cannot record the seal either, so writing first would leave a document
        # behind for a command that then refuses to name it.
        ledger = _trial_ledger()
        # The declaration is asked for before the write rather than discovered by
        # the append. The store's own containment check is still the rule and
        # still refuses; asking first is what keeps a trial nobody declared from
        # leaving a sealed document on disk that no ledger row points at. Same
        # error either way, because from the operator's side it is the same
        # refusal, and a second exit code for a preflight would be a distinction
        # they cannot act on.
        if not ledger.declares_trial(trial_id):
            raise TrialLedgerAppendError()
        stored = _evidence_store().write(bundle)
        # ponytail: two appends, two transactions. A crash between them commits
        # RESULT_RECORDED without EVIDENCE_SEALED, and ``verify()`` still reports
        # a valid chain -- the bytes it holds are intact, only the reference to
        # the file is absent, and a verifier is asked whether the ledger lies,
        # not whether it is complete. Re-running ``record`` with the same bundle
        # closes the pair, because both event ids are content-derived. The fix is
        # one store transaction appending both events; it changes
        # ``PostgresTrialLedger``'s public surface, so add ``append_all(events)``
        # when an operator is bitten, not before.
        ledger.append(result_recorded_event(bundle))
        ledger.append(evidence_sealed_event(bundle, stored.sha256))
        return {"trial_id": trial_id, "evidence_sha256": stored.sha256}

    _run(operation)


@trial_app.command("import-legacy")
def research_trial_import_legacy(
    artifact: Annotated[Path, typer.Option("--artifact", help="Phase 7 outer result JSON.")],
    registered_at: Annotated[
        datetime | None,
        typer.Option(
            "--registered-at",
            help=(
                "Declared import clock, UTC (e.g. 2026-03-01T12:00:00). An "
                "ordinary retry is recognised by the source result digest and "
                "writes nothing, so this is only needed when a run crashed "
                "between writing the bundle and appending the event: pass that "
                "run's value and the retry derives the same digest."
            ),
        ),
    ] = None,
) -> None:
    """Import one preserved Phase 7 result as the legacy evidence it is.

    Never a registration: the event is ``LEGACY_IMPORTED`` and says why it is
    late. Idempotent by source result digest, so a second import adds no trial,
    attempt, or selection candidate -- and it answers with the digest the chain
    actually recorded rather than the one this run would have written.
    """

    def operation() -> dict[str, JsonValue]:
        result = import_phase7_artifact(
            artifact,
            ledger=_trial_ledger(),
            evidence=_evidence_store(),
            # Read once, and the same value on every call within this process, so
            # a retry that reaches the bundle again derives identical bytes.
            # ``_as_utc`` because a timestamp typed on a command line is naive
            # and the bundle's provenance refuses a naive one -- the same
            # convention every other UTC option in this file uses.
            now=_as_utc(registered_at) if registered_at is not None else datetime.now(UTC),
        )
        return {
            "trial_id": result.trial_id,
            "source_result_sha256": result.source_result_sha256,
            "evidence_sha256": result.evidence_sha256,
            "already_present": result.already_present,
        }

    _run(operation)


@trial_app.command("show")
def research_trial_show(
    trial_id: Annotated[str, typer.Option("--trial-id")],
) -> None:
    """Replay one trial's chain rows, and the evidence they reference.

    A trial nobody declared is an empty list rather than an error: the question
    is what the chain holds for this id, and inventing a trial to answer it would
    be the one thing a reader of the ledger must never do.
    """

    def operation() -> dict[str, JsonValue]:
        events = _trial_ledger().events_for(trial_id)
        return {
            "trial_id": trial_id,
            "events": cast(list[JsonValue], [_event_payload(record) for record in events]),
        }

    _run(operation)


@trial_app.command("count")
def research_trial_count() -> None:
    """Report the three deflation denominators, counted from the chain.

    Attempts, selection lotteries, and effective specifications answer different
    questions and are tracked separately: a repeated execution raises the audit
    count without being a new lottery, and that distinction is the reason these
    are three numbers rather than one.

    The specification count is a set of the ``spec_sha256`` values callers
    supplied, and the chain preserves them without vouching that any of them
    matches a candidate the protocol actually declared. Read it as a label the
    ledger keeps, not a verdict.
    """

    def operation() -> dict[str, JsonValue]:
        counters = _trial_ledger().counters()
        return {
            "audit_attempts": counters.audit_attempts,
            "selection_lotteries": counters.selection_lotteries,
            "effective_specifications": counters.effective_specifications,
        }

    _run(operation)


@trial_app.command("verify")
def research_trial_verify() -> None:
    """Verify the event chain, then re-read every file the chain points at.

    Two checks, and the second is the one a hash chain cannot do for itself: the
    hashes prove the events were not rewritten, and only reading the sealed
    bundles proves the evidence they name still exists and still hashes to its
    own name. A chain failure is reported rather than raised, because an operator
    asking "is this intact?" needs a plain reason, not a stack trace.
    """

    def operation() -> dict[str, JsonValue]:
        ledger = _trial_ledger()
        report = ledger.verify()
        if not report.valid:
            return {
                "valid": False,
                "checked_events": report.checked_events,
                "reason": report.reason,
            }
        evidence = _evidence_store()
        for event in ledger.replay():
            # A broken chain's events are not evidence of anything, which is why
            # this loop only runs once the report says the chain is intact.
            if isinstance(event.payload, (EvidenceSealedPayload, LegacyImportedPayload)):
                evidence.verify(event.payload.evidence_sha256)
        return {"valid": True, "checked_events": report.checked_events, "reason": None}

    payload = _execute(operation)
    _emit(payload, status="ok" if payload["valid"] else "invalid")
    if not payload["valid"]:
        # Deliberately not ``_fail``: the integrity report is the answer to
        # "is this intact?" and is written to stdout so it can be piped into a
        # reporter, while the exit code stays the signal a script branches on.
        raise typer.Exit(code=int(ExitCode.TRIAL_LEDGER_INTEGRITY))


if __name__ == "__main__":  # pragma: no cover
    # ``python -m trading_house.cli`` is how the determinism test starts a run
    # in a fresh interpreter under its own PYTHONHASHSEED. Without this the
    # module imports and exits silently, and the test compares two empty
    # strings -- which would pass.
    app()
