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
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
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
from pydantic import JsonValue, TypeAdapter, ValidationError
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
from trading_house.constitution.loader import LoadedConstitution, load_constitution
from trading_house.constitution.signing import load_private_key, sign_bytes
from trading_house.core.clock import SystemClock, ensure_utc
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
    PortfolioRiskRefusedError,
    PromotionRefusedError,
    ScenarioEvidenceError,
    SchemaValidationError,
    SignatureVerificationError,
    StatisticalInputError,
    TimestampError,
    TradingHouseError,
    TrialLedgerAppendError,
    TrialLedgerIntegrityError,
    UnresolvedIntentsError,
)
from trading_house.core.instruments import InstrumentContract
from trading_house.core.schemas import (
    RISK_DECISION_ADAPTER,
    OrderIntent,
    RejectedRiskDecision,
    Side,
)
from trading_house.core.values import (
    AssetClass,
    BookId,
    CanonicalModel,
    Horizon,
    IntentState,
    NonEmptyStr,
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
from trading_house.ops.backtest import build_strategy, mark_to_market_bundle, simulate
from trading_house.ops.compounding import (
    CompoundingReport,
    compounding_report,
    refuse_other_replay,
    request_replay_inputs,
    sealed_baseline,
    sealed_rerun,
)
from trading_house.ops.dataset import refuse_changed_dataset, refuse_dataset_mismatch, window_digest
from trading_house.ops.decide import (
    decide_trial,
    holdout_status,
    latest_report,
    refuse_unopenable,
    verify_reports,
)
from trading_house.ops.guard import LedgerEscalator, Mt5ProtectionPort
from trading_house.ops.health import BookReconciler, HealthService, build_audit_event
from trading_house.ops.holdout import refuse_forged_holdout_provenance, refuse_outside_coverage
from trading_house.ops.ledger import (
    build_evidence_store,
    execution_started_event,
    research_ledger_dsn,
    seal_bundle,
)
from trading_house.ops.package import package_from_chain, verify_package
from trading_house.ops.portfolio import recheck_submission
from trading_house.ops.scenarios import (
    ScenarioReport,
    baseline_at,
    declared_candidate,
    declared_grid,
    declared_window,
    refuse_edited_protocol,
    refuse_other_attempts,
    refuse_reused_attempts,
    registered_protocol,
    scenario_report,
    sealed_bundles,
    sealed_levels,
    started_attempts,
)
from trading_house.ops.splits import SplitsReport, splits_report
from trading_house.ops.validate import read_validation_inputs, statistical_evidence
from trading_house.research.backtest.costs import CostModel
from trading_house.research.backtest.engine import BacktestRefused, BacktestRequest
from trading_house.research.backtest.sizing import SizingMode
from trading_house.research.canonical import canonical_sha256
from trading_house.research.evidence import EvidenceBundle, EvidenceStore
from trading_house.research.ledger_store import PostgresTrialLedger
from trading_house.research.legacy_import import import_phase7_artifact
from trading_house.research.packages import PromotionStage, StrategyPackage
from trading_house.research.trial_ledger import (
    EvidenceSealedPayload,
    HoldoutState,
    LedgerRecord,
    LegacyImportedPayload,
    TrialProtocol,
    TrialSpec,
)
from trading_house.research.validation.capacity import capacity_diagnostic
from trading_house.risk.engine import RiskEngine
from trading_house.settings import RuntimeSettings
from trading_house.strategies.registry import strategy_scope

DEFAULT_CONSTITUTION = Path("config/risk_constitution.yaml")
DEFAULT_SIGNATURE = Path("config/risk_constitution.yaml.sig")
DEFAULT_PUBLIC_KEY = Path("config/risk_constitution.public.pem")
DEFAULT_BINDING = Path("config/venue_binding.mt5.yaml")
DEFAULT_BINDING_SIGNATURE = Path("config/venue_binding.mt5.yaml.sig")
DEFAULT_ALEMBIC_CONFIG = Path("alembic.ini")


class ExitPolicyName(StrEnum):
    NONE = "none"
    FIXED_TARGET = "fixed_target"
    CHANDELIER = "chandelier"


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
    ScenarioEvidenceError: ExitCode.SCENARIO_EVIDENCE,
    StatisticalInputError: ExitCode.STATISTICAL_INPUT,
    PromotionRefusedError: ExitCode.PROMOTION_REFUSED,
    PortfolioRiskRefusedError: ExitCode.PORTFOLIO_RISK,
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
package_app = typer.Typer(no_args_is_help=True, help="Strategy-package commands.")
dataset_app = typer.Typer(no_args_is_help=True, help="Dataset-digest commands.")
app.add_typer(constitution_app, name="constitution")
app.add_typer(db_app, name="db")
app.add_typer(audit_app, name="audit")
app.add_typer(data_app, name="data")
app.add_typer(order_app, name="order")
app.add_typer(guard_app, name="guard")
app.add_typer(backtest_app, name="backtest")
app.add_typer(research_app, name="research")
research_app.add_typer(trial_app, name="trial")
research_app.add_typer(package_app, name="package")
research_app.add_typer(dataset_app, name="dataset")


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
    except PortfolioRiskRefusedError as error:
        # The refusing gates are RejectionReason values, a closed enum, so the
        # key carries no free text from a DSN, a path or a broker message.
        raise _fail(
            error.public_message,
            EXIT_CODES[PortfolioRiskRefusedError],
            reasons=",".join(error.reasons),
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
            # Phase 10: the decision was sized against the portfolio as it
            # was then. Read it again, under the lock and after the gate, and
            # refuse before an intent exists if a signed limit now would.
            constitution = _constitution().constitution
            recheck_submission(
                RiskEngine(constitution, clock),
                approved,
                venue=adapter,
                history=ledger,
                binding=binding,
                books=tuple(constitution.books),
                book=book,
                instrument_id=instrument,
                side=side,
                now=clock.now(),
            )
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
    """Replay one registered strategy over the stored bars of its registry instrument and
    timeframe.

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
        settings = _settings()
        instrument_contract = _instrument_contract(contract, strategy_id=strategy)
        request = _backtest_request(
            strategy=strategy,
            exit_policy=exit_policy,
            start=start,
            end=end,
            firm_equity=_decimal(firm_equity),
            cost_model=CostModel(
                commission_per_lot_per_side=_decimal(commission_per_lot_per_side),
                slippage_points_per_side=_decimal(slippage_points_per_side),
                swap_long_points_per_day=_decimal(swap_long_points_per_day),
                swap_short_points_per_day=_decimal(swap_short_points_per_day),
                triple_swap_weekday=triple_swap_weekday,
                stress_multiplier=_decimal(stress_multiplier),
            ),
            atr_period=atr_period,
            spread_window=spread_window,
            defective_bar_tolerance=_unit_interval_decimal(defective_bar_tolerance),
        )
        loaded_constitution = load_constitution(
            settings.constitution_path,
            settings.constitution_signature_path,
            settings.constitution_public_key_path,
        )
        outcome = simulate(
            request,
            bars=_bar_store(),
            contract=instrument_contract,
            constitution=loaded_constitution,
        )
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


def _declared_candidate(protocol: TrialProtocol, trial_id: str) -> TrialSpec:
    """The one candidate a trial names, or a refusal naming what was asked for.

    ``StopIteration`` would be the natural failure of a bare ``next(...)`` and
    the wrong one here: it is not an error the catch-all may report as an
    internal fault, and the remedy is the operator passing the ``--trial-id``
    the protocol actually declares.

    The lookup itself is ``ops.scenarios.declared_candidate`` and not a second
    copy of it, because this command and the report's identity check must agree
    about which candidate a grid was registered as -- the orchestrator seals this
    candidate's digest and the report compares against it, so a divergence here
    would refuse a correctly sealed grid at exit 19, after three documents and
    nine events were already in the chain. Only the error class is this layer's.
    """

    candidate = declared_candidate(protocol, trial_id)
    if candidate is None:
        raise ConfigurationError()
    return candidate


def _refuse_scope_mismatch(protocol: TrialProtocol) -> None:
    """A registered ``data`` scope must be the strategy's own, never a borrowed one.

    ``TrialProtocol.data.instrument_id``/``timeframe`` are otherwise never compared
    with anything. A protocol copied from another strategy's registration -- a
    Phase 7 file with ``timeframe: M15`` reused for an H1 strategy -- would
    otherwise be accepted, and the append-only ledger would permanently record a
    protocol that misdescribes the data its runs replayed. Called before anything
    is written: at ``register``, the earliest point, and inside ``_seal_levels``,
    which every run-producing command funnels through.

    An unregistered ``strategy_id`` is not this check's problem to raise: at
    ``register`` a protocol may preregister a strategy that is not (yet, or
    ever) in the code registry -- ``register`` has never required one, and a
    run-producing command raises its own ``ConfigurationError`` for an unknown
    strategy before it would ever reach here. Silent on that case rather than
    refusing it under this rule.
    """

    try:
        scope = strategy_scope(protocol.strategy_id)
    except ConfigurationError:
        return
    if (protocol.data.instrument_id, protocol.data.timeframe) != (
        scope.instrument_id,
        Timeframe(scope.timeframe),
    ):
        raise ConfigurationError()


def _instrument_contract(contract: Path, *, strategy_id: str) -> InstrumentContract:
    """The contract file, decoded and checked against the strategy's registered instrument.

    The ``try``/``except`` moves out of ``backtest run`` unchanged, comment and
    all. ``--contract`` has no producer in this repo, so a hand-written
    trailing comma is the likeliest mistake either command will see and both
    must answer it at exit 2.
    """

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
    if instrument_contract.instrument_id != strategy_scope(strategy_id).instrument_id:
        raise ConfigurationError()
    return instrument_contract


def _backtest_request(
    *,
    strategy: str,
    exit_policy: ExitPolicyName,
    start: datetime,
    end: datetime,
    firm_equity: Decimal,
    cost_model: CostModel,
    atr_period: int,
    spread_window: int,
    defective_bar_tolerance: Decimal,
    sizing: SizingMode = SizingMode.CONSTANT_NOTIONAL,
) -> BacktestRequest:
    """The run's declared inputs, with every cost already in ``cost_model``.

    Takes a built ``CostModel`` rather than the six cost options, because the
    orchestrator's only difference from ``backtest run`` is where the multiplier
    came from -- its grid rather than a typed ``--stress-multiplier``. Taking the
    model makes that the single difference and keeps the rest of the request
    construction out of the loop.

    The ``try``/``except ValueError`` moves out of ``backtest run`` unchanged:
    ``BacktestRequest`` validates ``firm_equity`` in ``__post_init__`` and
    raises a bare ``ValueError``, so without it a mistyped equity escapes as
    "unexpected failure".
    """

    try:
        scope = strategy_scope(strategy)
        return BacktestRequest(
            strategy=build_strategy(strategy, exit_policy=scope.exit_arm(exit_policy.value)),
            instrument_id=scope.instrument_id,
            timeframe=Timeframe(scope.timeframe),
            start=_as_utc(start),
            end=_as_utc(end),
            firm_equity=firm_equity,
            cost_model=cost_model,
            atr_period=atr_period,
            spread_window=spread_window,
            defective_bar_tolerance=defective_bar_tolerance,
            sizing=sizing,
        )
    except ValueError as error:
        # ``firm_equity`` above; a bad range raises ``BacktestRefused`` from
        # ``run()`` further down. Two shapes out of one boundary: wrapping only
        # the second would let a mistyped equity escape as "unexpected failure".
        raise ConfigurationError() from error


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


def _evidence_bundle(path: Path) -> EvidenceBundle:
    """The bundle ``record`` seals, from either of the two documents it accepts.

    A bare ``EvidenceBundle``, or the command payload ``backtest run --mark-to-market``
    writes, which wraps one under ``"bundle"`` inside the status envelope every
    command here emits. The second shape is accepted only because that command
    produces it: an operator following the documented flow redirects one into
    ``--evidence`` verbatim, and refusing it would refuse the flow the design
    documents. It is unwrapped here rather than in ``_load_json_model`` because
    that helper has other callers -- ``register`` still wants a bare
    ``TrialProtocol`` -- and a wrapper one command produces is not a wrapper
    every caller should learn to accept.

    Nothing else is read. A document that is neither shape fails validation
    exactly as a mistyped path does, which is the same ``configuration invalid``
    refusal at exit 2, because both are an operator who named a document this
    command does not accept.
    """

    try:
        data = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise ConfigurationError() from error
    try:
        document = json.loads(data)
    except json.JSONDecodeError as error:
        # Same exit and message as the pydantic refusal below, reached before it:
        # malformed JSON is not a bundle in either shape, and letting the decode
        # error escape would answer it with a correlation id.
        raise ConfigurationError() from error
    if isinstance(document, dict) and "bundle" in document:
        data = json.dumps(document["bundle"])
    return EvidenceBundle.model_validate_json(data)


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


@dataset_app.command("digest")
def research_dataset_digest(
    instrument: Annotated[str, typer.Option("--instrument", help="Instrument id.")],
    timeframe: Annotated[Timeframe, typer.Option("--timeframe", help="Bar timeframe.")],
    start: Annotated[
        datetime, typer.Option("--start", help="First bar open time, UTC, inclusive.")
    ],
    end: Annotated[datetime, typer.Option("--end", help="Last bar open time, UTC, inclusive.")],
) -> None:
    """Print the digest of the stored bars a replay of one window reads.

    Read only. The digest is a SHA-256, domain-separated and canonical, over exactly the bars
    ``backtest run`` and the run commands read for the same ``--start`` and ``--end`` (defective
    bars included, one bar past ``--end`` in the store's half-open range). Declare it as a
    protocol's ``data.dataset_sha256`` or ``holdout.dataset_sha256`` instead of typing a hash:
    a run whose stored bars hash differently is refused before anything is written. It also
    prints the bar count and the first and last bar times it covers.

    It proves what bytes the replay reads, not where they came from; a store that is later
    corrected hashes differently, by design. A window the store does not hold is refused (exit
    10), and so is a window inside the store that holds no bar (exit 20).
    """

    def operation() -> dict[str, JsonValue]:
        found = window_digest(
            _bar_store(),
            instrument_id=instrument,
            timeframe=timeframe,
            start=_as_utc(start),
            end=_as_utc(end),
        )
        return {
            "dataset_sha256": found.dataset_sha256,
            "instrument_id": instrument,
            "timeframe": timeframe.value,
            "bar_count": found.bar_count,
            "first_event_time": found.first_event_time.isoformat(),
            "last_event_time": found.last_event_time.isoformat(),
        }

    _run(operation)


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
        _refuse_scope_mismatch(parsed)
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

    ``--spec-sha256`` is the ``canonical_sha256`` of the declared ``TrialSpec``,
    not of the protocol. The ledger refuses (exit 15, nothing written) a digest
    that no preregistration declares for the trial; a trial declared only by a
    legacy import is not checked. See the README's "Phase 8A.1".

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
    evidence: Annotated[
        Path,
        typer.Option(
            "--evidence",
            help=(
                "Evidence bundle JSON: a bare bundle, or the document "
                "`backtest run --mark-to-market` printed."
            ),
        ),
    ],
) -> None:
    """Seal one attempt's evidence and record that it was written.

    ``--evidence`` takes the document ``backtest run --mark-to-market`` printed,
    status envelope and all, as well as a bare bundle: see ``_evidence_bundle``,
    which reads those two shapes and nothing else.

    The bundle is the single source of identity, so ``--trial-id`` and
    ``--attempt-id`` are checked against it rather than trusted: a row whose
    ``trial_id`` column disagreed with the payload it seals is exactly what
    nothing else in this system would notice.

    The evidence is written before the events are appended, so a failed append
    leaves a document nothing points at rather than a ledger row whose document
    is missing -- the unreferenced side of that pair is the recoverable one. Both
    events are derived from the bundle, and both ids are deterministic, so a
    retried ``record`` is one event read back.

    A bundle whose provenance says the holdout was opened, consumed or locked is
    refused (exit 21) before any write: only ``open-holdout`` seals an opened bundle.
    """

    def operation() -> dict[str, JsonValue]:
        bundle = _evidence_bundle(evidence)
        if bundle.trial_id != trial_id or bundle.attempt_id != attempt_id:
            raise SchemaValidationError()
        # Only ``open-holdout`` may seal an opened bundle: refused before anything is written.
        refuse_forged_holdout_provenance(bundle)
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
        # The store write and the two ledger appends behind this call are three
        # operations, and the deferral note for that is on ``seal_bundle``.
        evidence_sha256 = seal_bundle(bundle, ledger=ledger, store=_evidence_store())
        return {"trial_id": trial_id, "evidence_sha256": evidence_sha256}

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

    The specification count is a set of the ``spec_sha256`` values the chain's
    starts carry. The ledger vouches for a start of a preregistered trial at
    append time (Phase 8A.1), but not for a legacy import, not for other event
    types, and not for a start appended before 8A.1.
    """

    def operation() -> dict[str, JsonValue]:
        counters = _trial_ledger().counters()
        return {
            "audit_attempts": counters.audit_attempts,
            "selection_lotteries": counters.selection_lotteries,
            "effective_specifications": counters.effective_specifications,
        }

    _run(operation)


def _scenario_report_for(
    trial_id: str, ledger: PostgresTrialLedger, store: EvidenceStore
) -> ScenarioReport:
    """One candidate's report, from the chain and the evidence store alone.

    Two different reads, and the difference is the whole point. The bundles come
    from ``events_for(trial_id)``, which is right for them: an
    ``EVIDENCE_SEALED`` row does carry the trial's id, so one trial's sealed
    documents are exactly the rows that query returns. The protocol does not:
    a ``PREREGISTERED`` row's ``trial_id`` is null, because one event seals a
    whole candidate family, so ``_TRIAL_EVENTS_SQL`` -- ``WHERE trial_id = %s``
    -- never returns it. ``replay()`` is the read that carries the registration.

    Both are used here rather than choosing one, because getting it wrong fails
    quietly in the direction that matters: from ``events_for`` alone every
    registered candidate would be reported as never registered, and the operator
    would be told to go preregister a trial they already preregistered.

    Each digest is read through ``store.read``, which re-serialises what it
    decoded and refuses any document whose bytes are not today's canonical
    encoding. A report is therefore a read of bytes that were verified on the way
    in, not of objects a caller assembled.

    ``record.event_json`` is the chain's stored *event* -- the whole
    ``LedgerEvent``, projected into jsonb -- so the payload is reached at
    ``["payload"]`` and read through ``EvidenceSealedPayload`` rather than out of
    a dict: a malformed row is a ``ValidationError`` from the ledger's own schema
    rather than a ``KeyError`` from here.
    """

    sealed = sealed_bundles(ledger.events_for(trial_id), store.read)
    return scenario_report(
        trial_id=trial_id,
        protocol=registered_protocol(ledger.replay(), trial_id),
        sealed=sealed,
    )


@dataclass(frozen=True)
class _RunInputs:
    """What ``scenarios`` and ``compounding`` hand one simulation besides its costs."""

    parsed: TrialProtocol
    trial_id: str
    spec_sha256: str
    ledger: PostgresTrialLedger
    store: EvidenceStore
    constitution: LoadedConstitution
    instrument_contract: InstrumentContract
    started_at: datetime
    occurred_at: datetime
    registered_at: datetime
    agent_run_id: str
    exit_policy: ExitPolicyName
    firm_equity: str
    atr_period: int
    spread_window: int
    defective_bar_tolerance: str
    opening: bool = False
    """True for the one-time holdout opening: the window is the protocol's holdout, the
    bundles are sealed as OPENED, and nothing already sealed is a level to skip."""


def _constitution() -> LoadedConstitution:
    settings = _settings()
    return load_constitution(
        settings.constitution_path,
        settings.constitution_signature_path,
        settings.constitution_public_key_path,
    )


def _request_for(run: _RunInputs, cost_model: CostModel, sizing: SizingMode) -> BacktestRequest:
    """One level's request, built from the protocol and the options, and nothing else."""

    start, end = declared_window(run.parsed, opened=run.opening)
    return _backtest_request(
        # The strategy the registration names, the window it declared, and the
        # costs built from its baseline -- things the reports check against that
        # same protocol, so they are read from it rather than typed here. What is
        # left of ``_backtest_request``'s arguments is what a protocol does not
        # state and therefore cannot contradict.
        strategy=run.parsed.strategy_id,
        exit_policy=run.exit_policy,
        start=start,
        end=end,
        firm_equity=_decimal(run.firm_equity),
        cost_model=cost_model,
        atr_period=run.atr_period,
        spread_window=run.spread_window,
        defective_bar_tolerance=_unit_interval_decimal(run.defective_bar_tolerance),
        sizing=sizing,
    )


def _refuse_unwritable_provenance(run: _RunInputs, attempt_ids: Iterable[str]) -> None:
    """The values ``mark_to_market_bundle`` would validate only after the start row.

    Same checks, taken first: a blank id or an offset-less timestamp is a typed
    ``ConfigurationError`` here and not an orphan start row there.
    """

    text = TypeAdapter(NonEmptyStr)
    for value in (run.trial_id, run.agent_run_id, *attempt_ids):
        try:
            text.validate_python(value)
        except ValidationError as error:
            raise ConfigurationError() from error
    for moment in (run.started_at, run.occurred_at, run.registered_at):
        ensure_utc(_as_utc(moment))


def _seal_levels(
    run: _RunInputs,
    *,
    sizing: SizingMode,
    attempts: Mapping[Decimal, str],
    unsealed_retry: bool,
) -> dict[Decimal, str]:
    """Pre-flight every refusal, then run the levels not already sealed.

    Returns each level's evidence digest, for a level skipped as much as for one
    run. Everything that needs no simulation is taken before the first append, in
    this order: the ``--protocol`` file must be the registered protocol; every
    level's request is built and validated, and the option values the bundle would
    validate later are validated now (a mistyped option leaves no orphan start
    row); the code's strategy id and version equal the protocol's; no level is
    sealed under another attempt id; no attempt id this run would start is already
    started; the run's replay inputs must equal
    those of the constant-notional baseline the chain holds -- for ``compounding``
    the one 1.0x baseline (required), for ``scenarios`` every constant level
    already sealed, if any; and, last, the stored bars of the window must hash to the
    protocol's declared dataset hash (8E: the only pre-flight that reads the bar store,
    skipped when every level is already sealed, and a window the store does not hold is
    refused here as a coverage error).

    A level already sealed under this run's own attempt id is skipped entirely:
    no start event, no simulation, no seal. Re-running would derive a different
    bundle and seal a second document at that level.

    ``run.opening`` is the one-time holdout opening (8D2): the window is the protocol's
    holdout, the bundles are sealed as OPENED, no level counts as already done, and the
    replay inputs must equal the research 1.0x baseline's (``sealed_baseline`` requires
    exactly one).

    The reads above and the writes below are not one transaction. The slice
    assumes a single operator: two concurrent runs could both pass the pre-flight.
    Take a ledger lock if that assumption stops holding.

    What can still orphan a start row: a data-dependent ``BacktestRefused`` from
    ``simulate``, a ``derive_daily_returns`` refusal (a ruined account), both of
    which need the bars, and the post-run dataset agreement (the store changed between
    the pre-flight and the run: one row more, no file more). For ``compounding`` the
    attempt id is then spent.
    """

    _refuse_scope_mismatch(run.parsed)
    refuse_edited_protocol(registered_protocol(run.ledger.replay(), run.trial_id), run.parsed)
    _refuse_unwritable_provenance(run, attempts.values())
    requests = {m: _request_for(run, baseline_at(run.parsed, m), sizing) for m in attempts}
    for request in requests.values():
        if (request.strategy.id, request.strategy.version) != (
            run.parsed.strategy_id,
            run.parsed.strategy_version,
        ):
            raise ScenarioEvidenceError() from ValueError(
                f"the code's strategy is {request.strategy.id} {request.strategy.version}; "
                f"the protocol declares {run.parsed.strategy_id} {run.parsed.strategy_version}"
            )
    records = run.ledger.events_for(run.trial_id)
    sealed = sealed_bundles(records, run.store.read)
    if run.opening:
        # An opening is a different experiment from the grid whose levels sit sealed beside
        # it, and the command has already refused any earlier opening: no level is "done",
        # and the research attempt ids are not this run's to compare against.
        done: dict[Decimal, str] = {}
    else:
        refuse_other_attempts(sealed, sizing=sizing, allowed=attempts.get)
        done = sealed_levels(sealed, sizing=sizing, allowed=attempts.get)
    running = {m: a for m, a in attempts.items() if m not in done}
    refuse_reused_attempts(
        started_attempts(records), sealed, running=running, unsealed_retry=unsealed_retry
    )
    if sizing is SizingMode.COMPOUNDING or run.opening:
        baselines = [sealed_baseline(sealed, run.trial_id)[1]]
    else:
        baselines = [b for _, b in sealed if b.sizing is SizingMode.CONSTANT_NOTIONAL]
    expected = request_replay_inputs(
        next(iter(requests.values())),
        contract_sha256=run.instrument_contract.digest(),
        constitution_sha256=run.constitution.constitution_sha256,
    )
    for baseline in baselines:
        refuse_other_replay(expected, baseline.result, what="this run")
    digests = dict(done)
    if running:
        # The last pre-flight, and the only one that reads the bar store: the stored bars of
        # this window must hash to the digest the protocol declared (8E, E-5). Taken once,
        # because every level replays the same window, and not at all when every level is
        # already sealed, which writes and runs nothing.
        window = next(iter(requests.values()))
        declared = (
            run.parsed.holdout.dataset_sha256 if run.opening else run.parsed.data.dataset_sha256
        )
        computed = window_digest(
            _bar_store(),
            instrument_id=window.instrument_id,
            timeframe=window.timeframe,
            start=window.start,
            end=window.end,
        ).dataset_sha256
        refuse_dataset_mismatch(declared, computed)
        for multiplier, request in requests.items():
            digests[multiplier] = _run_and_seal(
                run, attempt_id=attempts[multiplier], request=request, preflight_digest=computed
            )
    return digests


def _run_and_seal(
    run: _RunInputs, *, attempt_id: str, request: BacktestRequest, preflight_digest: str
) -> str:
    """One attempt: start it, simulate it, seal its bundle. Returns the evidence digest.

    The digest the run computed over the bars it replayed must be the one the pre-flight took
    (8E, E-6): the store did not change in between. Checked before the bundle is built, so a
    run on other data seals nothing; its start row is already appended and its attempt id spent,
    like any refusal that needs the bars.
    """

    run.ledger.append(
        execution_started_event(run.trial_id, attempt_id, run.spec_sha256, _as_utc(run.started_at))
    )
    outcome = simulate(
        request,
        bars=_bar_store(),
        contract=run.instrument_contract,
        constitution=run.constitution,
    )
    refuse_changed_dataset(preflight_digest, outcome.dataset_sha256)
    bundle = mark_to_market_bundle(
        outcome,
        trial_id=run.trial_id,
        attempt_id=attempt_id,
        spec_sha256=run.spec_sha256,
        agent_run_id=run.agent_run_id,
        occurred_at=_as_utc(run.occurred_at),
        registered_at=_as_utc(run.registered_at),
        holdout_state=HoldoutState.OPENED if run.opening else HoldoutState.NOT_DEFINED,
    )
    return seal_bundle(bundle, ledger=run.ledger, store=run.store)


@trial_app.command("scenarios")
def research_trial_scenarios(
    protocol: Annotated[
        Path,
        typer.Option(
            "--protocol",
            help=(
                "Frozen TrialProtocol JSON. The grid, its costs, its window and its "
                "strategy all come from here."
            ),
        ),
    ],
    trial_id: Annotated[str, typer.Option("--trial-id")],
    attempt_prefix: Annotated[
        str,
        typer.Option("--attempt-prefix", help="Attempt ids are this prefix plus the multiplier."),
    ],
    started_at: Annotated[datetime, typer.Option("--started-at")],
    occurred_at: Annotated[datetime, typer.Option("--occurred-at")],
    registered_at: Annotated[datetime, typer.Option("--registered-at")],
    agent_run_id: Annotated[str, typer.Option("--agent-run-id")],
    exit_policy: Annotated[ExitPolicyName, typer.Option("--exit-policy")],
    firm_equity: Annotated[str, typer.Option("--firm-equity")],
    contract: Annotated[Path, typer.Option("--contract")],
    atr_period: Annotated[int, typer.Option("--atr-period", min=1)],
    spread_window: Annotated[int, typer.Option("--spread-window", min=1)],
    defective_bar_tolerance: Annotated[str, typer.Option("--defective-bar-tolerance")] = "0",
) -> None:
    """Run, seal and compare the cost grid this protocol preregistered.

    Every level is one attempt, because §5.6 counts a distinct ``attempt_id``
    as an audit attempt and a distinct ``trial_id`` as a selection lottery: one
    candidate examined at three cost levels is three attempts and **one**
    selection, and the selection count is what DSR divides by.

    The grid, its costs, its window and its strategy come from ``--protocol``
    and from nowhere else. There is deliberately no ``--commission-per-lot-per-side``
    here although ``backtest run`` has one, and no ``--start``/``--end``
    although that has them too: the protocol preregistered ``costs.baseline``,
    ``data.start``/``data.end`` and ``strategy_id``, ``scenario-report`` checks
    every level against those same declarations, and a second copy typed on the
    command line could only ever agree with them or refuse the whole grid.

    The rule behind that is one sentence: **this command takes no option for any
    value the report compares against the protocol.** The remaining options are
    ones the registration does not state -- ``--exit-policy``, ``--firm-equity``,
    ``--atr-period``, ``--spread-window``, ``--defective-bar-tolerance``,
    ``--contract`` -- so a copy of one of those cannot disagree with anything.

    It matters more than tidiness, because the mistake is a one-way door. An
    operator who registered over a year and typed the last week would get three
    sealed documents and nine appended events before the report refused at 19, and
    nothing in a retry could undo them. Not having the option is the only version
    of this that has no such state to reach.

    Everything that needs no simulation is refused before anything is written. A
    ``--protocol`` **file** edited after registration is refused: the command
    compares the file's canonical digest with the registered protocol's. So is a
    level already sealed under another attempt id, an attempt id the trial has
    already started for another level, stored bars of the window that do not hash to the
    protocol's declared ``data.dataset_sha256`` (``research dataset digest`` prints it), and
    a run whose replay inputs
    (``--firm-equity``, ``--exit-policy``, ``--atr-period``, ``--spread-window``,
    ``--defective-bar-tolerance``, the contract and the constitution) differ from
    those of a constant-notional level already sealed. The options are built and
    validated first, so a mistyped one leaves nothing behind. A registration cannot
    be amended, so a grid that was never declared has to be preregistered as a new
    one and run against its own candidate.

    A level already sealed under this run's own attempt id is skipped entirely:
    nothing is started, simulated or sealed there, and the existing digest is
    reported. That is what makes a retry a true no-op, including after a change to
    a provenance option, which would otherwise derive a different bundle and seal a
    second document at that level.

    Every level shares one specification digest, computed from the protocol's own
    candidate rather than typed three times. Three hand-typed digests is exactly
    how the machine that verified 8B1 and 8B2a ended up with ``trial-1`` counting
    two effective specifications: the ledger faithfully recorded what it was told,
    and nothing checked it. Here there is nothing to mistype -- and the report
    now compares each level's declared digest against the registration, so the
    drift is caught even for a grid sealed entirely outside this command.
    """

    def operation() -> dict[str, JsonValue]:
        parsed = _load_json_model(protocol, TrialProtocol)
        # No ``candidate_for``: ``TrialProtocol`` has no such method, and its
        # candidates are a tuple of ``TrialSpec`` reached by comprehension. An
        # unknown ``--trial-id`` is a ``ConfigurationError``, not a bare
        # ``StopIteration`` the catch-all would answer with a correlation id.
        spec_sha256 = canonical_sha256(_declared_candidate(parsed, trial_id))
        ledger = _trial_ledger()
        store = _evidence_store()
        grid = declared_grid(parsed)
        attempts = {m: f"{attempt_prefix}-{m}" for m in grid}
        run = _RunInputs(
            parsed=parsed,
            trial_id=trial_id,
            spec_sha256=spec_sha256,
            ledger=ledger,
            store=store,
            constitution=_constitution(),
            instrument_contract=_instrument_contract(contract, strategy_id=parsed.strategy_id),
            started_at=started_at,
            occurred_at=occurred_at,
            registered_at=registered_at,
            agent_run_id=agent_run_id,
            exit_policy=exit_policy,
            firm_equity=firm_equity,
            atr_period=atr_period,
            spread_window=spread_window,
            defective_bar_tolerance=defective_bar_tolerance,
        )
        digests = _seal_levels(
            run, sizing=SizingMode.CONSTANT_NOTIONAL, attempts=attempts, unsealed_retry=True
        )
        scenarios: list[JsonValue] = [
            {
                "multiplier": str(multiplier),
                "attempt_id": attempts[multiplier],
                "evidence_sha256": digests[multiplier],
            }
            for multiplier in grid
        ]
        return {
            "trial_id": trial_id,
            "scenarios": scenarios,
            "report": cast(
                JsonValue,
                json.loads(_scenario_report_for(trial_id, ledger, store).model_dump_json()),
            ),
        }

    _run(operation)


def _compounding_report_for(
    trial_id: str, ledger: PostgresTrialLedger, store: EvidenceStore
) -> CompoundingReport:
    """The trial's compounding rerun beside its 1.0x constant-notional baseline."""

    sealed = sealed_bundles(ledger.events_for(trial_id), store.read)
    return compounding_report(
        trial_id=trial_id,
        protocol=registered_protocol(ledger.replay(), trial_id),
        constant=sealed_baseline(sealed, trial_id),
        compounding=sealed_rerun(sealed, trial_id),
    )


@trial_app.command("compounding")
def research_trial_compounding(
    protocol: Annotated[
        Path,
        typer.Option(
            "--protocol",
            help="Frozen TrialProtocol JSON. The costs, window and strategy come from here.",
        ),
    ],
    trial_id: Annotated[str, typer.Option("--trial-id")],
    attempt_id: Annotated[str, typer.Option("--attempt-id", help="The one attempt this run is.")],
    started_at: Annotated[datetime, typer.Option("--started-at")],
    occurred_at: Annotated[datetime, typer.Option("--occurred-at")],
    registered_at: Annotated[datetime, typer.Option("--registered-at")],
    agent_run_id: Annotated[str, typer.Option("--agent-run-id")],
    exit_policy: Annotated[ExitPolicyName, typer.Option("--exit-policy")],
    firm_equity: Annotated[str, typer.Option("--firm-equity")],
    contract: Annotated[Path, typer.Option("--contract")],
    atr_period: Annotated[int, typer.Option("--atr-period", min=1)],
    spread_window: Annotated[int, typer.Option("--spread-window", min=1)],
    defective_bar_tolerance: Annotated[str, typer.Option("--defective-bar-tolerance")] = "0",
) -> None:
    """Rerun the candidate once with equity-based sizing, seal it, and compare it.

    Runs at the protocol's baseline costs as one attempt, then prints the sealed
    constant-notional baseline beside it, with the capacity state.

    Like ``scenarios`` it checks before the first write: the ``--protocol`` file is
    the registered one, the options are valid, the strategy in code is the one the
    protocol declares, a 1.0x constant-notional baseline is sealed, the stored bars of
    the window hash to the protocol's declared dataset hash, and the run's replay inputs
    equal that baseline's. It refuses an ``--attempt-id`` the trial
    has started and not sealed as this run's own rerun; an id already sealed as
    this run's rerun is a no-op that reports the existing digest.

    A run that stops after its start row (a simulation refusal, a ruined account)
    leaves that row behind and its attempt id is spent. Retry under a NEW
    ``--attempt-id``; ``audit_attempts`` rises by one more.
    """

    # Like ``scenarios``: no option for any value the report compares against the
    # protocol (no window, strategy or cost option). Kept out of the docstring
    # because that is the help text.
    #
    # Why a started id is spent rather than resumed (policy, deliberately strict):
    # a start row is derived from the trial and the attempt id alone, so resuming
    # it would append nothing and the audit count would not rise for a run that
    # happened. ``scenarios`` resumes its own levels only because each level is a
    # distinct id it computed itself.
    def operation() -> dict[str, JsonValue]:
        parsed = _load_json_model(protocol, TrialProtocol)
        spec_sha256 = canonical_sha256(_declared_candidate(parsed, trial_id))
        ledger = _trial_ledger()
        store = _evidence_store()
        run = _RunInputs(
            parsed=parsed,
            trial_id=trial_id,
            spec_sha256=spec_sha256,
            ledger=ledger,
            store=store,
            constitution=_constitution(),
            instrument_contract=_instrument_contract(contract, strategy_id=parsed.strategy_id),
            started_at=started_at,
            occurred_at=occurred_at,
            registered_at=registered_at,
            agent_run_id=agent_run_id,
            exit_policy=exit_policy,
            firm_equity=firm_equity,
            atr_period=atr_period,
            spread_window=spread_window,
            defective_bar_tolerance=defective_bar_tolerance,
        )
        digest = _seal_levels(
            run,
            sizing=SizingMode.COMPOUNDING,
            attempts={Decimal(1): attempt_id},
            unsealed_retry=False,
        )[Decimal(1)]
        return {
            "trial_id": trial_id,
            "attempt_id": attempt_id,
            "evidence_sha256": digest,
            "report": cast(
                JsonValue,
                json.loads(_compounding_report_for(trial_id, ledger, store).model_dump_json()),
            ),
            "capacity": cast(JsonValue, json.loads(capacity_diagnostic(parsed).model_dump_json())),
        }

    _run(operation)


@trial_app.command("compounding-report")
def research_trial_compounding_report(
    trial_id: Annotated[str, typer.Option("--trial-id")],
) -> None:
    """Compare one candidate's sealed compounding rerun with its sealed baseline."""

    def operation() -> dict[str, JsonValue]:
        report = _compounding_report_for(trial_id, _trial_ledger(), _evidence_store())
        return cast(dict[str, JsonValue], json.loads(report.model_dump_json()))

    _run(operation)


@trial_app.command("capacity")
def research_trial_capacity(
    trial_id: Annotated[str, typer.Option("--trial-id")],
) -> None:
    """State what can be said about capacity for one registered candidate."""

    def operation() -> dict[str, JsonValue]:
        protocol = registered_protocol(_trial_ledger().replay(), trial_id)
        # Nested: the diagnostic's own ``status`` would otherwise replace the
        # envelope's ``status`` key.
        diagnostic = json.loads(capacity_diagnostic(protocol).model_dump_json())
        return {"capacity": cast(JsonValue, diagnostic)}

    _run(operation)


def _splits_report_for(
    trial_id: str, ledger: PostgresTrialLedger, store: EvidenceStore
) -> SplitsReport:
    """The trial's one sealed 1.0x constant-notional run, cut as its protocol declares."""

    sealed = sealed_bundles(ledger.events_for(trial_id), store.read)
    return splits_report(
        trial_id=trial_id,
        protocol=registered_protocol(ledger.replay(), trial_id),
        sealed=sealed_baseline(sealed, trial_id),
    )


@trial_app.command("splits")
def research_trial_splits(
    trial_id: Annotated[str, typer.Option("--trial-id")],
) -> None:
    """Print the walk-forward folds and the CPCV folds, splits and paths of one candidate.

    Reads the trial's one sealed constant-notional 1.0x run and the validation policy
    its registered protocol declared. The folds are calendar arithmetic over the run's
    daily series; a series too short for one walk-forward fold is reported as undefined,
    with the reason. Each CPCV path also states how many closed trades its test samples
    kept and dropped under the declared purge. The counts do not depend on the embargo.

    Read only. It computes no statistic and states no decision about the candidate.
    """

    def operation() -> dict[str, JsonValue]:
        report = _splits_report_for(trial_id, _trial_ledger(), _evidence_store())
        return cast(dict[str, JsonValue], json.loads(report.model_dump_json()))

    _run(operation)


@trial_app.command("validate")
def research_trial_validate(
    trial_id: Annotated[str, typer.Option("--trial-id")],
) -> None:
    """Print every statistical measurement of one registered candidate's sealed evidence.

    Reads the trial's one sealed constant-notional 1.0x run, its compounding run and its
    1.5x and 2.0x runs where they are sealed, the other candidates of its protocol, and
    the counters of the chain. Each measurement is a finite value or the reason there is
    none, and names the digests of the sealed documents it came from. A level with no
    sealed run is reported as undefined, never as zero. The bootstrap and the Monte Carlo
    are seeded from the chain's own identifiers, so a second read prints the same bytes.

    Read only. It states no decision about the candidate: what a figure means for the
    candidate is for a later step to say.
    """

    def operation() -> dict[str, JsonValue]:
        inputs = read_validation_inputs(trial_id, _trial_ledger(), _evidence_store())
        return cast(
            dict[str, JsonValue], json.loads(statistical_evidence(inputs).model_dump_json())
        )

    _run(operation)


@trial_app.command("scenario-report")
def research_trial_scenario_report(
    trial_id: Annotated[str, typer.Option("--trial-id")],
) -> None:
    """Check one candidate's sealed scenarios against its declared grid, and report.

    The grid is read from the ``PREREGISTERED`` event in the chain, not from a
    file, so what is checked is what was sealed before any result existed.

    Reports; does not judge. Nothing here says what to do with a candidate's
    degradation: that is 8D's question, and 8D fixes its own limits before it
    looks at any of these numbers.
    """

    def operation() -> dict[str, JsonValue]:
        report = _scenario_report_for(trial_id, _trial_ledger(), _evidence_store())
        return cast(dict[str, JsonValue], json.loads(report.model_dump_json()))

    payload = _execute(operation)
    _emit(payload)


@trial_app.command("decide")
def research_trial_decide(
    trial_id: Annotated[str, typer.Option("--trial-id")],
    occurred_at: Annotated[
        datetime,
        typer.Option(
            "--occurred-at",
            help=(
                "Declared time of the decision, UTC (e.g. 2026-03-01T12:00:00). Required, "
                "so that a retry names the same value; a rerun on an unchanged "
                "chain appends nothing whatever value it is given."
            ),
        ),
    ],
) -> None:
    """Evaluate the nine gates for one declared trial, seal the report and record the decision.

    Reads a prospective trial's statistical evidence as ``validate`` does, or a legacy
    trial's sealed bundle, and derives the holdout state from the chain. The nine gates
    pass, fail or are unavailable against thresholds taken from the signed policy and the
    protocol's own validation spec; none is an option here. The decision is REJECTED,
    RESEARCH_PASSED or PAPER_APPROVED: REJECTED whenever a blocking reason stands,
    RESEARCH_PASSED only for a locked, unopened holdout and eight passing research gates.

    Seals the report, then appends ``VALIDATED`` and ``GATE_DECIDED`` naming its digest.
    A rerun on an unchanged chain appends nothing; evidence sealed for any trial since
    makes it seal a new report. It creates no package, changes no
    stage and takes no stage option. A policy that moved since the trial's first report
    is refused (exit 21), as is an undeclared trial (exit 19).

    While the holdout is opened or consumed it also reads the opened 1.5x and 2.0x bundles
    for gate 2, and refuses (exit 21, nothing written) unless each is sealed exactly once as
    the protocol's candidate on the holdout window. Any decision recorded after an opening
    consumes the holdout.
    """

    def operation() -> dict[str, JsonValue]:
        outcome = decide_trial(
            trial_id,
            ledger=_trial_ledger(),
            store=_evidence_store(),
            occurred_at=_as_utc(occurred_at),
        )
        return cast(dict[str, JsonValue], json.loads(outcome.model_dump_json()))

    _run(operation)


@trial_app.command("report")
def research_trial_report(
    trial_id: Annotated[str, typer.Option("--trial-id")],
) -> None:
    """Print the validation report the trial's last recorded decision names.

    Read only. The report is re-read from the evidence store through its digest, and a
    decision the chain holds that is not the report's own is refused as an integrity
    failure (exit 17). A trial with no recorded decision is refused (exit 21).
    """

    def operation() -> dict[str, JsonValue]:
        digest, report = latest_report(trial_id, _trial_ledger(), _evidence_store())
        return {
            "report_sha256": digest,
            "report": cast(JsonValue, json.loads(report.model_dump_json())),
        }

    _run(operation)


@trial_app.command("holdout")
def research_trial_holdout(
    trial_id: Annotated[str, typer.Option("--trial-id")],
) -> None:
    """Print the holdout state derived from the chain for one declared trial.

    Read only and never stored: legacy evidence is contaminated, a protocol's own
    holdout state gives not defined or locked, one sealed opening gives opened, a decision
    after it consumed, and a second opening contaminated. Nothing here can open a holdout.
    """

    def operation() -> dict[str, JsonValue]:
        status = holdout_status(trial_id, _trial_ledger(), _evidence_store())
        return cast(dict[str, JsonValue], json.loads(status.model_dump_json()))

    _run(operation)


@trial_app.command("open-holdout")
def research_trial_open_holdout(
    protocol: Annotated[
        Path,
        typer.Option(
            "--protocol",
            help=(
                "Frozen TrialProtocol JSON. The holdout window, its costs and its strategy "
                "all come from here; nothing about them is an option."
            ),
        ),
    ],
    trial_id: Annotated[str, typer.Option("--trial-id")],
    attempt_prefix: Annotated[
        str,
        typer.Option(
            "--attempt-prefix",
            help="Attempt ids are this prefix, the word holdout and the multiplier.",
        ),
    ],
    started_at: Annotated[datetime, typer.Option("--started-at")],
    occurred_at: Annotated[datetime, typer.Option("--occurred-at")],
    registered_at: Annotated[datetime, typer.Option("--registered-at")],
    agent_run_id: Annotated[str, typer.Option("--agent-run-id")],
    exit_policy: Annotated[ExitPolicyName, typer.Option("--exit-policy")],
    firm_equity: Annotated[str, typer.Option("--firm-equity")],
    contract: Annotated[Path, typer.Option("--contract")],
    atr_period: Annotated[int, typer.Option("--atr-period", min=1)],
    spread_window: Annotated[int, typer.Option("--spread-window", min=1)],
    defective_bar_tolerance: Annotated[str, typer.Option("--defective-bar-tolerance")] = "0",
) -> None:
    """Open the locked holdout ONCE: run the cost grid over it and seal three opened bundles.

    Allowed only when the derived holdout is locked and the trial's latest decision is
    RESEARCH_PASSED under the policy in force. Runs the protocol's grid (1.0x, 1.5x, 2.0x of
    its baseline costs) over the protocol's holdout window as three attempts, and seals each
    bundle with its provenance holdout state opened and the dataset digest its own run
    computed over the stored holdout bars (which must equal the protocol's declared holdout
    dataset hash, checked before the first write). The window and the costs are not options.
    The next step is ``decide``, which reads the opened 1.5x and 2.0x bundles for gate 2
    and, by recording any decision after the opening, consumes the holdout.

    Everything that needs no simulation is refused before anything is written: a holdout
    that is not locked (a second opening included), a latest decision other than
    RESEARCH_PASSED, a window outside the stored bars, stored bars that do not hash to the
    declared holdout dataset hash, a ``--protocol`` file that is not the registered one, an
    invalid option, and a run whose replay inputs differ from the research baseline's.
    A run that stops after its FIRST SEALED bundle leaves a partial opening, which is final:
    ``decide`` refuses until both stressed bundles exist, and the opening cannot be
    repeated. A simulator refusal at 1.0x seals nothing: the holdout stays locked and a new
    ``--attempt-prefix`` can retry.
    """

    def operation() -> dict[str, JsonValue]:
        parsed = _load_json_model(protocol, TrialProtocol)
        spec_sha256 = canonical_sha256(_declared_candidate(parsed, trial_id))
        ledger = _trial_ledger()
        store = _evidence_store()
        registered = registered_protocol(ledger.replay(), trial_id)
        refuse_unopenable(trial_id, registered, ledger=ledger, store=store)
        start, end = declared_window(registered, opened=True)
        scope = strategy_scope(registered.strategy_id)
        refuse_outside_coverage(
            start, end, _bar_store().coverage(scope.instrument_id, Timeframe(scope.timeframe))
        )
        grid = declared_grid(parsed)
        attempts = {m: f"{attempt_prefix}-holdout-{m}" for m in grid}
        run = _RunInputs(
            parsed=parsed,
            trial_id=trial_id,
            spec_sha256=spec_sha256,
            ledger=ledger,
            store=store,
            constitution=_constitution(),
            instrument_contract=_instrument_contract(contract, strategy_id=parsed.strategy_id),
            started_at=started_at,
            occurred_at=occurred_at,
            registered_at=registered_at,
            agent_run_id=agent_run_id,
            exit_policy=exit_policy,
            firm_equity=firm_equity,
            atr_period=atr_period,
            spread_window=spread_window,
            defective_bar_tolerance=defective_bar_tolerance,
            opening=True,
        )
        digests = _seal_levels(
            run, sizing=SizingMode.CONSTANT_NOTIONAL, attempts=attempts, unsealed_retry=False
        )
        bundles: list[JsonValue] = [
            {
                "multiplier": str(multiplier),
                "attempt_id": attempts[multiplier],
                "evidence_sha256": digests[multiplier],
            }
            for multiplier in grid
        ]
        return {
            "trial_id": trial_id,
            "bundles": bundles,
            "holdout": cast(
                JsonValue,
                json.loads(holdout_status(trial_id, ledger, store).model_dump_json()),
            ),
        }

    _run(operation)


@trial_app.command("verify")
def research_trial_verify() -> None:
    """Verify the event chain, then re-read every file the chain points at.

    Two checks, and the second is the one a hash chain cannot do for itself: the
    hashes prove the events were not rewritten, and only reading the sealed
    bundles and validation reports proves the documents they name still exist and
    still hash to their own names. A chain failure is reported rather than raised,
    because an operator asking "is this intact?" needs a plain reason, not a
    stack trace.
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
        events = ledger.replay()
        for event in events:
            # A broken chain's events are not evidence of anything, which is why
            # this loop only runs once the report says the chain is intact.
            if isinstance(event.payload, (EvidenceSealedPayload, LegacyImportedPayload)):
                evidence.verify(event.payload.evidence_sha256)
        # The second document kind: every report a VALIDATED or GATE_DECIDED event names.
        verify_reports(events, evidence)
        return {"valid": True, "checked_events": report.checked_events, "reason": None}

    payload = _execute(operation)
    _emit(payload, status="ok" if payload["valid"] else "invalid")
    if not payload["valid"]:
        # Deliberately not ``_fail``: the integrity report is the answer to
        # "is this intact?" and is written to stdout so it can be piped into a
        # reporter, while the exit code stays the signal a script branches on.
        raise typer.Exit(code=int(ExitCode.TRIAL_LEDGER_INTEGRITY))


class PackageStageName(StrEnum):
    """The stages a package can be requested at. ``SANDBOX`` is not one: a decision alone
    yields it, and a command that wrote it would grant nothing."""

    PAPER = "paper"
    LIVE = "live"


_PACKAGE_STAGES = {
    PackageStageName.PAPER: PromotionStage.PAPER,
    PackageStageName.LIVE: PromotionStage.LIVE,
}


def _read_package(path: Path) -> StrategyPackage:
    return _load_json_model(path, StrategyPackage)


@package_app.command("create")
def research_package_create(
    trial_id: Annotated[str, typer.Option("--trial-id")],
    stage: Annotated[PackageStageName, typer.Option("--stage", case_sensitive=False)],
    authorization_ref: Annotated[
        str,
        typer.Option(
            "--authorization-ref",
            help="A reference to a person's paper authorization. Never checked, only recorded.",
        ),
    ],
    book: Annotated[str, typer.Option("--book", help="The book id the strategy belongs to.")],
    horizon: Annotated[Horizon, typer.Option("--horizon")],
    asset_class: Annotated[
        list[AssetClass],
        typer.Option("--asset-class", help="An asset class the strategy trades; repeatable."),
    ],
    out: Annotated[
        Path, typer.Option("--out", help="The package file to write. Never overwritten.")
    ],
    capital_authorization_ref: Annotated[
        str | None,
        typer.Option(
            "--capital-authorization-ref",
            help="LIVE only: a separate capital authorization reference, not the paper one.",
        ),
    ] = None,
    signature_sha256: Annotated[
        str | None,
        typer.Option("--signature-sha256", help="LIVE only: a reference to a person's signature."),
    ] = None,
    paper_package: Annotated[
        Path | None,
        typer.Option("--paper-package", help="LIVE only: the PAPER package file it continues."),
    ] = None,
) -> None:
    """Write a strategy package for one trial's latest recorded decision, or refuse.

    Reads the chain and writes ONE JSON file; it appends nothing to the ledger, changes no
    stage and signs nothing. PAPER needs a latest decision of PAPER_APPROVED and an
    authorization reference. LIVE needs the PAPER package it continues, a signature
    reference, and a capital authorization reference that is not the paper one: a decision
    never reaches LIVE by itself. The authorization and the signature are references; nothing
    here checks that a person made either. The specification's id and hypothesis come from
    the declared candidate; book, horizon and asset classes are options. A refused request
    (exit 21) writes no file, and an existing ``--out`` is never overwritten.
    """

    def operation() -> dict[str, JsonValue]:
        package = package_from_chain(
            trial_id,
            ledger=_trial_ledger(),
            store=_evidence_store(),
            stage=_PACKAGE_STAGES[stage],
            book=book,
            horizon=horizon,
            asset_classes=asset_class,
            authorization_ref=authorization_ref,
            capital_authorization_ref=capital_authorization_ref,
            signature_sha256=signature_sha256,
            paper_package=None if paper_package is None else _read_package(paper_package),
        )
        try:
            # Exclusive create: an existing ``--out`` is refused, never overwritten.
            with out.open("x", encoding="utf-8") as stream:
                stream.write(package.model_dump_json())
        except OSError as error:
            raise ConfigurationError() from error
        return {
            "package_id": package.package_id,
            "stage": package.stage.value,
            "out": str(out),
            "package_sha256": canonical_sha256(package),
        }

    _run(operation)


@package_app.command("verify")
def research_package_verify(
    file: Annotated[Path, typer.Option("--file", help="The package file to check.")],
    paper_package: Annotated[
        Path | None,
        typer.Option("--paper-package", help="LIVE only: the PAPER package it continues."),
    ] = None,
) -> None:
    """Re-check a package file against the chain. Read only: appends nothing and signs nothing.

    The report digest the package names must be a readable report (an altered or missing
    one exits 17), a ``VALIDATED`` event must name it, the trial's latest recorded decision
    must name it and be PAPER_APPROVED (for PAPER and LIVE), the ledger reference must be
    the hash of a row the chain holds, the source hash must be the protocol's strategy hash,
    the specification must be the declared candidate's, and the stage rules must hold. Any
    failure is exit 21. A LIVE package is checked against the PAPER package it continues.
    Authorization and signature references are only checked for presence and distinctness.
    """

    def operation() -> dict[str, JsonValue]:
        verified = verify_package(
            _read_package(file),
            ledger=_trial_ledger(),
            store=_evidence_store(),
            paper_package=None if paper_package is None else _read_package(paper_package),
        )
        return {"valid": True, **cast(dict[str, JsonValue], json.loads(verified.model_dump_json()))}

    _run(operation)


if __name__ == "__main__":  # pragma: no cover
    # ``python -m trading_house.cli`` is how the determinism test starts a run
    # in a fresh interpreter under its own PYTHONHASHSEED. Without this the
    # module imports and exits silently, and the test compares two empty
    # strings -- which would pass.
    app()
