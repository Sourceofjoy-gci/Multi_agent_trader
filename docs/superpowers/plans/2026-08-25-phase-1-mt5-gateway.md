# Phase 1 MT5 Gateway Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a read-only MetaTrader 5 gateway that connects to a demo terminal, describes instruments in venue-neutral terms, snapshots quotes, reconciles positions, and reports health — sending zero orders.

**Architecture:** Exactly one module imports `MetaTrader5`. A single-threaded actor owns every `mt5.*` call behind a priority queue. All logic worth testing — symbol mapping, retcode classification, magic derivation — lives in pure functions that take plain dataclasses, so they run on Linux CI without a terminal.

**Tech Stack:** Python 3.12, MetaTrader5 (Windows-only), Pydantic v2, pytest, Hypothesis, Ruff, mypy strict, uv.

**Spec:** `docs/superpowers/specs/2026-08-25-phase-1-mt5-gateway-design.md`

## Global Constraints

- **This phase sends no orders.** `submit`, `amend_protection` and `close` raise `NotImplementedError` with a message naming Phase 3. Any code path that could mutate broker state is a defect.
- **Exactly one module may `import MetaTrader5`:** `src/trading_house/brokers/mt5/terminal.py`. Enforced by `tests/acceptance/test_architecture.py`.
- `MetaTrader5` is a platform-conditional dependency: `"MetaTrader5>=5.0.45,<6 ; sys_platform == 'win32'"`. It cannot be imported on Linux, so nothing outside `terminal.py` may import it even transitively.
- **`brokers/mt5/__init__.py` must be import-safe on Linux** — no eager import of `terminal.py`.
- Every quantity and price is `Decimal`. Never `float`. MT5 returns floats; convert with `Decimal(str(value))`, never `Decimal(value)`.
- All distances are in **price units**, never broker "points". `stops_level` and `freeze_level` are in points and must be multiplied by `point`.
- Every timestamp crossing out of `terminal.py` is timezone-aware UTC. MT5 returns broker-server epoch seconds; convert at that boundary (invariant I-10).
- Canonical models stay `strict=True`, `frozen=True`, `extra="forbid"`.
- Line length 100. Enums written `class X(str, Enum):  # noqa: UP042`. mypy strict, no new `# type: ignore`.
- `__init__.py` re-exports use an explicit `__all__` list (`--no-implicit-reexport`).
- Coverage gate is `--cov-fail-under=95`. `terminal.py` is omitted from coverage and capped at 80 statements by test.
- Every task ends green on `uv run ruff format --check .`, `uv run ruff check .`, `uv run mypy`, and its own tests.
- Run `uv run ruff format .` before committing. Ruff excludes `*.md`; do not remove that exclusion.
- Invariants I-1 through I-16 must all still hold. The existing acceptance suites must stay green.

## Safety Constraints

- **The terminal must be pointed at a DEMO account.** The gateway reads `account_info().trade_mode` inside `start()` and raises `NonDemoAccountError` unless it reports demo, before serving any request.
- Live-terminal tests carry `@pytest.mark.mt5` and skip when no terminal is present. They are never run in CI.
- If a task needs a live terminal and none is available, **stop and report** rather than weakening a test to make it pass.

## File Structure

| File | Responsibility |
|---|---|
| `src/trading_house/brokers/mt5/__init__.py` | Import-safe package entry; lazy `__getattr__` |
| `src/trading_house/brokers/mt5/boundary.py` | **Pure.** Plain DTOs mirroring MT5 returns, MT5 integer constants, and the `TerminalPort` protocol |
| `src/trading_house/brokers/mt5/terminal.py` | **The only module importing `MetaTrader5`.** Implements `TerminalPort`, converts MT5 named tuples to DTOs, converts server time to UTC |
| `src/trading_house/brokers/mt5/retcodes.py` | **Pure.** Retcode → `RejectReason` |
| `src/trading_house/brokers/mt5/magic.py` | **Pure.** `intent_id` + book range → magic |
| `src/trading_house/brokers/mt5/contracts.py` | **Pure.** `Mt5SymbolInfo` → `InstrumentContract` |
| `src/trading_house/brokers/mt5/gateway.py` | The actor: thread, priority queue, heartbeat, `GatewayMetrics`, demo guard |
| `src/trading_house/brokers/mt5/adapter.py` | `Mt5BrokerAdapter` implementing `BrokerAdapter` |
| `src/trading_house/core/errors.py` | **Modify.** `+BrokerUnavailableError`, `+NonDemoAccountError`, `+ExitCode.BROKER/ACCOUNT_MODE` |
| `src/trading_house/core/venue.py` | **Modify.** `+RejectReason.UNKNOWN` and its `REJECT_CLASS` entry |
| `src/trading_house/cli.py` | **Modify.** `+EXIT_CODES` entries |
| `src/trading_house/ops/health.py` | **Modify.** Fifth step — `BookReconciler` |
| `scripts/broker_audit.py` | Read-only broker capability audit |
| `pyproject.toml` | **Modify.** Conditional dependency, `mt5` marker, coverage omit |
| `tests/acceptance/test_architecture.py` | **Modify.** Narrow the ban to a single-file exemption |
| `tests/acceptance/test_phase0.py` | **Modify.** Scope its ban to the Phase 0 packages |

Design note: `boundary.py` holds both the DTOs and the `TerminalPort` protocol because together they *are* one thing — the contract describing what the gateway needs from a terminal, expressed without importing `MetaTrader5`. Splitting them would separate a protocol from the types it speaks in.

---

### Task 1: Contract Additions

**Files:**
- Modify: `src/trading_house/core/venue.py`
- Modify: `src/trading_house/core/errors.py`
- Modify: `src/trading_house/cli.py`
- Test: `tests/unit/core/test_venue.py`, `tests/unit/core/test_errors.py`, `tests/unit/test_cli.py`

**Interfaces:**
- Produces: `RejectReason.UNKNOWN`, `BrokerUnavailableError`, `NonDemoAccountError`, `ExitCode.BROKER = 8`, `ExitCode.ACCOUNT_MODE = 9`. Every later task depends on these.
- Consumes: nothing new.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/core/test_venue.py — append
def test_unknown_reject_reason_fails_closed_to_safe_mode() -> None:
    """An unclassifiable broker response must not be guessed as retryable."""

    assert REJECT_CLASS[RejectReason.UNKNOWN] is RejectClass.AUTHORITY
    assert recovery_for(RejectReason.UNKNOWN) is RecoveryAction.ENTER_SAFE_MODE
```

```python
# tests/unit/core/test_errors.py — append
from trading_house.core.errors import (
    BrokerUnavailableError,
    ExitCode,
    NonDemoAccountError,
)


def test_broker_errors_carry_fixed_public_messages() -> None:
    assert str(BrokerUnavailableError()) == "broker terminal unavailable"
    assert str(NonDemoAccountError()) == "refusing to operate a non-demo account"


def test_broker_exit_codes_are_distinct_and_new() -> None:
    assert ExitCode.BROKER == 8
    assert ExitCode.ACCOUNT_MODE == 9
    assert len({member.value for member in ExitCode}) == len(list(ExitCode))
```

```python
# tests/unit/test_cli.py — append
def test_broker_errors_have_stable_exit_codes() -> None:
    from trading_house.core.errors import BrokerUnavailableError, NonDemoAccountError

    assert cli.EXIT_CODES[BrokerUnavailableError] == cli.ExitCode.BROKER
    assert cli.EXIT_CODES[NonDemoAccountError] == cli.ExitCode.ACCOUNT_MODE
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/core/test_venue.py tests/unit/core/test_errors.py tests/unit/test_cli.py -q --no-cov`

Expected: FAIL — `RejectReason` has no `UNKNOWN`, and the two error classes do not exist.

Note the existing `test_every_typed_error_has_a_stable_exit_code` in `tests/unit/test_cli.py` will also fail once the errors exist but before `EXIT_CODES` is updated. That is the guard working; satisfy it in Step 3.

- [ ] **Step 3: Add the contract members**

In `src/trading_house/core/venue.py`, append to `RejectReason`:

```python
    UNKNOWN = "unknown"
```

and to `REJECT_CLASS`:

```python
    RejectReason.UNKNOWN: RejectClass.AUTHORITY,
```

In `src/trading_house/core/errors.py`, append to `ExitCode`:

```python
    BROKER = 8
    ACCOUNT_MODE = 9
```

and add the two errors, matching the file's existing shape exactly:

```python
class BrokerUnavailableError(TradingHouseError):
    """Raised when the broker terminal cannot be reached or initialised."""

    public_message = "broker terminal unavailable"


class NonDemoAccountError(TradingHouseError):
    """Raised when the connected account is not a demo account."""

    public_message = "refusing to operate a non-demo account"
```

In `src/trading_house/cli.py`, add to `EXIT_CODES`:

```python
    BrokerUnavailableError: ExitCode.BROKER,
    NonDemoAccountError: ExitCode.ACCOUNT_MODE,
```

and import both from `trading_house.core.errors`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit tests/property tests/acceptance -q --no-cov`

Expected: PASS. `REJECT_CLASS`'s totality test and the CLI's exit-code totality test both now cover the new members.

- [ ] **Step 5: Verify the static gates**

Run: `uv run ruff format . && uv run ruff format --check . && uv run ruff check . && uv run mypy`

Expected: all exit `0`.

- [ ] **Step 6: Commit**

```bash
git add src/trading_house/core src/trading_house/cli.py tests
git commit -m "feat: classify unknown broker responses as fail-closed"
```

---

### Task 2: Broker Capability Audit

Runs early because its findings feed the contract-mapping tests in Task 6 with real values instead of invented ones.

**Files:**
- Create: `scripts/broker_audit.py`
- Create: `tests/unit/scripts/test_broker_audit.py`
- Create (from a live run): `docs/broker-audit-fbs-demo.md`

**Interfaces:**
- Produces: `format_audit_report(rows: Sequence[AuditRow]) -> str` and the `AuditRow` dataclass, both pure and testable without a terminal.
- Consumes: nothing from earlier tasks. `scripts/` sits outside `src/`, so the import ban does not apply and this script may import `MetaTrader5` directly.

- [ ] **Step 1: Write the failing test for the pure formatting**

```python
# tests/unit/scripts/test_broker_audit.py
import importlib.util
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _load_module():  # noqa: ANN202
    spec = importlib.util.spec_from_file_location(
        "broker_audit", PROJECT_ROOT / "scripts" / "broker_audit.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_report_renders_every_field_the_spec_requires() -> None:
    """Spec 17.2 names the exact fields that decide strategy feasibility."""

    module = _load_module()
    row = module.AuditRow(
        server_symbol="EURUSD",
        trade_mode=4,
        trade_exemode=2,
        filling_mode=2,
        trade_stops_level=0,
        trade_freeze_level=0,
        volume_min=0.01,
        volume_step=0.01,
        volume_max=200.0,
        trade_tick_value_loss=1.0,
        trade_tick_size=1e-05,
        digits=5,
        swap_long=-0.5,
        swap_short=0.2,
        median_spread_points=8.0,
    )

    report = module.format_audit_report([row])

    for token in (
        "EURUSD", "trade_mode", "trade_exemode", "filling_mode",
        "trade_stops_level", "trade_freeze_level", "volume_min",
        "volume_step", "volume_max", "trade_tick_value_loss",
        "swap_long", "swap_short", "median_spread_points",
    ):
        assert token in report


def test_report_flags_a_zero_stops_level() -> None:
    """stops_level == 0 is the case that breaks a PositiveDecimal contract."""

    module = _load_module()
    row = module.AuditRow(
        server_symbol="EURUSD", trade_mode=4, trade_exemode=2, filling_mode=2,
        trade_stops_level=0, trade_freeze_level=0, volume_min=0.01,
        volume_step=0.01, volume_max=200.0, trade_tick_value_loss=1.0,
        trade_tick_size=1e-05, digits=5, swap_long=0.0, swap_short=0.0,
        median_spread_points=8.0,
    )

    assert "zero stops_level" in module.format_audit_report([row]).lower()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/unit/scripts/test_broker_audit.py -q --no-cov`

Expected: FAIL — `scripts/broker_audit.py` does not exist.

- [ ] **Step 3: Write the script**

```python
# scripts/broker_audit.py
"""Dump every broker capability that decides which strategies are implementable.

Read-only. Connects to a DEMO terminal, reads symbol and account metadata,
and writes a Markdown report. Sends no orders and changes nothing.

Usage:  uv run python scripts/broker_audit.py EURUSD XAUUSD > docs/broker-audit.md
"""

from __future__ import annotations

import statistics
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, fields

SPREAD_SAMPLES = 30
SPREAD_INTERVAL_SECONDS = 0.2
ACCOUNT_TRADE_MODE_DEMO = 0


@dataclass(frozen=True, slots=True)
class AuditRow:
    server_symbol: str
    trade_mode: int
    trade_exemode: int
    filling_mode: int
    trade_stops_level: int
    trade_freeze_level: int
    volume_min: float
    volume_step: float
    volume_max: float
    trade_tick_value_loss: float
    trade_tick_size: float
    digits: int
    swap_long: float
    swap_short: float
    median_spread_points: float


def format_audit_report(rows: Sequence[AuditRow]) -> str:
    """Render the audit as Markdown, flagging values that constrain the design."""

    lines = ["# Broker capability audit", ""]
    for row in rows:
        lines.append(f"## {row.server_symbol}")
        lines.append("")
        for field in fields(row):
            if field.name == "server_symbol":
                continue
            lines.append(f"- `{field.name}`: {getattr(row, field.name)}")
        notes: list[str] = []
        if row.trade_stops_level == 0:
            notes.append(
                "zero stops_level — min_stop_distance floors to one price increment"
            )
        if row.trade_mode != 4:
            notes.append(f"trade_mode is {row.trade_mode}, not FULL — restricted trading")
        if row.filling_mode == 0:
            notes.append("no FOK/IOC filling bits — RETURN only")
        if notes:
            lines.append("")
            lines.append("**Notes:**")
            lines.extend(f"- {note}" for note in notes)
        lines.append("")
    return "\n".join(lines)


def _collect(mt5, server_symbol: str) -> AuditRow:  # noqa: ANN001
    info = mt5.symbol_info(server_symbol)
    if info is None:
        raise SystemExit(f"symbol not found: {server_symbol}")
    spreads: list[float] = []
    for _ in range(SPREAD_SAMPLES):
        tick = mt5.symbol_info_tick(server_symbol)
        if tick is not None and tick.ask > 0 and tick.bid > 0:
            spreads.append((tick.ask - tick.bid) / info.point)
        time.sleep(SPREAD_INTERVAL_SECONDS)
    return AuditRow(
        server_symbol=server_symbol,
        trade_mode=info.trade_mode,
        trade_exemode=info.trade_exemode,
        filling_mode=info.filling_mode,
        trade_stops_level=info.trade_stops_level,
        trade_freeze_level=info.trade_freeze_level,
        volume_min=info.volume_min,
        volume_step=info.volume_step,
        volume_max=info.volume_max,
        trade_tick_value_loss=info.trade_tick_value_loss,
        trade_tick_size=info.trade_tick_size,
        digits=info.digits,
        swap_long=info.swap_long,
        swap_short=info.swap_short,
        median_spread_points=statistics.median(spreads) if spreads else float("nan"),
    )


def main(symbols: Sequence[str]) -> int:
    import MetaTrader5 as mt5  # noqa: PLC0415 — script-local, outside the src ban

    if not mt5.initialize():
        print(f"initialize failed: {mt5.last_error()}", file=sys.stderr)
        return 1
    try:
        account = mt5.account_info()
        if account is None:
            print("no account info", file=sys.stderr)
            return 1
        if account.trade_mode != ACCOUNT_TRADE_MODE_DEMO:
            print(
                f"REFUSING: account trade_mode is {account.trade_mode}, not demo",
                file=sys.stderr,
            )
            return 2
        for symbol in symbols:
            mt5.symbol_select(symbol, True)
        rows = [_collect(mt5, symbol) for symbol in symbols]
    finally:
        mt5.shutdown()
    print(format_audit_report(rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:] or ["EURUSD", "XAUUSD"]))
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/unit/scripts/test_broker_audit.py -q --no-cov`

Expected: PASS, 2 tests. Note the pure formatting is tested with no terminal.

- [ ] **Step 5: Run the audit against the demo terminal**

Requires the FBS MetaTrader 5 terminal running and logged into a **demo** account.

Run: `uv run python scripts/broker_audit.py EURUSD XAUUSD > docs/broker-audit-fbs-demo.md`

Expected: exit `0` and a report naming both symbols. If it exits `2`, the terminal is on a non-demo account — **stop and report; do not proceed**. If no terminal is running, stop and report that Step 5 is blocked; Tasks 3–10 do not depend on it and can proceed, but Task 11's live tests will also be blocked.

- [ ] **Step 6: Verify the static gates**

Run: `uv run ruff format . && uv run ruff format --check . && uv run ruff check . && uv run mypy`

Expected: all exit `0`. Note `mypy` checks the `trading_house` package only, so `scripts/` is not type-checked; ruff still lints it.

- [ ] **Step 7: Commit**

```bash
git add scripts/broker_audit.py tests/unit/scripts docs/broker-audit-fbs-demo.md
git commit -m "feat: audit broker capabilities before trusting any symbol spec"
```

---

### Task 3: The Terminal Boundary

**Files:**
- Create: `src/trading_house/brokers/mt5/__init__.py`
- Create: `src/trading_house/brokers/mt5/boundary.py`
- Test: `tests/unit/brokers/mt5/test_boundary.py`

**Interfaces:**
- Produces: `Mt5SymbolInfo`, `Mt5Tick`, `Mt5Position`, `Mt5CheckResult`, `TerminalPort`, and the MT5 integer constants. Every later task consumes these.
- Consumes: nothing from earlier tasks.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/brokers/mt5/test_boundary.py
from datetime import UTC, datetime

from trading_house.brokers.mt5.boundary import (
    SYMBOL_FILLING_FOK,
    SYMBOL_FILLING_IOC,
    SYMBOL_TRADE_EXECUTION_MARKET,
    SYMBOL_TRADE_MODE_DISABLED,
    SYMBOL_TRADE_MODE_FULL,
    Mt5Position,
    Mt5SymbolInfo,
    Mt5Tick,
    TerminalPort,
)

EXPECTED_PORT_METHODS = {
    "initialize", "shutdown", "account_trade_mode", "server_utc_offset_seconds",
    "symbol_info", "symbol_tick", "positions", "order_check", "last_error",
}


def test_terminal_port_surface_is_exactly_the_calls_the_gateway_needs() -> None:
    """A wide port is a leaky port — every method here must reach real IPC."""

    actual = {name for name in vars(TerminalPort) if not name.startswith("_")}
    assert actual == EXPECTED_PORT_METHODS


def test_port_is_a_runtime_checkable_protocol() -> None:
    assert getattr(TerminalPort, "_is_protocol", False) is True


def test_dtos_are_frozen() -> None:
    tick = Mt5Tick(bid=1.1, ask=1.2, observed_at=datetime(2026, 8, 25, 9, 0, tzinfo=UTC))
    try:
        tick.bid = 2.0  # type: ignore[misc]
    except AttributeError:
        return
    raise AssertionError("Mt5Tick must be frozen")


def test_mt5_constants_match_the_documented_values() -> None:
    """These mirror MetaTrader5's own enums so pure code need not import it."""

    assert SYMBOL_TRADE_MODE_DISABLED == 0
    assert SYMBOL_TRADE_MODE_FULL == 4
    assert SYMBOL_FILLING_FOK == 1
    assert SYMBOL_FILLING_IOC == 2
    assert SYMBOL_TRADE_EXECUTION_MARKET == 2


def test_position_dto_carries_the_magic_used_for_book_attribution() -> None:
    position = Mt5Position(
        ticket=1, magic=110001, server_symbol="EURUSD", volume=0.1,
        price_open=1.1, sl=1.05, tp=None, is_buy=True,
        opened_at=datetime(2026, 8, 25, 9, 0, tzinfo=UTC),
    )
    assert position.magic == 110001


def test_boundary_module_does_not_import_metatrader5() -> None:
    """The whole point: this module must import on Linux."""

    import ast
    from pathlib import Path

    source = Path(
        "src/trading_house/brokers/mt5/boundary.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "MetaTrader5" not in imported


def test_package_imports_on_any_platform() -> None:
    """brokers.mt5 must not eagerly import terminal.py, which needs Windows."""

    import trading_house.brokers.mt5 as package

    assert package is not None
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/unit/brokers/mt5/test_boundary.py -q --no-cov`

Expected: collection ERROR — `No module named 'trading_house.brokers.mt5'`.

- [ ] **Step 3: Implement the boundary**

```python
# src/trading_house/brokers/mt5/boundary.py
"""What the gateway needs from a terminal, expressed without importing MetaTrader5.

These DTOs mirror the fields MetaTrader 5 returns, as plain frozen dataclasses,
so every module except ``terminal.py`` stays importable on Linux and testable
without a broker. The integer constants mirror MetaTrader 5's own enums for the
same reason.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

SYMBOL_TRADE_MODE_DISABLED = 0
SYMBOL_TRADE_MODE_LONGONLY = 1
SYMBOL_TRADE_MODE_SHORTONLY = 2
SYMBOL_TRADE_MODE_CLOSEONLY = 3
SYMBOL_TRADE_MODE_FULL = 4

SYMBOL_FILLING_FOK = 1
SYMBOL_FILLING_IOC = 2

SYMBOL_TRADE_EXECUTION_REQUEST = 0
SYMBOL_TRADE_EXECUTION_INSTANT = 1
SYMBOL_TRADE_EXECUTION_MARKET = 2
SYMBOL_TRADE_EXECUTION_EXCHANGE = 3

ACCOUNT_TRADE_MODE_DEMO = 0
ACCOUNT_TRADE_MODE_CONTEST = 1
ACCOUNT_TRADE_MODE_REAL = 2


@dataclass(frozen=True, slots=True)
class Mt5SymbolInfo:
    """The subset of ``symbol_info()`` that determines tradability and sizing."""

    name: str
    digits: int
    point: float
    trade_tick_size: float
    trade_tick_value_loss: float
    volume_min: float
    volume_step: float
    volume_max: float
    trade_stops_level: int
    trade_freeze_level: int
    trade_mode: int
    trade_exemode: int
    filling_mode: int
    currency_base: str
    currency_profit: str


@dataclass(frozen=True, slots=True)
class Mt5Tick:
    bid: float
    ask: float
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class Mt5Position:
    ticket: int
    magic: int
    server_symbol: str
    volume: float
    price_open: float
    sl: float
    tp: float | None
    is_buy: bool
    opened_at: datetime


@dataclass(frozen=True, slots=True)
class Mt5CheckResult:
    retcode: int
    comment: str


@runtime_checkable
class TerminalPort(Protocol):
    """Every MetaTrader 5 call the gateway makes. Nothing wider."""

    def initialize(self) -> bool: ...
    def shutdown(self) -> None: ...
    def account_trade_mode(self) -> int: ...
    def server_utc_offset_seconds(self) -> int: ...
    def symbol_info(self, server_symbol: str) -> Mt5SymbolInfo | None: ...
    def symbol_tick(self, server_symbol: str) -> Mt5Tick | None: ...
    def positions(self) -> Sequence[Mt5Position]: ...
    def order_check(self, request: Mapping[str, object]) -> Mt5CheckResult | None: ...
    def last_error(self) -> tuple[int, str]: ...
```

```python
# src/trading_house/brokers/mt5/__init__.py
"""MetaTrader 5 gateway.

``terminal.py`` imports ``MetaTrader5``, which exists only on Windows, so it is
never imported eagerly here. Import it lazily through attribute access, and only
on a platform that has it.
"""

from typing import Any

__all__ = ["Mt5BrokerAdapter"]


def __getattr__(name: str) -> Any:
    if name == "Mt5BrokerAdapter":
        from trading_house.brokers.mt5.adapter import Mt5BrokerAdapter

        return Mt5BrokerAdapter
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
```

Note `Mt5BrokerAdapter` does not exist until Task 8. The lazy `__getattr__` means importing the package still succeeds; only attribute access would fail. `test_package_imports_on_any_platform` therefore passes now.

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/unit/brokers/mt5/test_boundary.py -q --no-cov`

Expected: PASS, 7 tests.

- [ ] **Step 5: Verify the static gates and regression**

Run: `uv run ruff format . && uv run ruff format --check . && uv run ruff check . && uv run mypy && uv run pytest tests/unit tests/property tests/acceptance -q --no-cov`

Expected: all exit `0`.

- [ ] **Step 6: Commit**

```bash
git add src/trading_house/brokers/mt5 tests/unit/brokers/mt5
git commit -m "feat: describe the terminal boundary without importing it"
```

---

### Task 4: Retcode Classification

**Files:**
- Create: `src/trading_house/brokers/mt5/retcodes.py`
- Test: `tests/unit/brokers/mt5/test_retcodes.py`

**Interfaces:**
- Produces: `reject_reason_for(retcode: int) -> RejectReason`, `RETCODE_REJECT_REASON`, `SUCCESS_RETCODES`.
- Consumes: `RejectReason` from Task 1.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/brokers/mt5/test_retcodes.py
import pytest

from trading_house.brokers.mt5.retcodes import (
    RETCODE_REJECT_REASON,
    SUCCESS_RETCODES,
    reject_reason_for,
)
from trading_house.core.venue import REJECT_CLASS, RecoveryAction, RejectReason, recovery_for


@pytest.mark.parametrize(
    ("retcode", "expected"),
    [
        (10004, RejectReason.REQUOTE),
        (10014, RejectReason.INVALID_QUANTITY),
        (10016, RejectReason.INVALID_STOPS),
        (10018, RejectReason.MARKET_CLOSED),
        (10019, RejectReason.INSUFFICIENT_FUNDS),
        (10020, RejectReason.PRICE_CHANGED),
        (10024, RejectReason.TIMEOUT),
        (10026, RejectReason.TRADE_DISABLED),
        (10027, RejectReason.TRADE_DISABLED),
        (10030, RejectReason.UNSUPPORTED_FILL),
        (10031, RejectReason.DISCONNECTED),
    ],
)
def test_documented_retcodes_map_to_their_neutral_reason(
    retcode: int, expected: RejectReason
) -> None:
    assert reject_reason_for(retcode) is expected


@pytest.mark.parametrize("retcode", [0, 1, 10015, 10021, 10099, 99999, -1])
def test_unrecognised_retcodes_fail_closed(retcode: int) -> None:
    """Guessing an unknown code as transient would produce a retry loop."""

    assert reject_reason_for(retcode) is RejectReason.UNKNOWN
    assert recovery_for(reject_reason_for(retcode)) is RecoveryAction.ENTER_SAFE_MODE


def test_every_mapped_reason_has_a_recovery_action() -> None:
    for reason in RETCODE_REJECT_REASON.values():
        assert reason in REJECT_CLASS


def test_success_retcodes_are_not_treated_as_rejections() -> None:
    """10008 PLACED and 10009 DONE are successes, not failures to classify."""

    assert SUCCESS_RETCODES == frozenset({10008, 10009})
    assert SUCCESS_RETCODES.isdisjoint(RETCODE_REJECT_REASON)


def test_invalid_price_is_deliberately_unmapped() -> None:
    """10015 has no clean neutral equivalent; mapping it PRICE_CHANGED would
    classify it transient and retry-loop on a genuinely malformed price."""

    assert 10015 not in RETCODE_REJECT_REASON
    assert reject_reason_for(10015) is RejectReason.UNKNOWN
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/unit/brokers/mt5/test_retcodes.py -q --no-cov`

Expected: collection ERROR — `No module named 'trading_house.brokers.mt5.retcodes'`.

- [ ] **Step 3: Implement the classifier**

```python
# src/trading_house/brokers/mt5/retcodes.py
"""Neutral classification of MetaTrader 5 trade return codes.

Unrecognised codes fail closed to ``RejectReason.UNKNOWN``, which is classified
``AUTHORITY`` and therefore enters safe mode. Guessing an unknown code as
transient would let recovery retry it forever.

Codes with no clean neutral equivalent are deliberately absent. ``10015
INVALID_PRICE`` is the clearest: it means the price we sent was malformed,
which is contractual rather than transient, and ``RejectReason`` has no
``INVALID_PRICE`` member. Adding one later is additive and cheap;
mis-classifying it now is a live retry loop.
"""

from trading_house.core.venue import RejectReason

RETCODE_REJECT_REASON: dict[int, RejectReason] = {
    10004: RejectReason.REQUOTE,
    10014: RejectReason.INVALID_QUANTITY,
    10016: RejectReason.INVALID_STOPS,
    10018: RejectReason.MARKET_CLOSED,
    10019: RejectReason.INSUFFICIENT_FUNDS,
    10020: RejectReason.PRICE_CHANGED,
    10024: RejectReason.TIMEOUT,
    10026: RejectReason.TRADE_DISABLED,
    10027: RejectReason.TRADE_DISABLED,
    10030: RejectReason.UNSUPPORTED_FILL,
    10031: RejectReason.DISCONNECTED,
}

SUCCESS_RETCODES = frozenset({10008, 10009})


def reject_reason_for(retcode: int) -> RejectReason:
    """Classify a retcode, failing closed on anything unrecognised."""

    return RETCODE_REJECT_REASON.get(retcode, RejectReason.UNKNOWN)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/unit/brokers/mt5/test_retcodes.py -q --no-cov`

Expected: PASS.

- [ ] **Step 5: Verify the static gates**

Run: `uv run ruff format . && uv run ruff format --check . && uv run ruff check . && uv run mypy`

Expected: all exit `0`.

- [ ] **Step 6: Commit**

```bash
git add src/trading_house/brokers/mt5/retcodes.py tests/unit/brokers/mt5/test_retcodes.py
git commit -m "feat: classify broker return codes, failing closed on the unknown"
```

---

### Task 5: Deterministic Magic Derivation

**Files:**
- Create: `src/trading_house/brokers/mt5/magic.py`
- Test: `tests/unit/brokers/mt5/test_magic.py`, `tests/property/test_magic.py`

**Interfaces:**
- Produces: `derive_magic(intent_id: str, magic_range: tuple[int, int]) -> int`.
- Consumes: nothing. Book magic ranges come from the signed venue binding at the call site.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/brokers/mt5/test_magic.py
import pytest

from trading_house.brokers.mt5.magic import derive_magic

FX_SCALP = (110000, 119999)


def test_derivation_is_deterministic() -> None:
    """Recovery after a crash recomputes the magic with no persisted mapping."""

    assert derive_magic("intent-1", FX_SCALP) == derive_magic("intent-1", FX_SCALP)


def test_derivation_stays_inside_the_books_range() -> None:
    for index in range(500):
        magic = derive_magic(f"intent-{index}", FX_SCALP)
        assert FX_SCALP[0] <= magic <= FX_SCALP[1]


def test_different_books_give_different_magics_for_one_intent() -> None:
    assert derive_magic("intent-1", (110000, 119999)) != derive_magic(
        "intent-1", (120000, 129999)
    )


def test_a_descending_range_is_rejected() -> None:
    with pytest.raises(ValueError, match="ascending"):
        derive_magic("intent-1", (119999, 110000))


def test_a_single_slot_range_is_usable() -> None:
    assert derive_magic("intent-1", (110000, 110000)) == 110000
```

```python
# tests/property/test_magic.py
from hypothesis import given
from hypothesis import strategies as st

from trading_house.brokers.mt5.magic import derive_magic

RANGES = st.tuples(
    st.integers(min_value=1, max_value=900_000),
    st.integers(min_value=0, max_value=99_999),
).map(lambda pair: (pair[0], pair[0] + pair[1]))


@given(st.text(min_size=1, max_size=64), RANGES)
def test_magic_is_always_inside_the_range(intent_id: str, magic_range: tuple[int, int]) -> None:
    magic = derive_magic(intent_id, magic_range)

    assert magic_range[0] <= magic <= magic_range[1]


@given(st.text(min_size=1, max_size=64), RANGES)
def test_magic_is_always_deterministic(intent_id: str, magic_range: tuple[int, int]) -> None:
    assert derive_magic(intent_id, magic_range) == derive_magic(intent_id, magic_range)


@given(st.text(min_size=1, max_size=64), RANGES)
def test_magic_is_always_a_positive_int(intent_id: str, magic_range: tuple[int, int]) -> None:
    """MetaTrader 5 magic numbers must be positive integers."""

    magic = derive_magic(intent_id, magic_range)

    assert isinstance(magic, int)
    assert magic > 0
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/brokers/mt5/test_magic.py tests/property/test_magic.py -q --no-cov`

Expected: collection ERROR — module does not exist.

- [ ] **Step 3: Implement the derivation**

```python
# src/trading_house/brokers/mt5/magic.py
"""Deterministic magic-number derivation inside a book's signed range.

Determinism is the point: after a crash, an intent's magic can be recomputed
with no persisted mapping to lose. Sequential allocation would require
persisting that mapping, which is one more thing that can go missing at exactly
the wrong moment.

Magic is a book-and-intent LOCATOR, not an identity. A 10,000-wide range
collides by birthday after roughly 120 concurrent intents, so recovery scopes
queries by magic AND time window, with the intent ledger authoritative.
"""

import hashlib

_DIGEST_HEX_CHARS = 8


def derive_magic(intent_id: str, magic_range: tuple[int, int]) -> int:
    """Map an intent id into the book's magic range, always identically."""

    start, end = magic_range
    if start > end:
        raise ValueError("magic_range must be ascending")
    span = end - start + 1
    digest = hashlib.sha256(intent_id.encode("utf-8")).hexdigest()[:_DIGEST_HEX_CHARS]
    return start + (int(digest, 16) % span)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/brokers/mt5/test_magic.py tests/property/test_magic.py -q --no-cov`

Expected: PASS.

- [ ] **Step 5: Verify the static gates**

Run: `uv run ruff format . && uv run ruff format --check . && uv run ruff check . && uv run mypy`

Expected: all exit `0`.

- [ ] **Step 6: Commit**

```bash
git add src/trading_house/brokers/mt5/magic.py tests/unit/brokers/mt5/test_magic.py tests/property/test_magic.py
git commit -m "feat: derive magic numbers deterministically from intent ids"
```

---

### Task 6: Symbol Info to InstrumentContract

The largest pure module, and where three real conversion traps live.

**Files:**
- Create: `src/trading_house/brokers/mt5/contracts.py`
- Test: `tests/unit/brokers/mt5/test_contracts.py`

**Interfaces:**
- Produces: `to_instrument_contract(info, *, instrument_id)`, `supported_fills(filling_mode, trade_exemode)`, `asset_class_for(instrument_id)`, `decimal_of(value)`.
- Consumes: `Mt5SymbolInfo` and the MT5 constants (Task 3); `InstrumentContract`, `FillPolicy`, `FinancingModel` from `core.instruments`; `AssetClass`, `InstrumentId` from `core.values`; `ConfigurationError` from `core.errors`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/brokers/mt5/test_contracts.py
from decimal import Decimal

import pytest

from trading_house.brokers.mt5.boundary import (
    SYMBOL_FILLING_FOK,
    SYMBOL_FILLING_IOC,
    SYMBOL_TRADE_EXECUTION_INSTANT,
    SYMBOL_TRADE_EXECUTION_MARKET,
    SYMBOL_TRADE_MODE_DISABLED,
    SYMBOL_TRADE_MODE_FULL,
    SYMBOL_TRADE_MODE_LONGONLY,
    Mt5SymbolInfo,
)
from trading_house.brokers.mt5.contracts import (
    asset_class_for,
    decimal_of,
    supported_fills,
    to_instrument_contract,
)
from trading_house.core.errors import ConfigurationError
from trading_house.core.instruments import FillPolicy, FinancingModel
from trading_house.core.values import AssetClass


def _info(**overrides: object) -> Mt5SymbolInfo:
    base: dict[str, object] = {
        "name": "EURUSD",
        "digits": 5,
        "point": 1e-05,
        "trade_tick_size": 1e-05,
        "trade_tick_value_loss": 1.0,
        "volume_min": 0.01,
        "volume_step": 0.01,
        "volume_max": 200.0,
        "trade_stops_level": 10,
        "trade_freeze_level": 5,
        "trade_mode": SYMBOL_TRADE_MODE_FULL,
        "trade_exemode": SYMBOL_TRADE_EXECUTION_INSTANT,
        "filling_mode": SYMBOL_FILLING_FOK | SYMBOL_FILLING_IOC,
        "currency_base": "EUR",
        "currency_profit": "USD",
    }
    base.update(overrides)
    return Mt5SymbolInfo(**base)  # type: ignore[arg-type]


def test_float_conversion_avoids_binary_artifacts() -> None:
    """Decimal(0.1) is 0.1000000000000000055511151231257827 — never use it."""

    assert decimal_of(0.1) == Decimal("0.1")
    assert decimal_of(1e-05) == Decimal("0.00001")
    assert str(decimal_of(0.1)) == "0.1"


def test_distances_convert_from_points_to_price_units() -> None:
    contract = to_instrument_contract(_info(), instrument_id="fx.eurusd")

    assert contract.min_stop_distance == Decimal("10") * Decimal("0.00001")
    assert contract.freeze_distance == Decimal("5") * Decimal("0.00001")


def test_zero_stops_level_floors_to_one_price_increment() -> None:
    """PositiveDecimal cannot express 'no minimum', and a stop must still be
    at least one tick from the market."""

    contract = to_instrument_contract(
        _info(trade_stops_level=0, trade_freeze_level=0), instrument_id="fx.eurusd"
    )

    assert contract.min_stop_distance == Decimal("0.00001")
    assert contract.freeze_distance == Decimal("0.00001")


def test_sizing_uses_tick_value_loss_not_tick_value() -> None:
    contract = to_instrument_contract(
        _info(trade_tick_value_loss=0.97), instrument_id="fx.eurusd"
    )

    assert contract.value_per_price_increment == Decimal("0.97")


def test_price_increment_uses_tick_size_not_point() -> None:
    """point and trade_tick_size differ on some instruments; sizing needs tick size."""

    contract = to_instrument_contract(
        _info(point=1e-05, trade_tick_size=1e-04), instrument_id="fx.eurusd"
    )

    assert contract.price_increment == Decimal("0.0001")


@pytest.mark.parametrize(
    ("filling_mode", "exemode", "expected"),
    [
        (SYMBOL_FILLING_FOK, SYMBOL_TRADE_EXECUTION_INSTANT, {FillPolicy.FOK}),
        (SYMBOL_FILLING_IOC, SYMBOL_TRADE_EXECUTION_INSTANT, {FillPolicy.IOC}),
        (
            SYMBOL_FILLING_FOK | SYMBOL_FILLING_IOC,
            SYMBOL_TRADE_EXECUTION_INSTANT,
            {FillPolicy.FOK, FillPolicy.IOC},
        ),
        (
            SYMBOL_FILLING_IOC,
            SYMBOL_TRADE_EXECUTION_MARKET,
            {FillPolicy.IOC, FillPolicy.RETURN},
        ),
        (0, SYMBOL_TRADE_EXECUTION_INSTANT, {FillPolicy.RETURN}),
    ],
)
def test_supported_fills_needs_both_bitmask_and_execution_mode(
    filling_mode: int, exemode: int, expected: set[FillPolicy]
) -> None:
    """RETURN availability depends on exemode, not the bitmask — deriving from
    the bitmask alone silently drops a valid policy."""

    assert supported_fills(filling_mode, exemode) == frozenset(expected)


def test_a_disabled_symbol_is_rejected() -> None:
    with pytest.raises(ConfigurationError):
        to_instrument_contract(
            _info(trade_mode=SYMBOL_TRADE_MODE_DISABLED), instrument_id="fx.eurusd"
        )


def test_long_only_symbols_are_not_shortable() -> None:
    contract = to_instrument_contract(
        _info(trade_mode=SYMBOL_TRADE_MODE_LONGONLY), instrument_id="equity_cfd.aapl"
    )

    assert contract.shortable is False


@pytest.mark.parametrize(
    ("instrument_id", "expected"),
    [
        ("fx.eurusd", AssetClass.FX),
        ("metal.xauusd", AssetClass.METAL),
        ("equity_cfd.aapl", AssetClass.EQUITY_CFD),
    ],
)
def test_asset_class_derives_from_the_instrument_id_prefix(
    instrument_id: str, expected: AssetClass
) -> None:
    assert asset_class_for(instrument_id) is expected


def test_an_unknown_prefix_is_rejected() -> None:
    with pytest.raises(ConfigurationError):
        asset_class_for("crypto.btcusd")


def test_equity_cfds_are_financed_by_dividend_adjustment() -> None:
    contract = to_instrument_contract(
        _info(trade_mode=SYMBOL_TRADE_MODE_FULL), instrument_id="equity_cfd.aapl"
    )

    assert contract.financing is FinancingModel.DIVIDEND_ADJUSTMENT


def test_fx_and_metals_are_financed_by_swap() -> None:
    assert (
        to_instrument_contract(_info(), instrument_id="fx.eurusd").financing
        is FinancingModel.SWAP
    )
    assert (
        to_instrument_contract(_info(), instrument_id="metal.xauusd").financing
        is FinancingModel.SWAP
    )


def test_currencies_map_from_base_and_profit() -> None:
    contract = to_instrument_contract(
        _info(currency_base="XAU", currency_profit="USD"), instrument_id="metal.xauusd"
    )

    assert contract.base_currency == "XAU"
    assert contract.quote_currency == "USD"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/brokers/mt5/test_contracts.py -q --no-cov`

Expected: collection ERROR — module does not exist.

- [ ] **Step 3: Implement the mapping**

```python
# src/trading_house/brokers/mt5/contracts.py
"""Map MetaTrader 5 symbol metadata onto the venue-neutral InstrumentContract.

Three conversions carry real risk and are handled explicitly here:

* MetaTrader 5 returns floats; the canonical contracts are Decimal. Every
  conversion goes through ``Decimal(str(value))`` because ``Decimal(0.1)`` is
  ``0.1000000000000000055511151231257827…``, and a silent artifact in
  ``value_per_price_increment`` propagates straight into position sizing.
* ``stops_level`` and ``freeze_level`` are in POINTS; the contract is in price
  units. A broker reporting zero means "no minimum", which a PositiveDecimal
  cannot express, so the value floors to one price increment — a stop must be
  at least one tick from the market regardless.
* ``supported_fills`` needs the filling bitmask AND the execution mode.
  Deriving it from the bitmask alone silently drops RETURN.
"""

from __future__ import annotations

from decimal import Decimal

from trading_house.brokers.mt5.boundary import (
    SYMBOL_FILLING_FOK,
    SYMBOL_FILLING_IOC,
    SYMBOL_TRADE_EXECUTION_EXCHANGE,
    SYMBOL_TRADE_EXECUTION_MARKET,
    SYMBOL_TRADE_MODE_DISABLED,
    SYMBOL_TRADE_MODE_FULL,
    SYMBOL_TRADE_MODE_SHORTONLY,
    Mt5SymbolInfo,
)
from trading_house.core.errors import ConfigurationError
from trading_house.core.instruments import FillPolicy, FinancingModel, InstrumentContract
from trading_house.core.values import AssetClass, InstrumentId

_PREFIX_ASSET_CLASS = {
    "fx": AssetClass.FX,
    "metal": AssetClass.METAL,
    "equity_cfd": AssetClass.EQUITY_CFD,
}

_SHORTABLE_TRADE_MODES = frozenset({SYMBOL_TRADE_MODE_FULL, SYMBOL_TRADE_MODE_SHORTONLY})
_RETURN_EXECUTION_MODES = frozenset(
    {SYMBOL_TRADE_EXECUTION_MARKET, SYMBOL_TRADE_EXECUTION_EXCHANGE}
)


def decimal_of(value: float) -> Decimal:
    """Convert without binary artifacts. Never use ``Decimal(float)`` directly."""

    return Decimal(str(value))


def asset_class_for(instrument_id: str) -> AssetClass:
    """Derive the asset class from the instrument id's prefix."""

    prefix = instrument_id.split(".", 1)[0]
    asset_class = _PREFIX_ASSET_CLASS.get(prefix)
    if asset_class is None:
        raise ConfigurationError()
    return asset_class


def supported_fills(filling_mode: int, trade_exemode: int) -> frozenset[FillPolicy]:
    """Derive fill policies from the bitmask AND the execution mode."""

    policies: set[FillPolicy] = set()
    if filling_mode & SYMBOL_FILLING_FOK:
        policies.add(FillPolicy.FOK)
    if filling_mode & SYMBOL_FILLING_IOC:
        policies.add(FillPolicy.IOC)
    if trade_exemode in _RETURN_EXECUTION_MODES or not policies:
        policies.add(FillPolicy.RETURN)
    return frozenset(policies)


def _financing_for(asset_class: AssetClass) -> FinancingModel:
    if asset_class is AssetClass.EQUITY_CFD:
        return FinancingModel.DIVIDEND_ADJUSTMENT
    return FinancingModel.SWAP


def to_instrument_contract(
    info: Mt5SymbolInfo, *, instrument_id: InstrumentId
) -> InstrumentContract:
    """Build the neutral contract, rejecting symbols that cannot be traded."""

    if info.trade_mode == SYMBOL_TRADE_MODE_DISABLED:
        raise ConfigurationError()

    asset_class = asset_class_for(instrument_id)
    point = decimal_of(info.point)
    price_increment = decimal_of(info.trade_tick_size)
    fills = supported_fills(info.filling_mode, info.trade_exemode)
    if not fills:
        raise ConfigurationError()

    return InstrumentContract(
        instrument_id=instrument_id,
        asset_class=asset_class,
        base_currency=info.currency_base,
        quote_currency=info.currency_profit,
        price_increment=price_increment,
        quantity_increment=decimal_of(info.volume_step),
        quantity_min=decimal_of(info.volume_min),
        quantity_max=decimal_of(info.volume_max),
        value_per_price_increment=decimal_of(info.trade_tick_value_loss),
        min_stop_distance=max(decimal_of(info.trade_stops_level) * point, price_increment),
        freeze_distance=max(decimal_of(info.trade_freeze_level) * point, price_increment),
        session_calendar_id=f"{asset_class.value}.default",
        financing=_financing_for(asset_class),
        shortable=info.trade_mode in _SHORTABLE_TRADE_MODES,
        supported_fills=fills,
    )
```

Note `session_calendar_id` is derived rather than configured. Real trading calendars arrive with the equity work; a derived default keeps the contract satisfiable now without inventing config no one reads.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/brokers/mt5/test_contracts.py -q --no-cov`

Expected: PASS.

- [ ] **Step 5: Verify the static gates and regression**

Run: `uv run ruff format . && uv run ruff format --check . && uv run ruff check . && uv run mypy && uv run pytest tests/unit tests/property -q --no-cov`

Expected: all exit `0`.

- [ ] **Step 6: Commit**

```bash
git add src/trading_house/brokers/mt5/contracts.py tests/unit/brokers/mt5/test_contracts.py
git commit -m "feat: map broker symbol metadata to neutral instrument contracts"
```

---

### Task 7: The Terminal Implementation and the Import Boundary

This is where `MetaTrader5` enters the codebase.

**Files:**
- Create: `src/trading_house/brokers/mt5/terminal.py`
- Modify: `pyproject.toml`
- Modify: `tests/acceptance/test_architecture.py`
- Modify: `tests/acceptance/test_phase0.py`

**Interfaces:**
- Produces: `Mt5Terminal` implementing `TerminalPort`.
- Consumes: the DTOs and protocol from Task 3.

- [ ] **Step 1: Write the failing architecture tests**

In `tests/acceptance/test_architecture.py`, add near the other module-level constants:

```python
MT5_IMPORT_ALLOWED = frozenset({SOURCE_ROOT / "brokers" / "mt5" / "terminal.py"})
TERMINAL_STATEMENT_CAP = 80
```

and add these tests:

```python
def test_metatrader5_is_importable_from_exactly_one_module() -> None:
    """The venue-neutral core exists so a second broker is cheap. One file
    may speak MT5; anything wider re-creates the coupling this phase removed."""

    offenders = [
        path.relative_to(PROJECT_ROOT).as_posix()
        for path, tree in _parsed()
        if path not in MT5_IMPORT_ALLOWED and "MetaTrader5" in _imported_top_level(tree)
    ]

    assert offenders == []


def test_the_terminal_module_is_where_metatrader5_actually_lives() -> None:
    """Guard the guard: if terminal.py stops importing it, the exemption is stale."""

    terminal = SOURCE_ROOT / "brokers" / "mt5" / "terminal.py"
    assert terminal.exists()
    tree = ast.parse(terminal.read_text(encoding="utf-8"))
    assert "MetaTrader5" in _imported_top_level(tree)


def test_the_terminal_module_stays_thin() -> None:
    """terminal.py is omitted from coverage, so a size cap is what stops it
    becoming the place untested logic accumulates."""

    terminal = SOURCE_ROOT / "brokers" / "mt5" / "terminal.py"
    tree = ast.parse(terminal.read_text(encoding="utf-8"))
    statements = sum(1 for node in ast.walk(tree) if isinstance(node, ast.stmt))

    assert statements <= TERMINAL_STATEMENT_CAP, (
        f"terminal.py has {statements} statements; move logic into the pure modules"
    )
```

Remove `"MetaTrader5"` from `FORBIDDEN_TOP_LEVEL_IMPORTS` in that file — the two tests above now own that rule with the exemption.

In `tests/acceptance/test_phase0.py`, replace the tree-wide checks with a Phase-0-scoped one:

```python
PHASE_0_PACKAGES = ("core", "audit", "constitution", "database", "ops")
PHASE_0_MODULES = ("settings.py", "cli.py")


def test_phase0_packages_never_import_a_broker_or_agent_framework() -> None:
    """Phase 0's guarantee was about ITS packages, and stays true forever even
    as later phases add venues."""

    source_root = PROJECT_ROOT / "src" / "trading_house"
    paths = [p for package in PHASE_0_PACKAGES for p in (source_root / package).rglob("*.py")]
    paths += [source_root / module for module in PHASE_0_MODULES]

    offenders: list[str] = []
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module.split(".")[0]]
            else:
                continue
            if set(names) & FORBIDDEN_TOP_LEVEL_IMPORTS:
                offenders.append(path.relative_to(PROJECT_ROOT).as_posix())

    assert offenders == []
```

Keep `FORBIDDEN_TOP_LEVEL_IMPORTS` in `test_phase0.py` as-is, including `MetaTrader5` — the Phase 0 packages must still never import it.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/acceptance/test_architecture.py -q --no-cov`

Expected: FAIL — `terminal.py` does not exist, so `test_the_terminal_module_is_where_metatrader5_actually_lives` fails.

- [ ] **Step 3: Add the dependency and the test/coverage configuration**

In `pyproject.toml`, add to `[project] dependencies`:

```toml
  "MetaTrader5>=5.0.45,<6 ; sys_platform == 'win32'",
```

Add the marker to `[tool.pytest.ini_options] markers`:

```toml
markers = [
  "integration: requires PostgreSQL",
  "mt5: requires a running MetaTrader 5 terminal",
]
```

Add a coverage omit section:

```toml
[tool.coverage.run]
omit = ["src/trading_house/brokers/mt5/terminal.py"]
```

Then run `uv lock` to record the new dependency, and commit the updated `uv.lock`.

- [ ] **Step 4: Implement the terminal**

Keep this file thin — the statement cap is 80 and the test enforces it.

```python
# src/trading_house/brokers/mt5/terminal.py
"""The only module in this codebase that imports MetaTrader5.

Everything here is IPC plus type conversion. Logic belongs in the pure modules,
which are covered by tests; this file is omitted from coverage and capped in
size precisely so it cannot become a home for untested behaviour.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta

import MetaTrader5 as mt5

from trading_house.brokers.mt5.boundary import (
    Mt5CheckResult,
    Mt5Position,
    Mt5SymbolInfo,
    Mt5Tick,
)


class Mt5Terminal:
    """A thin, typed shell over the MetaTrader 5 IPC surface."""

    def __init__(self) -> None:
        self._offset = timedelta(0)

    def initialize(self) -> bool:
        return bool(mt5.initialize())

    def shutdown(self) -> None:
        mt5.shutdown()

    def account_trade_mode(self) -> int:
        account = mt5.account_info()
        return -1 if account is None else int(account.trade_mode)

    def server_utc_offset_seconds(self) -> int:
        tick = mt5.symbol_info_tick("EURUSD")
        if tick is None:
            return 0
        offset = round(float(tick.time) - datetime.now(UTC).timestamp())
        self._offset = timedelta(seconds=offset)
        return offset

    def _to_utc(self, server_epoch: float) -> datetime:
        return datetime.fromtimestamp(float(server_epoch), UTC) - self._offset

    def symbol_info(self, server_symbol: str) -> Mt5SymbolInfo | None:
        mt5.symbol_select(server_symbol, True)
        info = mt5.symbol_info(server_symbol)
        if info is None:
            return None
        return Mt5SymbolInfo(
            name=info.name,
            digits=int(info.digits),
            point=float(info.point),
            trade_tick_size=float(info.trade_tick_size),
            trade_tick_value_loss=float(info.trade_tick_value_loss),
            volume_min=float(info.volume_min),
            volume_step=float(info.volume_step),
            volume_max=float(info.volume_max),
            trade_stops_level=int(info.trade_stops_level),
            trade_freeze_level=int(info.trade_freeze_level),
            trade_mode=int(info.trade_mode),
            trade_exemode=int(info.trade_exemode),
            filling_mode=int(info.filling_mode),
            currency_base=info.currency_base,
            currency_profit=info.currency_profit,
        )

    def symbol_tick(self, server_symbol: str) -> Mt5Tick | None:
        tick = mt5.symbol_info_tick(server_symbol)
        if tick is None:
            return None
        return Mt5Tick(
            bid=float(tick.bid), ask=float(tick.ask), observed_at=self._to_utc(tick.time)
        )

    def positions(self) -> Sequence[Mt5Position]:
        raw = mt5.positions_get()
        if raw is None:
            return ()
        return tuple(
            Mt5Position(
                ticket=int(p.ticket),
                magic=int(p.magic),
                server_symbol=p.symbol,
                volume=float(p.volume),
                price_open=float(p.price_open),
                sl=float(p.sl),
                tp=float(p.tp) or None,
                is_buy=int(p.type) == 0,
                opened_at=self._to_utc(p.time),
            )
            for p in raw
        )

    def order_check(self, request: Mapping[str, object]) -> Mt5CheckResult | None:
        result = mt5.order_check(dict(request))
        if result is None:
            return None
        return Mt5CheckResult(retcode=int(result.retcode), comment=str(result.comment))

    def last_error(self) -> tuple[int, str]:
        code, description = mt5.last_error()
        return int(code), str(description)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/acceptance -q --no-cov`

Expected: PASS. On Linux the architecture tests still pass because they parse with `ast` and never import the module.

- [ ] **Step 6: Verify the static gates and the lock**

Run: `uv lock --check && uv run ruff format . && uv run ruff format --check . && uv run ruff check . && uv run mypy`

Expected: all exit `0`. If mypy cannot resolve `MetaTrader5` on this platform, add `[[tool.mypy.overrides]] module = "MetaTrader5" ignore_missing_imports = true` to `pyproject.toml` — that is a stub-availability accommodation, not a strictness relaxation.

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml uv.lock src/trading_house/brokers/mt5/terminal.py tests/acceptance
git commit -m "feat: confine every MetaTrader5 call to one thin module"
```

---

### Task 8: The Gateway Actor

**Files:**
- Create: `src/trading_house/brokers/mt5/gateway.py`
- Test: `tests/unit/brokers/mt5/test_gateway.py`

**Interfaces:**
- Produces: `Priority`, `GatewayMetrics`, `Mt5Gateway` with `start()`, `stop()`, `call(priority, operation)`, `metrics()`, and context-manager support.
- Consumes: `TerminalPort` and DTOs (Task 3); `BrokerUnavailableError`, `NonDemoAccountError` (Task 1); `Clock` from `core.clock`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/brokers/mt5/test_gateway.py
import threading
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

import pytest

from trading_house.brokers.mt5.boundary import (
    ACCOUNT_TRADE_MODE_DEMO,
    ACCOUNT_TRADE_MODE_REAL,
    Mt5CheckResult,
    Mt5Position,
    Mt5SymbolInfo,
    Mt5Tick,
)
from trading_house.brokers.mt5.gateway import Mt5Gateway, Priority
from trading_house.core.clock import SystemClock
from trading_house.core.errors import BrokerUnavailableError, NonDemoAccountError


class FakeTerminal:
    """A TerminalPort that needs no MetaTrader 5 and no Windows."""

    def __init__(
        self, *, trade_mode: int = ACCOUNT_TRADE_MODE_DEMO, initialises: bool = True
    ) -> None:
        self.trade_mode = trade_mode
        self.initialises = initialises
        self.shutdown_calls = 0
        self.gate = threading.Event()
        self.gate.set()

    def initialize(self) -> bool:
        return self.initialises

    def shutdown(self) -> None:
        self.shutdown_calls += 1

    def account_trade_mode(self) -> int:
        return self.trade_mode

    def server_utc_offset_seconds(self) -> int:
        return 0

    def symbol_info(self, server_symbol: str) -> Mt5SymbolInfo | None:
        self.gate.wait(5)
        return None

    def symbol_tick(self, server_symbol: str) -> Mt5Tick | None:
        return Mt5Tick(bid=1.1, ask=1.2, observed_at=datetime(2026, 8, 25, tzinfo=UTC))

    def positions(self) -> Sequence[Mt5Position]:
        return ()

    def order_check(self, request: Mapping[str, object]) -> Mt5CheckResult | None:
        return Mt5CheckResult(retcode=10009, comment="done")

    def last_error(self) -> tuple[int, str]:
        return 0, "ok"


def _gateway(terminal: FakeTerminal) -> Mt5Gateway:
    return Mt5Gateway(terminal, clock=SystemClock(), request_timeout_seconds=5.0)


def test_a_non_demo_account_is_refused_before_any_request_is_served() -> None:
    """There must be no state in which the gateway is connected to a live
    account and merely not trading yet."""

    terminal = FakeTerminal(trade_mode=ACCOUNT_TRADE_MODE_REAL)
    gateway = _gateway(terminal)

    with pytest.raises(NonDemoAccountError):
        gateway.start()

    assert terminal.shutdown_calls == 1


def test_a_terminal_that_will_not_initialise_raises_typed() -> None:
    gateway = _gateway(FakeTerminal(initialises=False))

    with pytest.raises(BrokerUnavailableError):
        gateway.start()


def test_calls_are_served_on_a_demo_account() -> None:
    with _gateway(FakeTerminal()) as gateway:
        tick = gateway.call(Priority.MARKET_DATA, lambda t: t.symbol_tick("EURUSD"))

    assert tick is not None
    assert tick.bid == 1.1


def test_higher_priority_work_overtakes_lower() -> None:
    """FIFO would let a protection call sit behind data polls. At scalp
    horizons that is the difference between a managed stop and a blown one."""

    terminal = FakeTerminal()
    order: list[Priority] = []
    with _gateway(terminal) as gateway:
        terminal.gate.clear()
        blocker = threading.Thread(
            target=lambda: gateway.call(Priority.MARKET_DATA, lambda t: t.symbol_info("X")),
            daemon=True,
        )
        blocker.start()
        threading.Event().wait(0.2)

        threads = []
        for priority in (Priority.MARKET_DATA, Priority.RECONCILE, Priority.PROTECTION):
            thread = threading.Thread(
                target=lambda p=priority: gateway.call(p, lambda t: order.append(p)),
                daemon=True,
            )
            thread.start()
            threads.append(thread)
            threading.Event().wait(0.05)

        terminal.gate.set()
        for thread in threads:
            thread.join(5)
        blocker.join(5)

    assert order == [Priority.PROTECTION, Priority.RECONCILE, Priority.MARKET_DATA]


def test_metrics_report_served_calls_and_a_heartbeat() -> None:
    with _gateway(FakeTerminal()) as gateway:
        gateway.call(Priority.MARKET_DATA, lambda t: t.symbol_tick("EURUSD"))
        metrics = gateway.metrics()

    assert metrics.served >= 1
    assert metrics.last_heartbeat.tzinfo is UTC


def test_a_failing_operation_propagates_and_is_counted() -> None:
    def boom(terminal: object) -> None:
        raise RuntimeError("broker exploded")

    with _gateway(FakeTerminal()) as gateway:
        with pytest.raises(RuntimeError, match="broker exploded"):
            gateway.call(Priority.MARKET_DATA, boom)
        assert gateway.metrics().failed >= 1


def test_stopping_shuts_the_terminal_down() -> None:
    terminal = FakeTerminal()
    gateway = _gateway(terminal)
    gateway.start()
    gateway.stop()

    assert terminal.shutdown_calls == 1


def test_calls_before_start_are_refused() -> None:
    gateway = _gateway(FakeTerminal())

    with pytest.raises(BrokerUnavailableError):
        gateway.call(Priority.MARKET_DATA, lambda t: None)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/brokers/mt5/test_gateway.py -q --no-cov`

Expected: collection ERROR — module does not exist.

- [ ] **Step 3: Implement the actor**

```python
# src/trading_house/brokers/mt5/gateway.py
"""A single-threaded actor owning every MetaTrader 5 call.

The MetaTrader 5 Python API is not thread-safe and its calls block, so exactly
one thread touches it. Work is prioritised rather than FIFO: a protection call
must never sit behind a queue of market-data polls.

A hard demo check runs inside ``start()`` before any request is served, so
there is no state in which the gateway is connected to a live account and
merely not trading yet.

Known limitation: ``mt5.*`` calls are blocking C calls. If one hangs, Python
cannot kill it and the actor thread is stuck. Callers time out, the heartbeat
stops advancing, and health reports unhealthy — but in-process recovery is not
possible. Real isolation needs a separate process.
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import IntEnum
from itertools import count
from types import TracebackType
from typing import Any, Self, TypeVar

from trading_house.brokers.mt5.boundary import ACCOUNT_TRADE_MODE_DEMO, TerminalPort
from trading_house.core.clock import Clock
from trading_house.core.errors import BrokerUnavailableError, NonDemoAccountError

T = TypeVar("T")

_SHUTDOWN_TIMEOUT_SECONDS = 5.0


class Priority(IntEnum):
    """Lower runs first. Protection must never queue behind a data poll."""

    PROTECTION = 0
    ORDER = 1
    RECONCILE = 2
    MARKET_DATA = 3


@dataclass(frozen=True, slots=True)
class GatewayMetrics:
    """Operational data about the actor, not venue-neutral fact."""

    queue_depth: Mapping[Priority, int]
    served: int
    failed: int
    last_heartbeat: datetime
    stale: bool


@dataclass(slots=True)
class _Request:
    operation: Callable[[TerminalPort], Any]
    done: threading.Event = field(default_factory=threading.Event)
    result: Any = None
    error: BaseException | None = None


class Mt5Gateway:
    """Serialise every terminal call through one prioritised thread."""

    def __init__(
        self,
        terminal: TerminalPort,
        *,
        clock: Clock,
        request_timeout_seconds: float = 10.0,
    ) -> None:
        self._terminal = terminal
        self._clock = clock
        self._timeout = request_timeout_seconds
        self._queue: queue.PriorityQueue[tuple[int, int, _Request]] = queue.PriorityQueue()
        self._sequence = count()
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()
        self._lock = threading.Lock()
        self._served = 0
        self._failed = 0
        self._depth: dict[Priority, int] = dict.fromkeys(Priority, 0)
        self._heartbeat = clock.now()
        self._server_utc_offset_seconds = 0

    def start(self) -> None:
        """Connect, refuse a non-demo account, then begin serving."""

        if not self._terminal.initialize():
            raise BrokerUnavailableError()
        try:
            if self._terminal.account_trade_mode() != ACCOUNT_TRADE_MODE_DEMO:
                raise NonDemoAccountError()
            self._server_utc_offset_seconds = self._terminal.server_utc_offset_seconds()
        except BaseException:
            self._terminal.shutdown()
            raise
        self._stopping.clear()
        self._thread = threading.Thread(target=self._serve, name="mt5-gateway", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stopping.set()
        thread = self._thread
        if thread is not None:
            thread.join(_SHUTDOWN_TIMEOUT_SECONDS)
        self._thread = None
        self._terminal.shutdown()

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback
        self.stop()

    def call(self, priority: Priority, operation: Callable[[TerminalPort], T]) -> T:
        """Run one operation on the actor thread and return its result."""

        if self._thread is None or not self._thread.is_alive():
            raise BrokerUnavailableError()
        request = _Request(operation=operation)
        with self._lock:
            self._depth[priority] += 1
        self._queue.put((int(priority), next(self._sequence), request))
        if not request.done.wait(self._timeout):
            raise BrokerUnavailableError()
        if request.error is not None:
            raise request.error
        return request.result  # type: ignore[no-any-return]

    def metrics(self) -> GatewayMetrics:
        with self._lock:
            return GatewayMetrics(
                queue_depth=dict(self._depth),
                served=self._served,
                failed=self._failed,
                last_heartbeat=self._heartbeat,
                stale=self._stopping.is_set(),
            )

    @property
    def server_utc_offset_seconds(self) -> int:
        return self._server_utc_offset_seconds

    def _serve(self) -> None:
        while not self._stopping.is_set():
            try:
                priority_value, _, request = self._queue.get(timeout=0.1)
            except queue.Empty:
                with self._lock:
                    self._heartbeat = self._clock.now()
                continue
            try:
                request.result = request.operation(self._terminal)
            except BaseException as error:  # noqa: BLE001 — relayed to the caller
                request.error = error
            finally:
                with self._lock:
                    self._depth[Priority(priority_value)] -= 1
                    self._heartbeat = self._clock.now()
                    if request.error is None:
                        self._served += 1
                    else:
                        self._failed += 1
                request.done.set()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/brokers/mt5/test_gateway.py -q --no-cov`

Expected: PASS, 8 tests. If the priority-ordering test is flaky, increase the settling waits — do not weaken the assertion.

- [ ] **Step 4b: Add the stale-until-reconciled rule**

Spec §2.4: *any connectivity gap marks gateway state stale, and it stays stale until a `reconcile()` succeeds.* The `stale` field above is currently wired to shutdown, which is not that rule. Encoding it now means Phase 3 inherits it rather than remembering it.

Add to `Mt5Gateway`:

```python
    def mark_stale(self) -> None:
        """Record that gateway state can no longer be trusted."""

        with self._lock:
            self._stale = True

    def mark_reconciled(self) -> None:
        """Clear staleness. Only a successful reconcile may call this."""

        with self._lock:
            self._stale = False
```

Initialise `self._stale = True` in `__init__` — state is untrusted until the first reconcile — and clear it nowhere else. Change `metrics()` to report `stale=self._stale`. In `_serve`, call `mark_stale()` whenever an operation raises, since a failed terminal call is exactly a connectivity gap.

Add these tests:

```python
def test_a_fresh_gateway_is_stale_until_reconciled() -> None:
    with _gateway(FakeTerminal()) as gateway:
        assert gateway.metrics().stale is True
        gateway.mark_reconciled()
        assert gateway.metrics().stale is False


def test_a_failed_call_marks_state_stale_again() -> None:
    """A failed terminal call is a connectivity gap; state cannot be trusted."""

    def boom(terminal: object) -> None:
        raise RuntimeError("connection lost")

    with _gateway(FakeTerminal()) as gateway:
        gateway.mark_reconciled()
        with pytest.raises(RuntimeError):
            gateway.call(Priority.MARKET_DATA, boom)
        assert gateway.metrics().stale is True
```

Run: `uv run pytest tests/unit/brokers/mt5/test_gateway.py -q --no-cov`

Expected: PASS, 10 tests.

- [ ] **Step 5: Verify the static gates and regression**

Run: `uv run ruff format . && uv run ruff format --check . && uv run ruff check . && uv run mypy && uv run pytest tests/unit tests/property tests/acceptance -q --no-cov`

Expected: all exit `0`.

- [ ] **Step 6: Commit**

```bash
git add src/trading_house/brokers/mt5/gateway.py tests/unit/brokers/mt5/test_gateway.py
git commit -m "feat: serialise terminal calls through a prioritised actor"
```

---

### Task 9: The Read-Only Adapter

**Files:**
- Create: `src/trading_house/brokers/mt5/adapter.py`
- Test: `tests/unit/brokers/mt5/test_adapter.py`

**Interfaces:**
- Produces: `Mt5BrokerAdapter(gateway, binding)` implementing `BrokerAdapter`.
- Consumes: `Mt5Gateway`, `Priority` (Task 8); `to_instrument_contract`, `asset_class_for` (Task 6); `reject_reason_for` (Task 4); `derive_magic` (Task 5); `VenueBinding` from `constitution.binding`; `BrokerAdapter`, `MarketSnapshot`, `Quote`, `ReconciliationReport`, `VenueHealth` from `brokers.base`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/brokers/mt5/test_adapter.py
import pytest

from trading_house.brokers.base import BrokerAdapter
from trading_house.brokers.mt5.adapter import Mt5BrokerAdapter
from trading_house.core.errors import ConfigurationError


def test_adapter_satisfies_the_broker_protocol() -> None:
    assert issubclass(Mt5BrokerAdapter, BrokerAdapter) or isinstance(
        Mt5BrokerAdapter, type
    )
    for method in (
        "describe_instrument", "snapshot", "precheck", "submit",
        "amend_protection", "close", "reconcile", "health",
    ):
        assert hasattr(Mt5BrokerAdapter, method)


@pytest.mark.parametrize("method", ["submit", "amend_protection", "close"])
def test_mutating_methods_refuse_in_this_phase(method: str) -> None:
    """Phase 1 sends no orders. These arrive with Phase 3's intent ledger."""

    import inspect

    source = inspect.getsource(getattr(Mt5BrokerAdapter, method))
    assert "NotImplementedError" in source
    assert "Phase 3" in source


def test_an_unbound_instrument_is_rejected() -> None:
    """The signed venue binding is authoritative for symbol identity."""

    adapter = Mt5BrokerAdapter.__new__(Mt5BrokerAdapter)
    adapter._server_symbols = {}  # type: ignore[attr-defined]

    with pytest.raises(ConfigurationError):
        adapter._server_symbol_for("fx.gbpusd")  # type: ignore[attr-defined]
```

Plus these, exercising the read paths against the `FakeTerminal` from Task 8 driven through a real `Mt5Gateway`:

```python
from decimal import Decimal

from trading_house.brokers.mt5.gateway import Mt5Gateway
from trading_house.constitution.binding import parse_venue_binding
from trading_house.core.clock import SystemClock

BINDING = parse_venue_binding(
    b"""
venue: mt5
books:
  fx_scalp: {magic_range: [110000, 119999]}
  fx_swing: {magic_range: [120000, 129999]}
instruments:
  fx.eurusd: {server_symbol: "EURUSD"}
"""
)


def _adapter(terminal: object) -> tuple[Mt5BrokerAdapter, Mt5Gateway]:
    gateway = Mt5Gateway(terminal, clock=SystemClock(), request_timeout_seconds=5.0)  # type: ignore[arg-type]
    gateway.start()
    return Mt5BrokerAdapter(gateway, BINDING), gateway


def test_describe_instrument_maps_a_terminal_symbol(symbol_terminal: object) -> None:
    adapter, gateway = _adapter(symbol_terminal)
    try:
        contract = adapter.describe_instrument("fx.eurusd")
    finally:
        gateway.stop()

    assert contract.instrument_id == "fx.eurusd"
    assert contract.price_increment == Decimal("0.00001")


def test_snapshot_skips_instruments_with_no_tick(tickless_terminal: object) -> None:
    """Fabricating a quote for a symbol the terminal has no tick for would put
    an invented price into the risk engine."""

    adapter, gateway = _adapter(tickless_terminal)
    try:
        snapshot = adapter.snapshot(["fx.eurusd"])
    finally:
        gateway.stop()

    assert snapshot.quotes == ()


def test_reconcile_attributes_positions_to_their_book(position_terminal: object) -> None:
    adapter, gateway = _adapter(position_terminal)
    try:
        report = adapter.reconcile("fx_scalp")
    finally:
        gateway.stop()

    assert report.book == "fx_scalp"
    assert len(report.positions) == 1


def test_reconcile_lists_out_of_range_magics_as_unmatched(position_terminal: object) -> None:
    """A position whose magic falls in no declared range is not ours to claim."""

    adapter, gateway = _adapter(position_terminal)
    try:
        report = adapter.reconcile("fx_swing")
    finally:
        gateway.stop()

    assert report.positions == ()
    assert len(report.unmatched_venue_refs) >= 1
```

Define `symbol_terminal`, `tickless_terminal` and `position_terminal` as fixtures returning `FakeTerminal` subclasses that override `symbol_info`, `symbol_tick` and `positions` respectively. `position_terminal.positions()` returns one `Mt5Position` with `magic=110042` (inside `fx_scalp`) and one with `magic=999999` (in no declared range).

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/brokers/mt5/test_adapter.py -q --no-cov`

Expected: collection ERROR — module does not exist.

- [ ] **Step 3: Implement the adapter**

```python
# src/trading_house/brokers/mt5/adapter.py
"""The read-only half of BrokerAdapter, backed by the MetaTrader 5 gateway.

Phase 1 sends no orders. ``submit``, ``amend_protection`` and ``close`` refuse
until Phase 3, when the intent ledger exists to make a lost response
recoverable. Symbol identity comes from the signed venue binding, never from a
broker string, so a renamed symbol is a config change rather than a code one.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from trading_house.brokers.base import (
    MarketSnapshot,
    Quote,
    ReconciliationReport,
    VenueHealth,
)
from trading_house.brokers.mt5.contracts import decimal_of, to_instrument_contract
from trading_house.brokers.mt5.gateway import Mt5Gateway, Priority
from trading_house.brokers.mt5.retcodes import reject_reason_for
from trading_house.constitution.binding import VenueBinding
from trading_house.core.clock import Clock
from trading_house.core.errors import ConfigurationError
from trading_house.core.instruments import InstrumentContract
from trading_house.core.schemas import OrderIntent, PositionState
from trading_house.core.values import BookId, InstrumentId, PositiveQuantity, Price
from trading_house.core.venue import ExecutionOutcome, PrecheckResult, VenueRef

_PHASE_3 = "order submission arrives in Phase 3 with the intent ledger"


class Mt5BrokerAdapter:
    """Read-only BrokerAdapter over one MetaTrader 5 terminal."""

    def __init__(self, gateway: Mt5Gateway, binding: VenueBinding, *, clock: Clock) -> None:
        self._gateway = gateway
        self._clock = clock
        self._server_symbols = {
            instrument_id: bound.server_symbol
            for instrument_id, bound in binding.instruments.items()
        }
        self._instrument_ids = {
            server_symbol: instrument_id
            for instrument_id, server_symbol in self._server_symbols.items()
        }
        self._magic_ranges = {
            book: bound.magic_range for book, bound in binding.books.items()
        }

    def _server_symbol_for(self, instrument_id: InstrumentId) -> str:
        server_symbol = self._server_symbols.get(instrument_id)
        if server_symbol is None:
            raise ConfigurationError()
        return server_symbol
```

Then implement the remaining methods on that class:

- `__init__` above builds `instrument_id -> server_symbol`, its inverse, and `book -> magic_range` from the signed binding.
- `_server_symbol_for(instrument_id)` raising `ConfigurationError` when the instrument is not in the signed binding.
- `describe_instrument` — `gateway.call(Priority.MARKET_DATA, ...)` to fetch `Mt5SymbolInfo`, then `to_instrument_contract(info, instrument_id=instrument_id)`. Raise `ConfigurationError` when the symbol is unknown to the terminal.
- `snapshot` — one `symbol_tick` per instrument, assembled into `MarketSnapshot` with `taken_at` from the clock. Skip instruments the terminal has no tick for rather than fabricating one.
- `precheck` — build an `order_check` request from the intent, call it at `Priority.ORDER`, map the retcode with `reject_reason_for`, and return `PrecheckResult`. This is non-mutating.
- `reconcile(book)` — `positions()` at `Priority.RECONCILE`, filter to the book's magic range from the binding, map each to `PositionState`, and return a `ReconciliationReport`. Positions whose magic falls in no declared range go into `unmatched_venue_refs`.
- `health()` — `VenueHealth(connected=..., server_utc_offset_seconds=gateway.server_utc_offset_seconds, last_quote_age_seconds=...)`.
- `submit`, `amend_protection`, `close` — each raising `NotImplementedError("order submission arrives in Phase 3 with the intent ledger")`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit/brokers/mt5 -q --no-cov`

Expected: PASS.

- [ ] **Step 5: Verify the static gates and regression**

Run: `uv run ruff format . && uv run ruff format --check . && uv run ruff check . && uv run mypy && uv run pytest tests/unit tests/property tests/acceptance -q --no-cov`

Expected: all exit `0`.

- [ ] **Step 6: Commit**

```bash
git add src/trading_house/brokers/mt5/adapter.py tests/unit/brokers/mt5/test_adapter.py
git commit -m "feat: implement the read-only half of the broker adapter"
```

---

### Task 10: The Health Gate's Fifth Step

**Files:**
- Modify: `src/trading_house/ops/health.py`
- Modify: `src/trading_house/cli.py`
- Modify: `tests/unit/ops/test_health.py`
- Modify: `tests/integration/ops/test_health.py`

**Interfaces:**
- Produces: `BookReconciler` protocol; `HealthReport` gains `books_reconciled: tuple[BookId, ...]` and `open_positions: int`.
- Consumes: `ReconciliationReport` from `brokers.base`.

- [ ] **Step 1: Write the failing tests**

Extend the ordered-gate test in `tests/unit/ops/test_health.py` so the expected call order becomes:

```python
EXPECTED_ORDER = [
    "constitution.verify",
    "database.connect",
    "database.revision",
    "audit.verify",
    "venue.reconcile",
    "audit.append:startup",
    "audit.append:constitution_loaded",
]
```

and add:

```python
def test_reconciliation_runs_before_the_startup_events_are_appended() -> None:
    harness = Harness()

    _service(harness).run()

    assert harness.calls.index("venue.reconcile") < harness.calls.index(
        "audit.append:startup"
    )


def test_open_positions_are_reported_not_fatal() -> None:
    """Phase 1 has created no positions and has no intent ledger to compare
    against, so a manual demo trade must not make health permanently red."""

    harness = Harness(open_positions=2)

    report = _service(harness).run()

    assert report.ready is True
    assert report.open_positions == 2


def test_every_declared_book_is_reconciled() -> None:
    harness = Harness()

    report = _service(harness).run()

    assert set(report.books_reconciled) == {"fx_scalp", "fx_swing"}
```

Update the `Harness` fake to record `"venue.reconcile"` and return a mapping of `BookId` to `ReconciliationReport`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit/ops/test_health.py -q --no-cov`

Expected: FAIL — `HealthService` takes no reconciler and the call order has six entries, not seven.

- [ ] **Step 3: Add the fifth step**

In `src/trading_house/ops/health.py`, add the protocol:

```python
class BookReconciler(Protocol):
    def __call__(self) -> Mapping[BookId, ReconciliationReport]: ...
```

Add `reconcile_books: BookReconciler` to `HealthService.__init__`, run it after `ledger.verify()` and before the two appends, and extend `HealthReport` with `books_reconciled: tuple[BookId, ...]` and `open_positions: int`.

Reconciliation reports; it does not fail readiness. Add a comment saying so, and why: Phase 1 owns no positions and has no intent ledger, so a mismatch is not yet computable. Mismatch detection arrives in Phase 3.

In `src/trading_house/cli.py`, wire the reconciler in the `health` command from an `Mt5BrokerAdapter` when the venue binding is present, and include `books_reconciled` and `open_positions` in the emitted JSON.

- [ ] **Step 3b: Append gateway lifecycle events to the audit ledger**

Phase 0.5's handoff contract requires gateway, configuration and operational events to be appended through the Phase 0 audit interface. Nothing so far does this, and it is the row that gives you a hash-chained forensic history of every terminal connection — which is what you want when a broker dispute happens.

Keep the gateway decoupled from the audit ledger: it emits, the wiring records. Add to `Mt5Gateway.__init__` an optional hook, defaulting to a no-op:

```python
        on_event: Callable[[str, Mapping[str, JsonValue]], None] | None = None,
```

Call it at four points, each with a payload carrying no credentials and no DSN:

| Event type | When | Payload |
|---|---|---|
| `gateway.connected` | after `initialize()` succeeds | `{"venue": "mt5"}` |
| `gateway.demo_verified` | after the `trade_mode` check passes | `{"venue": "mt5", "trade_mode": <int>}` |
| `gateway.disconnected` | in `stop()`, after shutdown | `{"venue": "mt5", "served": <int>, "failed": <int>}` |
| `gateway.reconciled` | in `mark_reconciled()` | `{"venue": "mt5"}` |

Note there is deliberately no event for a *failed* demo check carrying the account number — a refusal is logged as `gateway.disconnected` and the typed error carries the rest. Account identifiers are not audit payload material.

In `cli.py`'s `health` command, pass a hook that builds an `AuditEvent` and appends it through the existing `PostgresAuditLedger`, reusing the `_event` construction already in `ops/health.py`.

Add a test asserting all four events fire in order for a successful start–reconcile–stop cycle, and that no payload contains an account number:

```python
def test_gateway_lifecycle_is_audited() -> None:
    events: list[tuple[str, Mapping[str, object]]] = []
    terminal = FakeTerminal()
    gateway = Mt5Gateway(
        terminal, clock=SystemClock(), on_event=lambda name, payload: events.append((name, payload))
    )
    gateway.start()
    gateway.mark_reconciled()
    gateway.stop()

    assert [name for name, _ in events] == [
        "gateway.connected",
        "gateway.demo_verified",
        "gateway.reconciled",
        "gateway.disconnected",
    ]
    for _, payload in events:
        assert "account" not in str(payload).lower()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/unit tests/integration/ops -q --no-cov`

Expected: PASS. Integration health tests need Docker.

- [ ] **Step 5: Verify the static gates**

Run: `uv run ruff format . && uv run ruff format --check . && uv run ruff check . && uv run mypy`

Expected: all exit `0`.

- [ ] **Step 6: Commit**

```bash
git add src/trading_house/ops/health.py src/trading_house/cli.py tests
git commit -m "feat: gate readiness on venue reconciliation"
```

---

### Task 11: Live Terminal Tests, Acceptance, and Documentation

**Files:**
- Create: `tests/live/test_mt5_terminal.py`
- Create: `tests/acceptance/test_phase1.py`
- Modify: `README.md`
- Modify: `pyproject.toml` (add `tests/live` to `testpaths`)

**Interfaces:**
- Consumes: everything from Tasks 1–10.

- [ ] **Step 1: Write the live terminal tests**

```python
# tests/live/test_mt5_terminal.py
"""Tests that need a running MetaTrader 5 terminal on a DEMO account.

Marked ``mt5`` and skipped when no terminal is present. Never run in CI.
"""

from __future__ import annotations

import sys

import pytest

pytestmark = pytest.mark.mt5


def _terminal_available() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import MetaTrader5 as mt5
    except ImportError:
        return False
    if not mt5.initialize():
        return False
    mt5.shutdown()
    return True


pytestmark = [
    pytest.mark.mt5,
    pytest.mark.skipif(not _terminal_available(), reason="no MetaTrader 5 terminal"),
]


def test_gateway_starts_against_a_demo_account() -> None:
    from trading_house.brokers.mt5.gateway import Mt5Gateway, Priority
    from trading_house.brokers.mt5.terminal import Mt5Terminal
    from trading_house.core.clock import SystemClock

    with Mt5Gateway(Mt5Terminal(), clock=SystemClock()) as gateway:
        tick = gateway.call(Priority.MARKET_DATA, lambda t: t.symbol_tick("EURUSD"))

    assert tick is not None
    assert tick.ask >= tick.bid
    assert tick.observed_at.tzinfo is not None


def test_a_real_symbol_maps_to_a_valid_contract() -> None:
    from trading_house.brokers.mt5.contracts import to_instrument_contract
    from trading_house.brokers.mt5.gateway import Mt5Gateway, Priority
    from trading_house.brokers.mt5.terminal import Mt5Terminal
    from trading_house.core.clock import SystemClock

    with Mt5Gateway(Mt5Terminal(), clock=SystemClock()) as gateway:
        info = gateway.call(Priority.MARKET_DATA, lambda t: t.symbol_info("EURUSD"))

    assert info is not None
    contract = to_instrument_contract(info, instrument_id="fx.eurusd")
    assert contract.price_increment > 0
    assert contract.min_stop_distance > 0
    assert len(contract.supported_fills) >= 1


def test_server_time_offset_is_computed() -> None:
    from trading_house.brokers.mt5.gateway import Mt5Gateway
    from trading_house.brokers.mt5.terminal import Mt5Terminal
    from trading_house.core.clock import SystemClock

    with Mt5Gateway(Mt5Terminal(), clock=SystemClock()) as gateway:
        assert isinstance(gateway.server_utc_offset_seconds, int)
```

- [ ] **Step 2: Write the Phase 1 acceptance test**

```python
# tests/acceptance/test_phase1.py
"""Phase 1 acceptance: the read-only gateway holds its boundaries."""

from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MT5_PACKAGE = PROJECT_ROOT / "src" / "trading_house" / "brokers" / "mt5"


def test_only_the_terminal_module_imports_metatrader5() -> None:
    offenders = []
    for path in sorted((PROJECT_ROOT / "src").rglob("*.py")):
        if path.name == "terminal.py" and path.parent == MT5_PACKAGE:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import) and any(
                a.name.split(".")[0] == "MetaTrader5" for a in node.names
            ):
                offenders.append(path.name)
            elif (
                isinstance(node, ast.ImportFrom)
                and node.module
                and node.module.split(".")[0] == "MetaTrader5"
            ):
                offenders.append(path.name)
    assert offenders == []


def test_the_phase_sends_no_orders() -> None:
    """Every mutating adapter method must refuse in this phase."""

    source = (MT5_PACKAGE / "adapter.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    refusing = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and any(
            isinstance(inner, ast.Raise)
            and isinstance(inner.exc, ast.Call)
            and getattr(inner.exc.func, "id", "") == "NotImplementedError"
            for inner in ast.walk(node)
        )
    }
    assert {"submit", "amend_protection", "close"} <= refusing


def test_no_order_send_call_exists_anywhere_in_source() -> None:
    """order_send is the only MetaTrader 5 call that moves money."""

    for path in sorted((PROJECT_ROOT / "src").rglob("*.py")):
        assert "order_send" not in path.read_text(encoding="utf-8"), path.name


def test_the_pure_modules_import_on_any_platform() -> None:
    from trading_house.brokers.mt5 import contracts, magic, retcodes

    assert contracts is not None
    assert magic is not None
    assert retcodes is not None
```

- [ ] **Step 3: Run both suites**

Run: `uv run pytest tests/acceptance/test_phase1.py -q --no-cov`

Expected: PASS.

Run: `uv run pytest tests/live -q --no-cov -m mt5`

Expected: PASS on this machine with a demo terminal running; SKIPPED otherwise. If any live test fails against a real terminal, **stop and report** — that is real evidence the mapping is wrong, and it is exactly what this phase exists to discover.

- [ ] **Step 4: Update the README**

Add a "Phase 1 — MT5 gateway" section documenting: the demo-only requirement and the `trade_mode` guard; that the phase sends no orders; how to run the broker audit; how to run the live tests (`-m mt5`); and the operator commands' new `health` output fields. State plainly that `submit`, `amend_protection` and `close` refuse until Phase 3.

Add `tests/live` to `testpaths` in `pyproject.toml` so the marker-based skip works from a bare `uv run pytest`.

- [ ] **Step 5: Run the full verification gate**

Run: `uv lock --check`

Run: `uv run ruff format --check . && uv run ruff check . && uv run mypy`

Run: `uv run pytest`

Run: `uv run trading-house constitution verify && uv run trading-house constitution binding`

Run: `git status --short`

Expected: every command exits `0`; pytest reports no failures with coverage at or above 95%; git status shows no stray artifacts.

- [ ] **Step 6: Commit**

```bash
git add tests/live tests/acceptance/test_phase1.py README.md pyproject.toml
git commit -m "docs: accept the phase 1 read-only gateway"
```

---

## Final Verification Gate

After Task 11, run these from the repository root without relying on earlier output:

```bash
uv lock --check
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest
uv run trading-house constitution verify
uv run trading-house constitution binding
uv run trading-house health
git status --short
```

Expected results:

- every command exits `0`;
- pytest reports no failures and coverage at or above 95% with `terminal.py` omitted;
- `terminal.py` is under its 80-statement cap;
- `health` reports ready including `books_reconciled` and `open_positions`;
- no `MetaTrader5` import exists outside `brokers/mt5/terminal.py`;
- no `order_send` call exists anywhere in `src/`;
- the Phase 0 and Phase 0.5 acceptance suites are still green.

## Deliberately Not In This Plan

| Item | Where it lands |
|---|---|
| `submit`, `amend_protection`, `close` | Phase 3, with the intent ledger and `UNKNOWN`-state recovery |
| Market-data ingest, quality gates, storage | Phase 1.5, its own spec |
| Reconciliation mismatch as a safe-mode trigger | Phase 3 — needs the intent ledger to compute a mismatch |
| Real trading-session calendars | With the equity work; Phase 1 derives a default |
| Multi-account or multi-terminal operation | Later; needs one terminal per account in portable mode |
| `GatewayMetrics` wired to alerting | Phase 3, when P0 traffic and safe mode exist |

## Handoff To Phase 3

Phase 3 completes `BrokerAdapter`. It must:

- persist the intent **before** submission, deriving magic with `derive_magic` so a lost response is recoverable;
- treat magic as a locator, not an identity — scope recovery queries by magic **and** time window, with the intent ledger authoritative;
- branch recovery on `recovery_for()`, never widening a stop;
- turn a reconciliation mismatch into a safe-mode trigger, which this phase deliberately only reports;
- wire P0 queue depth to the safe-mode signal the gateway already measures.
