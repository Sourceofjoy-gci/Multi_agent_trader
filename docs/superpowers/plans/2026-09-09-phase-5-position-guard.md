# Phase 5 — The Position Guard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Guarantee that every position this system opened is, at every moment, carrying the stop the ledger says it carries — and that a missing stop is restored or escalated within two cycles.

**Architecture:** A supervised daemon runs one cycle per second. Each cycle reads open positions from the broker, reads what the append-only position store last recorded, calls a pure `decide()` per position, executes the single action it returns, and appends a row only when something changed. The rule that a stop may only move toward profit is enforced twice — `decide()` cannot emit a widening, and `amend_protection` refuses one — with a test that reaches each layer independently.

**Tech Stack:** Python 3.12, Pydantic v2, `Decimal` for prices, psycopg 3, PostgreSQL 18 + Alembic, pytest, Hypothesis, testcontainers, mypy strict, Ruff.

**Spec:** `docs/superpowers/specs/2026-09-09-phase-5-position-guard-design.md`

## Global Constraints

- **Every `uv` command must be prefixed `UV_SYSTEM_CERTS=1`** or it fails TLS verification in this environment.
- The mypy gate is `UV_SYSTEM_CERTS=1 uv run mypy` with **no path arguments** — the project scopes it via `packages = ["trading_house"]`.
- **`execution/` imports `core/` and `database/` and nothing else from this project** — not `brokers/`, not `risk/`, not `features/`, not `marketdata/`. An acceptance test pins it.
- **Only `brokers/mt5/terminal.py` may import MetaTrader5, and only it may contain the string `order_send`.** Acceptance tests pin both.
- **`terminal.py` is at 79 of an enforced 80-statement cap** (`test_the_terminal_module_stays_thin`). The SLTP call must be call-and-delegate with its mapping in `boundary.py`, which has no cap. **Do not raise the cap.**
- Line length 100. mypy strict. **No new `# type: ignore` in `src/`.**
- **Do not add a `# noqa` for a rule this project does not enable.** The ruff select list is exactly `["E", "F", "I", "B", "UP", "SIM", "RUF", "S", "PT"]`. A directive outside it suppresses nothing and `RUF100` fails the build. This defect class has cost this project seven rounds across earlier phases.
- Enums subclass `str, Enum` with `# noqa: UP042`. This project has no `StrEnum`; do not introduce one.
- **Every price and stop is `Decimal`.** MT5 returns floats; convert with `decimal_of(value)` (`Decimal(str(value))`), never `Decimal(float)`. **R-multiples are `FiniteFloat`** — `r_multiple_open`, `mae_r`, `mfe_r` are analytics, not money.
- **No credential, DSN, account number or raw broker message in any error, log or audit payload.**
- Integration tests need Docker. Baseline: **1058 passed / 4 skipped** at `45d1b8e` with Docker up and the market open.

## What Already Exists

Verified present at `56f3d9d`. Do not redefine any of it.

```python
# trading_house.core.venue   — the two neutral records Phase 4 created
@dataclass(frozen=True, slots=True)
class DealRecord:
    magic: int;  server_symbol: str;  volume: Decimal
    position_ticket: int;  dealt_at: datetime
    # NO entry direction — a closing deal is indistinguishable from the opening one

@dataclass(frozen=True, slots=True)
class PositionRecord:
    magic: int;  server_symbol: str;  volume: Decimal;  position_ticket: int
    # NO sl, price_open, is_buy or opened_at — the minimal shape the reconciler needed

# trading_house.core.schemas
class PositionState(CanonicalModel):
    intent_id: NonEmptyStr | None;  strategy_id: NonEmptyStr;  book: BookId
    instrument_id: InstrumentId;  side: Side;  quantity: PositiveQuantity
    open_price: Price;  current_sl: Price;  current_tp: Price | None
    opened_at_utc: datetime
    lifecycle: Literal["OPEN_PROTECTED","BREAKEVEN_ELIGIBLE","TRAILING","EXIT_PENDING","CLOSED"]
    r_multiple_open: FiniteFloat;  mae_r: FiniteFloat;  mfe_r: FiniteFloat
    initial_risk_distance: Price;  venue_ref: VenueRef

# trading_house.brokers.mt5.boundary
@dataclass(frozen=True, slots=True)
class Mt5Position:
    ticket: int;  magic: int;  server_symbol: str;  volume: float
    price_open: float;  sl: float;  tp: float | None;  is_buy: bool;  opened_at: datetime

class TerminalPort(Protocol):
    def positions(self) -> Sequence[Mt5Position] | None: ...          # None means ERROR
    def history_deals(self, start, end) -> Sequence[Mt5Deal] | None: ...  # None means ERROR
    def send_order(self, request: Mapping[str, object]) -> Mt5SendResult | None: ...
    def autotrading_enabled(self) -> bool: ...
    # plus initialize, shutdown, account_trade_mode, terminal_connected,
    # server_utc_offset_seconds, symbol_info, symbol_tick, copy_rates_range,
    # order_check, last_error

# trading_house.execution.reconciler
MATCH_LOOKBACK = timedelta(seconds=60);  RESOLUTION_TIMEOUT = timedelta(seconds=30)
class Verdict(str, Enum);  class Resolution;  class DealSource(Protocol)
def verdict(...) -> Resolution
def reconcile_all(ledger, deals, clock) -> Mapping[str, Verdict]
def require_clean_ledger(...) -> None

# trading_house.execution.ledger
class IntentLedger(Protocol);  class PostgresIntentLedger;  NON_TERMINAL_STATES

# trading_house.brokers.mt5.gateway
def mark_stale(self) -> None      # the only thing resembling safe mode
def mark_reconciled(self) -> None
```

**`adapter.amend_protection()` is the last method still raising `NotImplementedError`.** `tests/acceptance/test_phase1.py`'s `REFUSING_METHODS` is now `("amend_protection",)` and asserts exactly that — **Task 4 breaks it and must narrow it, not delete it.**

**There is no alerting and no safe-mode subsystem.** What exists is `Gateway.mark_stale()`, the hash-chained audit ledger (`audit.repository.append`), and the gateway's optional `_on_event` hook. "Escalate" means those three things and nothing more.

**Migrations run to `0005_one_submitting_per_intent`.** The new one is `0006`.

## File Structure

| File | Responsibility |
|---|---|
| `core/venue.py` | `PositionRecord` gains `sl`, `price_open`, `is_buy`, `opened_at`; `DealRecord` gains `entry` |
| `brokers/mt5/boundary.py` | `DealEntry` enum, the deal-entry mapping, the SLTP request builder |
| `brokers/mt5/terminal.py` | one call-and-delegate SLTP method — the cap allows one statement |
| `migrations/versions/0006_position_events.py` | the append-only position event table |
| `execution/positions.py` | the position event store |
| `execution/guard.py` | `GuardAction`, `decide()` — pure |
| `execution/loop.py` | the daemon cycle |
| `brokers/mt5/adapter.py` | `amend_protection()`, refusing a widening stop |
| `cli.py` | `guard run`, `guard status` |

---

### Task 1: Widen the neutral records and close the deal-entry gap

The guard needs a position's stop, entry price and direction, and it needs to tell a closing deal from an opening one. Neither is available today.

**Files:**
- Modify: `src/trading_house/core/venue.py`
- Modify: `src/trading_house/brokers/mt5/boundary.py`
- Modify: `src/trading_house/brokers/mt5/adapter.py` (the two mapping sites)
- Test: `tests/unit/core/test_venue.py`, `tests/unit/brokers/mt5/test_boundary.py`, `tests/unit/brokers/mt5/test_mt5_field_names.py`

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces:
  ```python
  # trading_house.core.venue
  class DealEntry(str, Enum):  # noqa: UP042
      IN = "in"        # opened or added to a position
      OUT = "out"      # closed or reduced a position
      INOUT = "inout"  # reversal: closed one side and opened the other

  @dataclass(frozen=True, slots=True)
  class DealRecord:
      magic: int; server_symbol: str; volume: Decimal
      position_ticket: int; dealt_at: datetime; entry: DealEntry

  @dataclass(frozen=True, slots=True)
  class PositionRecord:
      magic: int; server_symbol: str; volume: Decimal; position_ticket: int
      stop_loss: Decimal | None    # None when the broker reports 0 — NO stop
      open_price: Decimal; is_buy: bool; opened_at: datetime
  ```

**`stop_loss` is `Decimal | None`, and `None` is the whole point.** MT5 reports "no stop" as `0.0`, and `0.0` is also a syntactically valid price. Carrying the zero through would make "unprotected" indistinguishable from "protected at zero", and the guard's entire job is telling those apart.

**Warning — required fields break construction sites.** `PositionRecord` is built at 3 places across `adapter.py`, `tests/unit/brokers/mt5/test_adapter.py` and `tests/unit/execution/test_reconciler.py`; `DealRecord` at 2. Grep before assuming:
```bash
grep -rn "PositionRecord(\|DealRecord(" src/ tests/ --include=*.py
```

- [ ] **Step 1: Verify the MT5 constants before writing them down**

Do not take these from the plan. Check them against the installed package and report what you found:

```python
import MetaTrader5 as mt5
print(mt5.DEAL_ENTRY_IN, mt5.DEAL_ENTRY_OUT, mt5.DEAL_ENTRY_INOUT, mt5.DEAL_ENTRY_OUT_BY)
print([f for f in dir(mt5.TradeDeal) if not f.startswith("_")])
```

The plan expects `DEAL_ENTRY_IN == 0`, `OUT == 1`, `INOUT == 2`, `OUT_BY == 3`, and a deal field named `entry`. **If any differs, follow the package and say so in your report.** An earlier phase had a field name in a brief that was simply wrong, and only checking caught it.

- [ ] **Step 2: Write the failing tests**

In `tests/unit/brokers/mt5/test_mt5_field_names.py`, beside the existing assertions, pin what you just verified:

```python
def test_the_deal_entry_constants_are_what_the_mapping_assumes() -> None:
    """These four integers decide whether a deal opened or closed a position.
    Reading them wrong makes every close look like an open, and the guard
    would record a closed position as still live."""

    assert (mt5.DEAL_ENTRY_IN, mt5.DEAL_ENTRY_OUT, mt5.DEAL_ENTRY_INOUT) == (0, 1, 2)


def test_a_deal_exposes_its_entry_direction() -> None:
    assert "entry" in dir(mt5.TradeDeal)
```

In `tests/unit/brokers/mt5/test_boundary.py`:

```python
@pytest.mark.parametrize(
    ("raw", "expected"),
    [(0, DealEntry.IN), (1, DealEntry.OUT), (2, DealEntry.INOUT), (3, DealEntry.OUT)],
)
def test_deal_entry_maps_every_mt5_code(raw: int, expected: DealEntry) -> None:
    """DEAL_ENTRY_OUT_BY (3) closes a position against an opposing one. It is
    still a close, so it maps to OUT -- treating it as an open would leave a
    closed position on the books forever."""

    assert deal_entry_of(raw) is expected


def test_an_unknown_entry_code_is_refused_not_guessed() -> None:
    """A code MT5 adds later must fail loudly. Defaulting it to IN would
    silently mark closes as opens."""

    with pytest.raises(ConfigurationError):
        deal_entry_of(99)


def test_a_zero_stop_becomes_none_not_zero() -> None:
    """MT5 reports "no stop" as 0.0, which is also a valid price. Carrying the
    zero through makes unprotected indistinguishable from protected-at-zero,
    and telling those apart is the guard's entire job."""

    assert position_record_of(_mt5_position(sl=0.0)).stop_loss is None
    assert position_record_of(_mt5_position(sl=1.09700)).stop_loss == Decimal("1.09700")
```

- [ ] **Step 3: Run and watch them fail**

```bash
UV_SYSTEM_CERTS=1 uv run pytest tests/unit/brokers tests/unit/core -q --no-cov
```

- [ ] **Step 4: Add `DealEntry` and widen the records**

In `core/venue.py`, add `DealEntry` and the new fields exactly as the Interfaces block gives them.

In `boundary.py`, add the pure mappers — they belong here, not in `terminal.py`, which is one statement from its cap:

```python
_DEAL_ENTRY = {0: DealEntry.IN, 1: DealEntry.OUT, 2: DealEntry.INOUT, 3: DealEntry.OUT}


def deal_entry_of(raw: int) -> DealEntry:
    """Map MT5's entry code. An unknown code raises rather than defaulting."""

    entry = _DEAL_ENTRY.get(raw)
    if entry is None:
        raise ConfigurationError()
    return entry
```

Add `entry: int` to `Mt5Deal` and populate it in `terminal.py`'s existing mapping — that is one added line inside an existing constructor call, not a new statement.

- [ ] **Step 5: Repair every construction site, then commit**

```bash
grep -rn "PositionRecord(\|DealRecord(" src/ tests/ --include=*.py
UV_SYSTEM_CERTS=1 uv run pytest tests/unit tests/acceptance -q --no-cov
UV_SYSTEM_CERTS=1 uv run ruff format . && UV_SYSTEM_CERTS=1 uv run ruff check . && UV_SYSTEM_CERTS=1 uv run mypy
git add src tests
git commit -m "feat: carry a deal's entry direction and a position's stop"
```

---

### Task 2: The append-only position event store

**Files:**
- Create: `migrations/versions/0006_position_events.py`
- Create: `src/trading_house/execution/positions.py`
- Test: `tests/integration/execution/test_positions.py`

**Interfaces:**
- Consumes: `ConnectionFactory` (copy the Protocol from `execution/ledger.py`).
- Produces:
  ```python
  @dataclass(frozen=True, slots=True)
  class PositionEvent:
      seq: int; position_ticket: int; lifecycle: str
      event_time: datetime; payload: Mapping[str, Any]

  class PositionStore(Protocol):
      def append(self, position_ticket: int, lifecycle: str, event_time: datetime,
                 payload: Mapping[str, Any]) -> None: ...
      def latest(self, position_ticket: int) -> PositionEvent | None: ...
      def open_positions(self) -> tuple[PositionEvent, ...]: ...

  class PostgresPositionStore:  # satisfies PositionStore
      def __init__(self, connection_factory: ConnectionFactory) -> None: ...
  ```

`open_positions()` returns the latest event per ticket whose lifecycle is not `CLOSED` — the guard's working set.

**Note the deliberate asymmetry in naming.** The store's `open_positions()` is
what we *recorded*; the venue port's `positions_now()` (Task 5) is what the
broker *has*. They answer different questions and the guard's whole job is
comparing them, so they must not share a name — and `positions_now` is already
the name Phase 4 gave the equivalent method on `DealSource`.

- [ ] **Step 1: Write the migration**

Follow `0004_intent_events.py` exactly — same `SET ROLE`, schema reuse, `REVOKE ALL`, mutation-rejecting triggers, narrow grants. The `execution` schema already exists, so do **not** create it again.

```python
revision: str = "0006_position_events"
down_revision: str | None = "0005_one_submitting_per_intent"
```

Table `execution.position_events`: `seq BIGSERIAL PRIMARY KEY`, `position_ticket BIGINT NOT NULL`, `lifecycle TEXT NOT NULL`, `event_time TIMESTAMPTZ NOT NULL`, `recorded_at TIMESTAMPTZ NOT NULL DEFAULT now()`, `payload JSONB NOT NULL`, with a `CHECK` pinning lifecycle to the five `PositionState` values. Index on `(position_ticket, seq DESC)`. Grant the runtime role `SELECT, INSERT` on the table and `USAGE` on the sequence — **`position_events_seq_seq`**; confirm with `SELECT pg_get_serial_sequence('execution.position_events','seq')` if an insert is refused.

Reuse `execution.reject_intent_event_mutation()` if it is generic enough; otherwise add a sibling function. Say in your report which you did.

- [ ] **Step 2: Write the failing integration tests**

Follow `tests/integration/execution/test_ledger.py` — including its two-layer append-only proof, which is the pattern that matters:

```python
@pytest.mark.integration
def test_latest_is_the_most_recent_event(store) -> None:
    store.append(7, "OPEN_PROTECTED", NOW, {"stop": "1.09700"})
    store.append(7, "OPEN_PROTECTED", NOW, {"stop": "1.09800"})

    assert store.latest(7).payload["stop"] == "1.09800"


@pytest.mark.integration
def test_open_positions_excludes_closed_ones(store) -> None:
    """The guard's working set. A closed position that kept appearing would be
    checked forever against a broker that no longer has it."""

    store.append(7, "OPEN_PROTECTED", NOW, {})
    store.append(8, "OPEN_PROTECTED", NOW, {})
    store.append(8, "CLOSED", NOW, {})

    assert [e.position_ticket for e in store.open_positions()] == [7]


@pytest.mark.integration
def test_the_runtime_role_has_no_update_privilege(database) -> None:
    """Proves the GRANT layer. sqlstate 42501 is raised before Postgres ever
    consults the trigger, so this test alone says nothing about the trigger."""

    store = PostgresPositionStore(lambda: open_runtime_connection(database.runtime_dsn))
    store.append(7, "OPEN_PROTECTED", NOW, {})

    with open_runtime_connection(database.runtime_dsn) as connection, pytest.raises(
        psycopg.errors.InsufficientPrivilege
    ) as caught:
        connection.execute("UPDATE execution.position_events SET lifecycle = 'CLOSED'")

    assert caught.value.sqlstate == "42501"


@pytest.mark.integration
def test_the_trigger_refuses_a_role_that_does_have_privilege(database) -> None:
    """Proves the TRIGGER layer, which the test above cannot reach. Connect as
    a role holding UPDATE and assert P0001 -- otherwise a migration typo that
    dropped the trigger would ship green."""

    store = PostgresPositionStore(lambda: open_runtime_connection(database.runtime_dsn))
    store.append(7, "OPEN_PROTECTED", NOW, {})

    with psycopg.connect(database.migration_dsn) as connection, pytest.raises(
        psycopg.errors.RaiseException
    ) as caught:
        connection.execute("SET ROLE trading_house_owner")
        connection.execute("UPDATE execution.position_events SET lifecycle = 'CLOSED'")

    assert caught.value.sqlstate == "P0001"
```

- [ ] **Step 3: Write the store, run to green, commit**

Copy `execution/ledger.py`'s structure: module-level SQL constants, `ConnectionFactory` Protocol, a private failure exception, a class taking the factory. `append` **commits before returning**. Use `psycopg.types.json.Jsonb` for the payload, and store every `Decimal` as a string.

```bash
UV_SYSTEM_CERTS=1 uv run pytest tests/integration/execution -q --no-cov -m integration
UV_SYSTEM_CERTS=1 uv run ruff format . && UV_SYSTEM_CERTS=1 uv run ruff check . && UV_SYSTEM_CERTS=1 uv run mypy
git add migrations src/trading_house/execution tests/integration/execution
git commit -m "feat: append-only position event store"
```

---

### Task 3: The pure decision function

**Files:**
- Create: `src/trading_house/execution/guard.py`
- Test: `tests/unit/execution/test_guard.py`

**Interfaces:**
- Consumes: `PositionRecord`, `DealRecord`, `DealEntry` (Task 1); `PositionEvent` (Task 2).
- Produces:
  ```python
  class ActionKind(str, Enum):  # noqa: UP042
      NOTHING = "nothing"
      RECORD_ONLY = "record_only"     # the broker disagrees; believe the broker
      RESTORE_STOP = "restore_stop"   # sl is missing; put it back
      TIGHTEN_STOP = "tighten_stop"
      ADOPT_ORPHAN = "adopt_orphan"
      RECORD_CLOSED = "record_closed"
      ESCALATE = "escalate"

  @dataclass(frozen=True, slots=True)
  class GuardAction:
      kind: ActionKind
      stop_loss: Decimal | None = None   # the stop to write, for the three that write one
      reason: str = ""

  def decide(*, observed: PositionRecord | None, recorded: PositionEvent | None,
             is_buy: bool, recorded_stop: Decimal | None,
             min_stop_distance: Decimal, default_stop_distance: Decimal | None,
             failed_restores: int) -> GuardAction
  ```

**`decide()` must never return `TIGHTEN_STOP` or `RESTORE_STOP` with a stop that is worse than `recorded_stop`.** For a BUY a stop only ever rises; for a SELL it only ever falls. This is the first of the two enforcement layers (D-3); Task 4 builds the second.

- [ ] **Step 1: Write the failing tests**

```python
BUY = True
STOP = Decimal("1.09700")


def _observed(**overrides: object) -> PositionRecord:
    fields: dict[str, object] = {
        "magic": 110042, "server_symbol": "EURUSD", "volume": Decimal("0.25"),
        "position_ticket": 7, "stop_loss": STOP, "open_price": Decimal("1.10000"),
        "is_buy": True, "opened_at": NOW,
    }
    fields.update(overrides)
    return PositionRecord(**fields)  # type: ignore[arg-type]


def _decide(**overrides: object) -> GuardAction:
    args: dict[str, object] = {
        "observed": _observed(), "recorded": _recorded(stop=STOP), "is_buy": BUY,
        "recorded_stop": STOP, "min_stop_distance": Decimal("0.00001"),
        "default_stop_distance": Decimal("0.00300"), "failed_restores": 0,
    }
    args.update(overrides)
    return decide(**args)  # type: ignore[arg-type]


def test_a_matching_stop_does_nothing_and_writes_nothing() -> None:
    """The overwhelmingly common cycle. If this wrote a row, the table would
    grow by 86,400 rows per position per day and bury every real event."""

    assert _decide().kind is ActionKind.NOTHING


def test_a_missing_stop_is_restored_to_the_recorded_one() -> None:
    """sl == 0 at the broker means unprotected. This is the case the guard
    exists for."""

    action = _decide(observed=_observed(stop_loss=None))

    assert action.kind is ActionKind.RESTORE_STOP
    assert action.stop_loss == STOP


def test_a_second_failed_restore_escalates_instead_of_looping() -> None:
    """Retrying forever against a broker that keeps refusing is how a guard
    spins while a position sits unprotected."""

    action = _decide(observed=_observed(stop_loss=None), failed_restores=2)

    assert action.kind is ActionKind.ESCALATE


def test_a_broker_stop_that_disagrees_is_recorded_not_overwritten() -> None:
    """The broker is the truth about where the stop actually is. Overwriting it
    with our record would move a live stop based on stale belief."""

    action = _decide(observed=_observed(stop_loss=Decimal("1.09750")))

    assert action.kind is ActionKind.RECORD_ONLY
    assert action.stop_loss == Decimal("1.09750")


def test_an_orphan_with_a_stop_is_adopted_at_that_stop() -> None:
    action = _decide(recorded=None, recorded_stop=None)

    assert action.kind is ActionKind.ADOPT_ORPHAN
    assert action.stop_loss == STOP


def test_an_orphan_without_a_stop_is_adopted_at_the_supplied_distance() -> None:
    """The distance comes from the book's signed k_sigma against current ATR,
    computed by the caller. decide() never invents one."""

    action = _decide(
        observed=_observed(stop_loss=None), recorded=None, recorded_stop=None
    )

    assert action.kind is ActionKind.ADOPT_ORPHAN
    assert action.stop_loss == Decimal("1.09700")   # 1.10000 - 0.00300


def test_an_orphan_without_a_stop_or_a_distance_escalates() -> None:
    """No ATR means no defensible distance. Inventing one would be exactly the
    fabrication this project refuses everywhere else."""

    action = _decide(
        observed=_observed(stop_loss=None), recorded=None, recorded_stop=None,
        default_stop_distance=None,
    )

    assert action.kind is ActionKind.ESCALATE


def test_a_vanished_position_is_recorded_closed() -> None:
    assert _decide(observed=None).kind is ActionKind.RECORD_CLOSED


def test_a_vanished_position_with_no_record_does_nothing() -> None:
    """Neither the broker nor we have it. There is nothing to close."""

    assert _decide(observed=None, recorded=None).kind is ActionKind.NOTHING


@pytest.mark.parametrize(
    ("is_buy", "current", "candidate", "expected"),
    [
        (True, "1.09700", "1.09800", ActionKind.TIGHTEN_STOP),   # rises: allowed
        (True, "1.09700", "1.09600", ActionKind.NOTHING),        # falls: refused
        (False, "1.10300", "1.10200", ActionKind.TIGHTEN_STOP),  # falls: allowed
        (False, "1.10300", "1.10400", ActionKind.NOTHING),       # rises: refused
    ],
)
def test_a_stop_only_ever_moves_toward_profit(
    is_buy: bool, current: str, candidate: str, expected: ActionKind
) -> None:
    """The first of the two enforcement layers. A candidate that would widen
    produces NOTHING -- not an error, because a retraced price legitimately
    produces one every cycle."""

    action = decide_tighten(
        is_buy=is_buy, current=Decimal(current), candidate=Decimal(candidate)
    )

    assert action.kind is expected
```

Add `decide_tighten(*, is_buy, current, candidate) -> GuardAction` as the narrow entry point the loop uses for a trailing candidate, so the monotonic rule has one home rather than being scattered.

- [ ] **Step 2: Run and watch them fail, then write `guard.py`**

Order the branches so the dangerous cases cannot be shadowed: a vanished position first, then an orphan, then a missing stop, then a disagreement, then nothing.

- [ ] **Step 3: Prove the ordering is load-bearing**

Move the "missing stop" branch above the "vanished position" branch, confirm `test_a_vanished_position_is_recorded_closed` fails, revert. Report what you saw. A branch order nobody has watched break is a branch order nobody has checked.

- [ ] **Step 4: Run, then commit**

```bash
UV_SYSTEM_CERTS=1 uv run pytest tests/unit/execution -q --no-cov
UV_SYSTEM_CERTS=1 uv run ruff format . && UV_SYSTEM_CERTS=1 uv run ruff check . && UV_SYSTEM_CERTS=1 uv run mypy
git add src/trading_house/execution/guard.py tests/unit/execution/test_guard.py
git commit -m "feat: the guard's pure decision function"
```

---

### Task 4: `amend_protection`, and the second enforcement layer

**Files:**
- Modify: `src/trading_house/brokers/mt5/adapter.py`
- Modify: `src/trading_house/brokers/mt5/boundary.py`, `terminal.py`
- Modify: `tests/acceptance/test_phase1.py`
- Test: `tests/unit/brokers/mt5/test_adapter.py`

**Interfaces:**
- Produces: `Mt5BrokerAdapter.amend_protection(ref, stop_loss, take_profit) -> ExecutionOutcome`, refusing a stop that would widen against the position's current broker-side SL.

**You will break a Phase 1 guard, and narrowing it is part of the job.** `REFUSING_METHODS` is currently `("amend_protection",)` and asserts it raises `NotImplementedError`. Implementing it breaks that. **Do not delete the test** — replace the refusal assertion with what now holds: that this phase implements protection and still never closes a position on its own initiative. Say in your report exactly what the narrowed test now pins.

- [ ] **Step 1: Write the failing tests**

```python
def test_a_widening_stop_is_refused_at_the_boundary() -> None:
    """The SECOND enforcement layer. decide() cannot emit a widening, but this
    must refuse one handed to it directly -- otherwise a future caller, or a
    bug, could widen a live stop with nothing objecting. The layers must be
    tested separately: an earlier phase claimed enforcement by grant AND
    trigger while only ever exercising the grant."""

    terminal = FakeTerminal(positions=[_position(ticket=7, sl=1.09700, is_buy=True)])

    outcome = _adapter(terminal).amend_protection(
        _ref(position_ticket=7), stop_loss=Decimal("1.09600"), take_profit=None
    )

    assert not outcome.accepted
    assert terminal.sent == []          # nothing was even attempted


def test_a_tightening_stop_is_sent() -> None:
    terminal = FakeTerminal(positions=[_position(ticket=7, sl=1.09700, is_buy=True)])

    outcome = _adapter(terminal).amend_protection(
        _ref(position_ticket=7), stop_loss=Decimal("1.09800"), take_profit=None
    )

    assert outcome.accepted
    assert terminal.sent[0]["position"] == 7


def test_the_request_carries_float_prices_and_the_position_ticket() -> None:
    """MT5 returns None with no useful error when sl or tp is an int, and
    TRADE_ACTION_SLTP without a `position` modifies nothing at all. Both are
    documented traps."""

    terminal = FakeTerminal(positions=[_position(ticket=7, sl=1.09700, is_buy=True)])

    _adapter(terminal).amend_protection(
        _ref(position_ticket=7), stop_loss=Decimal("1.09800"), take_profit=None
    )

    request = terminal.sent[0]
    assert isinstance(request["sl"], float)
    assert isinstance(request["tp"], float)
    assert request["position"] == 7
    assert request["action"] == TRADE_ACTION_SLTP


def test_a_none_result_raises_rather_than_reporting_a_rejection() -> None:
    """Same rule as submit: a None result may mean the modification landed, so
    reporting a rejection would record a stop as unchanged when it moved."""

    terminal = FakeTerminal(
        positions=[_position(ticket=7, sl=1.09700, is_buy=True)], send_result=None
    )

    with pytest.raises(BrokerError):
        _adapter(terminal).amend_protection(
            _ref(position_ticket=7), stop_loss=Decimal("1.09800"), take_profit=None
        )


def test_a_position_the_broker_does_not_have_is_refused() -> None:
    """Amending a ticket that no longer exists must not be reported as done."""

    outcome = _adapter(FakeTerminal(positions=[])).amend_protection(
        _ref(position_ticket=7), stop_loss=Decimal("1.09800"), take_profit=None
    )

    assert not outcome.accepted
```

- [ ] **Step 2: Implement it**

`amend_protection` reads the position by ticket, refuses if absent, **refuses a stop that does not improve on the position's current `sl`**, builds `{"action": TRADE_ACTION_SLTP, "symbol", "position": int(ticket), "sl": float(...), "tp": float(...) or 0.0}` and sends it. The request builder goes in `boundary.py`; `terminal.py` gets one call-and-delegate method.

- [ ] **Step 3: Prove each layer independently**

Delete the adapter's widening check, confirm `test_a_widening_stop_is_refused_at_the_boundary` fails while every `decide()` test still passes; revert. Then make `decide()` able to emit a widening, confirm its own test fails while the adapter test still passes; revert. **Report both.** This is D-3, and the only way to know the outer layer is not masking the inner.

- [ ] **Step 4: Narrow the Phase 1 guard, run, commit**

```bash
UV_SYSTEM_CERTS=1 uv run pytest tests/unit tests/acceptance -q --no-cov
UV_SYSTEM_CERTS=1 uv run ruff format . && UV_SYSTEM_CERTS=1 uv run ruff check . && UV_SYSTEM_CERTS=1 uv run mypy
git add src tests
git commit -m "feat: amend_protection, refusing any stop that would widen"
```

---

### Task 5: The daemon cycle

**Files:**
- Create: `src/trading_house/execution/loop.py`
- Test: `tests/unit/execution/test_loop.py`

**Interfaces:**
- Consumes: everything from Tasks 1-4.
- Produces:
  ```python
  class ProtectionPort(Protocol):
      def positions_now(self) -> Sequence[PositionRecord] | None: ...    # None = ERROR
      def amend_protection(self, ref, stop_loss, take_profit) -> ExecutionOutcome: ...

  class Escalator(Protocol):
      def escalate(self, reason: str, payload: Mapping[str, Any]) -> None: ...

  @dataclass(frozen=True, slots=True)
  class CycleReport:
      checked: int; acted: int; escalated: int

  class PositionGuard:
      def __init__(self, store: PositionStore, venue: ProtectionPort,
                   escalator: Escalator, clock: Clock) -> None
      def cycle(self) -> CycleReport
      def run(self, stop: threading.Event, interval_seconds: float = 1.0) -> None
  ```

`run()` loops until `stop` is set, then appends a shutdown event.

- [ ] **Step 1: Write the failing tests**

```python
def test_an_unreadable_position_list_escalates_and_acts_on_nothing() -> None:
    """None means the broker read FAILED. Treating it as "no positions" would
    conclude every position had closed and stop guarding all of them."""

    venue = FakeVenue(positions_now_result=None)
    report = _guard(venue).cycle()

    assert report.escalated == 1
    assert venue.amended == []


def test_a_matching_stop_writes_nothing() -> None:
    store = RecordingStore()
    store.append(7, "OPEN_PROTECTED", NOW, {"stop_loss": "1.09700"})

    _guard(FakeVenue(positions=[_observed(stop_loss=Decimal("1.09700"))]), store).cycle()

    assert len(store.appended) == 1      # only the seed


def test_a_missing_stop_is_restored_and_recorded() -> None:
    store = RecordingStore()
    store.append(7, "OPEN_PROTECTED", NOW, {"stop_loss": "1.09700"})
    venue = FakeVenue(positions=[_observed(stop_loss=None)])

    _guard(venue, store).cycle()

    assert venue.amended[0].stop_loss == Decimal("1.09700")
    assert store.appended[-1][1] == "OPEN_PROTECTED"


def test_a_restore_that_keeps_failing_escalates_by_the_third_cycle() -> None:
    """I-21: restored or escalated within two cycles."""

    venue = FakeVenue(positions=[_observed(stop_loss=None)], amend_fails=True)
    guard = _guard(venue)

    guard.cycle(); guard.cycle(); report = guard.cycle()

    assert report.escalated == 1


def test_shutdown_is_recorded() -> None:
    """A gap in the event stream must be explainable. An unrecorded stop looks
    identical to a guard that silently died."""

    store, stop = RecordingStore(), threading.Event()
    stop.set()

    _guard(FakeVenue(), store).run(stop, interval_seconds=0.0)

    assert store.appended[-1][1] == "GUARD_STOPPED"
```

Add `FakeVenue` and `RecordingStore` to `tests/unit/execution/conftest.py` beside the existing `FakeVenue`/`RecordingLedger` — **rename if the names collide; do not shadow the Phase 4 fakes.**

- [ ] **Step 2: Write the R-arithmetic tests**

§5.1 of the spec specifies this and nothing above tests it. Every R-multiple,
every MAE/MFE figure and the whole trade-quality record derives from it, so an
error here is silent and propagates into every downstream analysis.

```python
def test_r_multiple_is_signed_by_side_against_the_fixed_risk_distance() -> None:
    """Entry 1.10000, initial risk 0.00300. At 1.10300 a BUY is +1R and a SELL
    at the same price is -1R. Getting the sign wrong would report every losing
    short as a winner."""

    assert r_multiple(
        is_buy=True, open_price=Decimal("1.10000"), current=Decimal("1.10300"),
        initial_risk_distance=Decimal("0.00300"),
    ) == pytest.approx(1.0)
    assert r_multiple(
        is_buy=False, open_price=Decimal("1.10000"), current=Decimal("1.10300"),
        initial_risk_distance=Decimal("0.00300"),
    ) == pytest.approx(-1.0)


def test_mae_and_mfe_are_running_extrema_not_the_latest_value() -> None:
    """They are the worst and best EVER seen, not the current excursion. A
    position that went to +2R and came back to 0 must still report mfe_r 2.0 --
    that is the whole point of recording them."""

    mae, mfe = 0.0, 0.0
    for r in (0.5, 2.0, -0.75, 0.25):
        mae, mfe = min(mae, r), max(mfe, r)

    assert (mae, mfe) == (-0.75, 2.0)


def test_the_risk_distance_is_never_recomputed_as_the_stop_moves() -> None:
    """Section 8.2 fixes initial_risk_distance at entry. Recomputing it from the
    CURRENT stop would silently redefine R every time the guard tightened one,
    making every trade's R-multiples incomparable with every other's."""

    store = RecordingStore()
    store.append(7, "OPEN_PROTECTED", NOW, {
        "stop_loss": "1.09700", "open_price": "1.10000",
        "initial_risk_distance": "0.00300",
    })
    venue = FakeVenue(positions=[_observed(stop_loss=Decimal("1.09900"))])

    _guard(venue, store).cycle()

    assert store.appended[-1][2]["initial_risk_distance"] == "0.00300"
```

`r_multiple` is a small pure helper in `execution/guard.py`; export it so the
loop and the tests share one definition rather than each computing the sign
themselves.

- [ ] **Step 3: Write the loop, run to green, commit**

MAE/MFE update on each cycle from the current tick, as `FiniteFloat`, against the fixed `initial_risk_distance`. Escalation calls `mark_stale()`, appends an audit event, and stops attempting modifications.

```bash
UV_SYSTEM_CERTS=1 uv run pytest tests/unit/execution -q --no-cov
UV_SYSTEM_CERTS=1 uv run ruff format . && UV_SYSTEM_CERTS=1 uv run ruff check . && UV_SYSTEM_CERTS=1 uv run mypy
git add src/trading_house/execution/loop.py tests/unit/execution
git commit -m "feat: the position guard cycle"
```

---

### Task 6: CLI, the monotonic property, acceptance and docs

**Files:**
- Modify: `src/trading_house/cli.py`
- Create: `tests/property/test_guard.py`, `tests/acceptance/test_phase5.py`
- Modify: `tests/acceptance/test_architecture.py`, `README.md`, `mt5-multi-agent-trading-house-spec.md`

- [ ] **Step 1: The CLI**

`guard_app` with `run` and `status`, following `order_app`'s shape. **`guard run` must NOT call `require_clean_ledger`** — D-6: the guard never opens anything, and a guard that stopped protecting live positions because an unrelated intent was stuck would abandon money at the worst moment. Add a comment saying so, because "run the gate everywhere" is the plausible-looking mistake.

`run` installs a SIGINT handler that sets the stop event so shutdown is recorded.

- [ ] **Step 2: The monotonic property — the test that carries this phase**

```python
@given(
    is_buy=st.booleans(),
    start=st.decimals(min_value=Decimal("1.0"), max_value=Decimal("2.0"), places=5),
    candidates=st.lists(
        st.decimals(min_value=Decimal("1.0"), max_value=Decimal("2.0"), places=5),
        min_size=1, max_size=40,
    ),
)
def test_no_sequence_of_cycles_moves_a_stop_away_from_profit(
    is_buy: bool, start: Decimal, candidates: list[Decimal]
) -> None:
    """Section 15's test_stop_monotonic. The failure mode is a SEQUENCE of
    individually plausible steps, which is why this is generative rather than
    a table."""

    stop = start
    for candidate in candidates:
        action = decide_tighten(is_buy=is_buy, current=stop, candidate=candidate)
        if action.kind is ActionKind.TIGHTEN_STOP:
            assert action.stop_loss is not None
            stop = action.stop_loss

    assert (stop >= start) if is_buy else (stop <= start)
```

**Prove it can fail:** invert the comparison in `decide_tighten`, confirm the property fails, report the shrunk counterexample, revert. Report how many examples reach the assertion — this project has shipped a property reached in 0 of 2000.

- [ ] **Step 3: Acceptance tests**

`tests/acceptance/test_phase5.py`: `execution/` imports nothing forbidden; and an AST check that `amend_protection` is called from exactly one place in `execution/`, with no loop around it — the same structural shape that pins the no-resend guarantee.

Add the guard's arrow to `test_architecture.py` beside the existing ones, with its parametrized guard-the-guard.

- [ ] **Step 4: Document**

Add I-21 to `mt5-multi-agent-trading-house-spec.md` §0.2, worded exactly:

> **I-21** — Every open position is verified against its recorded protection at least once per cycle, and a missing stop is restored or escalated within two cycles.

Add a "Phase 5 — the position guard" section to `README.md` stating the one promise, that the guard is deliberately not gated by the unresolved-intent check, and that MAE/MFE are **sampled at cycle resolution, not true extrema**.

- [ ] **Step 5: Full gate, then commit**

```bash
UV_SYSTEM_CERTS=1 uv run ruff format --check .
UV_SYSTEM_CERTS=1 uv run ruff check .
UV_SYSTEM_CERTS=1 uv run mypy
UV_SYSTEM_CERTS=1 uv run pytest -q
git add src tests README.md mt5-multi-agent-trading-house-spec.md
git commit -m "feat: guard commands, and pin I-21 and the monotonic stop"
```
