# Phase 4 — Execution Submission and Reconciliation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn an approved `RiskDecision` into at most one broker position, ever, and refuse to send anything new while an earlier intent is unresolved.

**Architecture:** An append-only intent event table is written and committed *before* `order_send` is called. `submit()` records exactly one terminal-or-unknown outcome and never reconciles. A separate reconciler owns every non-terminal intent, and the same reconciler backs the gate that runs on every order-placing command.

**Tech Stack:** Python 3.12, Pydantic v2, `Decimal` for money, psycopg 3, PostgreSQL 18 + Alembic, pytest, testcontainers, mypy strict, Ruff.

**Spec:** `docs/superpowers/specs/2026-09-07-phase-4-execution-submission-design.md`

## Global Constraints

- **Every `uv` command must be prefixed `UV_SYSTEM_CERTS=1`** or it fails TLS verification in this environment.
- The mypy gate is `UV_SYSTEM_CERTS=1 uv run mypy` with **no path arguments** — the project scopes it via `packages = ["trading_house"]`.
- **`execution/` imports `core/` and `database/` and nothing else from this project** — not `brokers/`, not `risk/`, not `marketdata/`. It declares the venue port it needs; the MT5 adapter satisfies it structurally. An acceptance test pins it.
- **Only `brokers/mt5/terminal.py` may import MetaTrader5.** An acceptance test pins it, with no exemption for any other module.
- **`terminal.py` has an enforced cap of 80 statements** (`test_the_terminal_module_stays_thin`). It is at **65**. If your additions breach it, move the mapping into `boundary.py` — which holds the dataclasses and pure functions already and has no cap — leaving `terminal.py` as call-and-delegate. Do not raise the cap.
- Line length 100. mypy strict. **No new `# type: ignore` in `src/`.**
- **Do not add a `# noqa` for a rule this project does not enable.** The ruff select list is exactly `["E", "F", "I", "B", "UP", "SIM", "RUF", "S", "PT"]`. A directive outside it suppresses nothing and `RUF100` fails the build. This defect class has cost this project six rounds across earlier phases.
- Enums subclass `str, Enum` with `# noqa: UP042`, matching `core.schemas.Side`. This project has no `StrEnum`; do not introduce one.
- All money and price values are `Decimal`. MT5 returns floats; convert with `decimal_of(value)` (`Decimal(str(value))`), never `Decimal(float)`.
- **No credential, DSN, account number or raw broker message may appear in any error, log or audit payload.**
- Integration tests need Docker. The baseline suite is **943 passed / 3 skipped at 98.07% coverage**.

## What Already Exists

Verified present at `c0b394c`. Do not redefine any of it.

```python
# trading_house.core.values
class IntentState(str, Enum):
    SUBMITTING; CONFIRMED; UNKNOWN; RECONCILING; FAILED; REJECTED

# trading_house.core.schemas
class OrderIntent(CanonicalModel):
    intent_id: NonEmptyStr;  proposal_id: NonEmptyStr;  book: BookId
    instrument_id: InstrumentId;  side: Side;  quantity: PositiveQuantity
    stop_loss: Price;  take_profit: Price | None;  time_in_force: TimeInForce
    max_slippage_bps: BasisPoints;  state: IntentState;  t_submit_utc: datetime
    venue_ref: VenueRef | None = None;  outcome: ExecutionOutcome | None = None

# trading_house.core.venue
class Mt5VenueRef(CanonicalModel):
    venue: Literal[Venue.MT5];  magic: NonNegativeInt;  server_symbol: NonEmptyStr
    order_ticket: PositiveInt | None = None;  position_ticket: PositiveInt | None = None
    retcode: int | None = None
VenueRef = Mt5VenueRef

class ExecutionOutcome(CanonicalModel):
    accepted: bool;  venue_ref: VenueRef | None;  filled_quantity: PositiveQuantity | None
    fill_price: Price | None;  reject_reason: RejectReason | None
    # validators: accepted requires venue_ref and forbids reject_reason;
    # rejected requires reject_reason and forbids any fill;
    # filled_quantity and fill_price are present together or not at all.

class RejectReason(str, Enum):   # requote, price_changed, timeout, disconnected,
    # invalid_stops, invalid_quantity, market_closed, unsupported_fill,
    # insufficient_funds, trade_disabled, account_disabled, unknown
def recovery_for(reason: RejectReason) -> RecoveryAction
    # RETRY_WITH_FRESH_PRICE | REFRESH_CONTRACT_AND_RESIZE | ENTER_SAFE_MODE

# trading_house.brokers.mt5.retcodes
RETCODE_REJECT_REASON: dict[int, RejectReason]   # 10004,10012,10014,10016,10017,
                                                 # 10018,10019,10020,10024,10026,
                                                 # 10027,10030,10031
SUCCESS_RETCODES = frozenset({10008, 10009, 10010})   # 10010 is a PARTIAL fill

# trading_house.brokers.mt5.magic
def derive_magic(intent_id: str, magic_range: tuple[int, int]) -> int

# trading_house.brokers.mt5.boundary  (dataclasses, frozen+slots)
class Mt5Position: ticket, magic, server_symbol, volume, price_open, sl, tp,
                   is_buy, opened_at
class Mt5CheckResult: retcode, comment
class TerminalPort(Protocol):   # NO order_send and NO deal history today
    initialize, shutdown, account_trade_mode, terminal_connected,
    server_utc_offset_seconds, symbol_info, symbol_tick, copy_rates_range,
    positions, order_check, last_error

# trading_house.brokers.mt5.contracts
def decimal_of(value: float) -> Decimal          # Decimal(str(value))

# trading_house.brokers.base
class ReconciliationReport(CanonicalModel):
    book: BookId;  positions: tuple[PositionState, ...]
    unmatched_venue_refs: tuple[VenueRef, ...];  reconciled_at: datetime

# trading_house.database.connection
def open_runtime_connection(dsn: SecretStr) -> psycopg.Connection[tuple[Any, ...]]

# trading_house.marketdata.store  — the repository pattern to copy
class ConnectionFactory(Protocol):
    def __call__(self) -> psycopg.Connection[tuple[Any, ...]]: ...
```

**`adapter.submit()`, `adapter.close()` and `adapter.amend_protection()` currently raise `NotImplementedError`.** `adapter.reconcile()` returns every venue position in `unmatched_venue_refs` with `positions` always empty, and its docstring says the intent ledger is what turns an unmatched ref into a matched `PositionState`.

**The four books and their signed magic ranges** (`config/venue_binding.mt5.yaml`): `fx_scalp` 110000-119999, `fx_swing` 120000-129999, `equity_swing` 130000-139999, `sleeve` 140000-149999.

## File Structure

| File | Responsibility |
|---|---|
| `brokers/mt5/boundary.py` | gains `Mt5SendResult`, `Mt5Deal`, two `TerminalPort` methods |
| `brokers/mt5/terminal.py` | gains `order_send`, `history_deals` — watch the 80-statement cap |
| `migrations/versions/0004_intent_events.py` | the `execution` schema, append-only by grant and trigger |
| `execution/ledger.py` | append an event, read current state, list non-terminal intents |
| `execution/manager.py` | `VenueSubmitPort`, `OrderManager.submit()` |
| `execution/reconciler.py` | `verdict()` pure function, `reconcile_all()` sweep, `require_clean_ledger()` gate |
| `brokers/mt5/adapter.py` | `submit()`, `close()`, ledger-aware `reconcile()` |
| `config/risk_constitution.yaml` | `max_spread_fraction_of_stop`, re-signed |
| `risk/engine.py` | the spread-fraction gate (closes R-8) |
| `cli.py` | `order submit`, `order reconcile`, `order status` |

---

### Task 1: Widen the MT5 boundary for writes

**Files:**
- Modify: `src/trading_house/brokers/mt5/boundary.py`
- Modify: `src/trading_house/brokers/mt5/terminal.py`
- Test: `tests/unit/brokers/mt5/test_boundary.py`, `tests/acceptance/test_architecture.py` (verify only, no change expected)

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces:
  ```python
  @dataclass(frozen=True, slots=True)
  class Mt5SendResult:
      retcode: int
      order_ticket: int | None
      position_ticket: int | None
      deal_ticket: int | None
      volume: float
      price: float
      comment: str

  @dataclass(frozen=True, slots=True)
  class Mt5Deal:
      ticket: int
      order_ticket: int
      position_ticket: int
      magic: int
      server_symbol: str
      volume: float
      price: float
      is_buy: bool
      dealt_at: datetime

  # added to TerminalPort
  def order_send(self, request: Mapping[str, object]) -> Mt5SendResult | None: ...
  def history_deals(self, start: datetime, end: datetime) -> Sequence[Mt5Deal]: ...
  ```

- [ ] **Step 1: Write the failing tests**

In `tests/unit/brokers/mt5/test_boundary.py`, beside the existing dataclass tests:

```python
def test_send_result_carries_every_ticket_mt5_can_return() -> None:
    """A submission can yield an order ticket, a position ticket and a deal
    ticket, and reconciliation needs all three: the deal proves execution, the
    position is what the guard will later modify, and the order is what a
    pending request is cancelled by."""

    result = Mt5SendResult(
        retcode=10009,
        order_ticket=1,
        position_ticket=2,
        deal_ticket=3,
        volume=0.25,
        price=1.10000,
        comment="Done",
    )

    assert (result.order_ticket, result.position_ticket, result.deal_ticket) == (1, 2, 3)


def test_send_result_tickets_are_optional_because_a_rejection_has_none() -> None:
    """A rejected send returns a retcode and nothing else. Making the tickets
    required would force the terminal layer to invent zeros, and zero is a
    meaningful magic value elsewhere in this codebase."""

    result = Mt5SendResult(
        retcode=10019,
        order_ticket=None,
        position_ticket=None,
        deal_ticket=None,
        volume=0.0,
        price=0.0,
        comment="No money",
    )

    assert result.order_ticket is None


def test_deal_carries_the_fields_reconciliation_matches_on() -> None:
    """Matching is by magic AND symbol AND volume AND time, because magic is a
    locator that collides, not an identity."""

    dealt = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)
    deal = Mt5Deal(
        ticket=9,
        order_ticket=8,
        position_ticket=7,
        magic=110042,
        server_symbol="EURUSD",
        volume=0.25,
        price=1.10000,
        is_buy=True,
        dealt_at=dealt,
    )

    assert (deal.magic, deal.server_symbol, deal.volume, deal.dealt_at) == (
        110042,
        "EURUSD",
        0.25,
        dealt,
    )
```

- [ ] **Step 2: Run it and watch it fail**

```bash
UV_SYSTEM_CERTS=1 uv run pytest tests/unit/brokers/mt5/test_boundary.py -q --no-cov
```

Expected: FAIL — `Mt5SendResult` is not defined.

- [ ] **Step 3: Add the dataclasses and widen the protocol**

In `boundary.py`, beside `Mt5CheckResult`, following that file's `@dataclass(frozen=True, slots=True)` style, add `Mt5SendResult` and `Mt5Deal` exactly as given in the Interfaces block above. Then add to `TerminalPort`:

```python
    def order_send(self, request: Mapping[str, object]) -> Mt5SendResult | None: ...
    def history_deals(self, start: datetime, end: datetime) -> Sequence[Mt5Deal]: ...
```

- [ ] **Step 4: Implement them in terminal.py**

```python
    def order_send(self, request: Mapping[str, object]) -> Mt5SendResult | None:
        result = mt5.order_send(dict(request))
        if result is None:
            return None
        return Mt5SendResult(
            retcode=int(result.retcode),
            order_ticket=int(result.order) or None,
            position_ticket=int(getattr(result, "position", 0)) or None,
            deal_ticket=int(result.deal) or None,
            volume=float(result.volume),
            price=float(result.price),
            comment=str(result.comment),
        )

    def history_deals(self, start: datetime, end: datetime) -> Sequence[Mt5Deal]:
        raw = mt5.history_deals_get(start, end)
        if raw is None:
            return ()
        return tuple(
            Mt5Deal(
                ticket=int(d.ticket),
                order_ticket=int(d.order),
                position_ticket=int(d.position_id),
                magic=int(d.magic),
                server_symbol=d.symbol,
                volume=float(d.volume),
                price=float(d.price),
                is_buy=int(d.type) == 0,
                dealt_at=self._to_utc(d.time),
            )
            for d in raw
        )
```

Two MT5 realities encoded here. `result.order`, `result.deal` and `result.position` are `0` — not `None` — when absent, which is why each is coerced with `or None`; leaving the zero through would put a falsely-positive ticket into a `VenueRef` whose fields are `PositiveInt | None`. And `position` is absent entirely on some result objects, hence `getattr`.

- [ ] **Step 5: Check the statement cap**

```bash
UV_SYSTEM_CERTS=1 uv run pytest tests/acceptance/test_architecture.py -q --no-cov
```

`test_the_terminal_module_stays_thin` caps `terminal.py` at 80 statements; it was at 65 before your change. If it now fails, do **not** raise the cap — move the dataclass construction into module-level helpers in `boundary.py` (e.g. `send_result_from(raw)`, `deal_from(raw, to_utc)`) and have `terminal.py` call them. Report which you did.

- [ ] **Step 6: Run the targeted tests and commit**

```bash
UV_SYSTEM_CERTS=1 uv run pytest tests/unit/brokers tests/acceptance -q --no-cov
UV_SYSTEM_CERTS=1 uv run ruff format . && UV_SYSTEM_CERTS=1 uv run ruff check . && UV_SYSTEM_CERTS=1 uv run mypy
git add src/trading_house/brokers/mt5 tests/unit/brokers/mt5
git commit -m "feat: widen the MT5 boundary with order_send and deal history"
```

---

### Task 2: The append-only intent ledger

**Files:**
- Create: `migrations/versions/0004_intent_events.py`
- Create: `src/trading_house/execution/__init__.py` (empty)
- Create: `src/trading_house/execution/ledger.py`
- Test: `tests/integration/execution/test_ledger.py`

**Interfaces:**
- Consumes: `ConnectionFactory` (copy the Protocol from `marketdata/store.py`), `OrderIntent`, `IntentState`.
- Produces:
  ```python
  @dataclass(frozen=True, slots=True)
  class IntentEvent:
      seq: int
      intent_id: str
      state: IntentState
      event_time: datetime
      payload: Mapping[str, Any]

  class IntentLedger(Protocol):
      def append(self, intent_id: str, state: IntentState, event_time: datetime,
                 payload: Mapping[str, Any]) -> None: ...
      def current_state(self, intent_id: str) -> IntentState | None: ...
      def events_for(self, intent_id: str) -> tuple[IntentEvent, ...]: ...
      def non_terminal(self) -> tuple[str, ...]: ...

  class PostgresIntentLedger:  # satisfies IntentLedger
      def __init__(self, connection_factory: ConnectionFactory) -> None: ...

  NON_TERMINAL_STATES = frozenset({IntentState.SUBMITTING, IntentState.UNKNOWN,
                                   IntentState.RECONCILING})
  ```

- [ ] **Step 1: Write the migration**

`migrations/versions/0004_intent_events.py`, following `0003_market_bars.py`'s shape exactly (`revision`, `down_revision = "0003_market_bars"`, `SET ROLE trading_house_owner`, schema creation, `REVOKE ALL ... FROM PUBLIC`):

```python
"""Create the append-only intent ledger (I-6, I-20)."""

from collections.abc import Sequence

from alembic import op

revision: str = "0004_intent_events"
down_revision: str | None = "0003_market_bars"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET ROLE trading_house_owner")
    op.execute("CREATE SCHEMA execution AUTHORIZATION trading_house_owner")
    op.execute("REVOKE ALL ON SCHEMA execution FROM PUBLIC")

    op.execute(
        """
        CREATE TABLE execution.intent_events (
            seq BIGSERIAL PRIMARY KEY,
            intent_id TEXT NOT NULL,
            state TEXT NOT NULL,
            event_time TIMESTAMPTZ NOT NULL,
            recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            payload JSONB NOT NULL,
            CONSTRAINT intent_state_known CHECK (state IN (
                'SUBMITTING', 'CONFIRMED', 'UNKNOWN',
                'RECONCILING', 'FAILED', 'REJECTED'
            ))
        )
        """
    )
    op.execute(
        "CREATE INDEX intent_events_latest ON execution.intent_events "
        "(intent_id, seq DESC)"
    )

    op.execute(
        """
        CREATE FUNCTION execution.reject_intent_event_mutation()
        RETURNS TRIGGER LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'execution.intent_events is append-only';
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER intent_events_no_mutation "
        "BEFORE UPDATE OR DELETE ON execution.intent_events "
        "FOR EACH ROW EXECUTE FUNCTION execution.reject_intent_event_mutation()"
    )
    op.execute(
        "CREATE TRIGGER intent_events_no_truncate "
        "BEFORE TRUNCATE ON execution.intent_events "
        "FOR EACH STATEMENT EXECUTE FUNCTION execution.reject_intent_event_mutation()"
    )

    op.execute("REVOKE ALL ON TABLE execution.intent_events FROM PUBLIC")
    op.execute("GRANT USAGE ON SCHEMA execution TO trading_house_runtime")
    op.execute(
        "GRANT SELECT, INSERT ON TABLE execution.intent_events TO trading_house_runtime"
    )
    op.execute(
        "GRANT USAGE, SELECT ON SEQUENCE execution.intent_events_seq_seq "
        "TO trading_house_runtime"
    )


def downgrade() -> None:
    op.execute("SET ROLE trading_house_owner")
    op.execute("DROP SCHEMA execution CASCADE")
```

Note the sequence grant: `BIGSERIAL` creates `intent_events_seq_seq`, and without `USAGE` on it the runtime role can `INSERT` in theory and fails in practice. Confirm the generated sequence name with `\ds execution.*` if the insert test fails.

- [ ] **Step 2: Write the failing integration tests**

`tests/integration/execution/test_ledger.py`, using the `database` fixture already in `tests/conftest.py` and following `tests/integration/marketdata/`'s style:

```python
@pytest.mark.integration
def test_current_state_is_the_latest_event(ledger: PostgresIntentLedger) -> None:
    ledger.append("i-1", IntentState.SUBMITTING, NOW, {"step": 1})
    ledger.append("i-1", IntentState.UNKNOWN, NOW, {"step": 2})
    ledger.append("i-1", IntentState.CONFIRMED, NOW, {"step": 3})

    assert ledger.current_state("i-1") is IntentState.CONFIRMED


@pytest.mark.integration
def test_every_transition_is_kept(ledger: PostgresIntentLedger) -> None:
    """The point of an append-only ledger is that an incident review can see
    how long an intent sat in UNKNOWN, not merely that it did."""

    ledger.append("i-1", IntentState.SUBMITTING, NOW, {})
    ledger.append("i-1", IntentState.UNKNOWN, NOW, {})
    ledger.append("i-1", IntentState.CONFIRMED, NOW, {})

    assert [e.state for e in ledger.events_for("i-1")] == [
        IntentState.SUBMITTING,
        IntentState.UNKNOWN,
        IntentState.CONFIRMED,
    ]


@pytest.mark.integration
def test_non_terminal_finds_exactly_the_intents_the_gate_must_resolve(
    ledger: PostgresIntentLedger,
) -> None:
    ledger.append("open-1", IntentState.SUBMITTING, NOW, {})
    ledger.append("open-2", IntentState.UNKNOWN, NOW, {})
    ledger.append("open-3", IntentState.RECONCILING, NOW, {})
    ledger.append("done-1", IntentState.SUBMITTING, NOW, {})
    ledger.append("done-1", IntentState.CONFIRMED, NOW, {})
    ledger.append("done-2", IntentState.REJECTED, NOW, {})
    ledger.append("done-3", IntentState.FAILED, NOW, {})

    assert set(ledger.non_terminal()) == {"open-1", "open-2", "open-3"}


@pytest.mark.integration
def test_an_intent_that_reached_a_terminal_state_is_not_reopened(
    ledger: PostgresIntentLedger,
) -> None:
    """A CONFIRMED intent followed by nothing must stay out of the gate's list.
    If `non_terminal` looked at any event rather than the latest, every intent
    that ever passed through SUBMITTING would block all trading forever."""

    ledger.append("i-1", IntentState.SUBMITTING, NOW, {})
    ledger.append("i-1", IntentState.CONFIRMED, NOW, {})

    assert ledger.non_terminal() == ()


@pytest.mark.integration
def test_the_database_refuses_an_update_from_the_runtime_role(
    database: DatabaseHarness,
) -> None:
    """Append-only by grant and trigger, not by application discipline.
    Discipline is not evidence; this is."""

    ledger = PostgresIntentLedger(lambda: open_runtime_connection(database.runtime_dsn))
    ledger.append("i-1", IntentState.SUBMITTING, NOW, {})

    with open_runtime_connection(database.runtime_dsn) as connection, pytest.raises(
        psycopg.errors.Error
    ):
        connection.execute(
            "UPDATE execution.intent_events SET state = 'CONFIRMED' WHERE intent_id = 'i-1'"
        )


@pytest.mark.integration
def test_the_database_refuses_a_delete_from_the_runtime_role(
    database: DatabaseHarness,
) -> None:
    ledger = PostgresIntentLedger(lambda: open_runtime_connection(database.runtime_dsn))
    ledger.append("i-1", IntentState.SUBMITTING, NOW, {})

    with open_runtime_connection(database.runtime_dsn) as connection, pytest.raises(
        psycopg.errors.Error
    ):
        connection.execute("DELETE FROM execution.intent_events")
```

Read `tests/integration/marketdata/conftest.py` for how that suite builds its store fixture and what the `database` harness exposes, and follow it — do not invent a second harness.

- [ ] **Step 3: Run and watch them fail**

```bash
UV_SYSTEM_CERTS=1 uv run pytest tests/integration/execution -q --no-cov -m integration
```

Expected: FAIL — `trading_house.execution` does not exist.

- [ ] **Step 4: Write the ledger**

`execution/ledger.py`, copying `marketdata/store.py`'s structure: module-level SQL constants, a `ConnectionFactory` Protocol, a private failure exception, and a class taking the factory. The three queries:

```python
_APPEND_SQL = """
INSERT INTO execution.intent_events (intent_id, state, event_time, payload)
VALUES (%s, %s, %s, %s)
"""

_CURRENT_STATE_SQL = """
SELECT state FROM execution.intent_events
WHERE intent_id = %s
ORDER BY seq DESC
LIMIT 1
"""

_EVENTS_SQL = """
SELECT seq, intent_id, state, event_time, payload
FROM execution.intent_events
WHERE intent_id = %s
ORDER BY seq
"""

_NON_TERMINAL_SQL = """
SELECT intent_id FROM (
    SELECT DISTINCT ON (intent_id) intent_id, state
    FROM execution.intent_events
    ORDER BY intent_id, seq DESC
) latest
WHERE state = ANY(%s)
"""
```

`append` must **commit before returning** — the caller's next action is `order_send`, and an uncommitted write is not a durability point. Use `psycopg.types.json.Jsonb` for the payload parameter.

- [ ] **Step 5: Run to green, then commit**

```bash
UV_SYSTEM_CERTS=1 uv run pytest tests/integration/execution -q --no-cov -m integration
UV_SYSTEM_CERTS=1 uv run ruff format . && UV_SYSTEM_CERTS=1 uv run ruff check . && UV_SYSTEM_CERTS=1 uv run mypy
git add migrations/versions/0004_intent_events.py src/trading_house/execution tests/integration/execution
git commit -m "feat: append-only intent ledger, enforced by grant and trigger"
```

---

### Task 3: The order manager and the write-before-send protocol

**Files:**
- Create: `src/trading_house/execution/manager.py`
- Test: `tests/unit/execution/test_manager.py`, `tests/unit/execution/conftest.py`

**Interfaces:**
- Consumes: `IntentLedger`, `IntentState`, `OrderIntent`, `ExecutionOutcome`, `RejectReason`, `recovery_for`.
- Produces:
  ```python
  class VenueSubmitPort(Protocol):
      def submit(self, intent: OrderIntent) -> ExecutionOutcome: ...

  class OrderManager:
      def __init__(self, ledger: IntentLedger, venue: VenueSubmitPort, clock: Clock) -> None
      def submit(self, intent: OrderIntent) -> IntentState
          # returns exactly one of CONFIRMED, REJECTED, UNKNOWN
  ```

- [ ] **Step 1: Write the fake venue and the failing tests**

`tests/unit/execution/conftest.py`:

```python
"""A venue that can do what a real broker cannot on demand.

The property this phase exists to guarantee -- that a lost response never
doubles a position -- cannot be staged against a live broker, because you
cannot make MT5 return None *after* it has executed. That is why this fake is
the primary instrument and not a convenience.
"""


class FakeVenue:
    def __init__(self, *, behaviour: str = "accept") -> None:
        self.behaviour = behaviour
        self.calls: list[OrderIntent] = []
        self.executed: list[OrderIntent] = []

    def submit(self, intent: OrderIntent) -> ExecutionOutcome:
        self.calls.append(intent)
        if self.behaviour == "lost_after_execution":
            # The broker DID execute; the answer never came back.
            self.executed.append(intent)
            raise TimeoutError("no response")
        if self.behaviour == "lost_before_execution":
            raise TimeoutError("no response")
        if self.behaviour == "reject":
            return ExecutionOutcome(
                accepted=False, venue_ref=None, filled_quantity=None,
                fill_price=None, reject_reason=RejectReason.INSUFFICIENT_FUNDS,
            )
        self.executed.append(intent)
        return ExecutionOutcome(
            accepted=True,
            venue_ref=Mt5VenueRef(
                venue=Venue.MT5, magic=110042, server_symbol="EURUSD",
                order_ticket=1, position_ticket=2, retcode=10009,
            ),
            filled_quantity=intent.quantity,
            fill_price=Decimal("1.10000"),
            reject_reason=None,
        )
```

`tests/unit/execution/test_manager.py`:

```python
def test_the_ledger_records_submitting_before_the_venue_is_called() -> None:
    """The durability point. If the write happened after the send, a crash in
    between would leave a broker position with no ledger entry at all, and
    nothing would ever look for it."""

    order = []
    ledger = RecordingLedger(on_append=lambda state: order.append(f"ledger:{state.value}"))
    venue = FakeVenue()
    venue_submit = venue.submit

    def watched(intent):
        order.append("venue:submit")
        return venue_submit(intent)

    venue.submit = watched  # type: ignore[method-assign]
    OrderManager(ledger, venue, FixedClock(NOW)).submit(_intent())

    assert order[0] == "ledger:SUBMITTING"
    assert order[1] == "venue:submit"


def test_an_accepted_submission_records_confirmed_once() -> None:
    ledger, venue = RecordingLedger(), FakeVenue()

    state = OrderManager(ledger, venue, FixedClock(NOW)).submit(_intent())

    assert state is IntentState.CONFIRMED
    assert [s for _, s in ledger.appended] == [IntentState.SUBMITTING, IntentState.CONFIRMED]


def test_a_rejection_records_its_reason_and_recovery() -> None:
    ledger, venue = RecordingLedger(), FakeVenue(behaviour="reject")

    state = OrderManager(ledger, venue, FixedClock(NOW)).submit(_intent())

    assert state is IntentState.REJECTED
    payload = ledger.appended[-1][2]
    assert payload["reject_reason"] == RejectReason.INSUFFICIENT_FUNDS.value
    assert payload["recovery"] == RecoveryAction.ENTER_SAFE_MODE.value


def test_a_lost_response_records_unknown_and_does_not_resend() -> None:
    """The flagship. The broker executed; the answer was lost. Resending here
    is what doubles a position, and it is the one thing that must never
    happen."""

    ledger, venue = RecordingLedger(), FakeVenue(behaviour="lost_after_execution")

    state = OrderManager(ledger, venue, FixedClock(NOW)).submit(_intent())

    assert state is IntentState.UNKNOWN
    assert len(venue.calls) == 1
    assert len(venue.executed) == 1


def test_the_manager_never_reconciles() -> None:
    """submit() records UNKNOWN and stops. Reconciliation belongs to the
    reconciler, so that the path recovering a lost response is the same code
    that recovers after a crash -- and is therefore exercised routinely."""

    ledger, venue = RecordingLedger(), FakeVenue(behaviour="lost_after_execution")

    OrderManager(ledger, venue, FixedClock(NOW)).submit(_intent())

    assert [s for _, s in ledger.appended] == [IntentState.SUBMITTING, IntentState.UNKNOWN]


def test_a_partial_fill_is_confirmed_at_the_filled_quantity() -> None:
    """Retcode 10010 is in SUCCESS_RETCODES. Recording the requested quantity
    would put a position size in the ledger that the broker never gave us."""

    ledger = RecordingLedger()
    venue = FakeVenue(behaviour="partial")   # fills half

    state = OrderManager(ledger, venue, FixedClock(NOW)).submit(
        _intent(quantity=Decimal("0.50"))
    )

    assert state is IntentState.CONFIRMED
    assert ledger.appended[-1][2]["filled_quantity"] == "0.25"
    assert ledger.appended[-1][2]["requested_quantity"] == "0.50"
```

Add a `"partial"` behaviour to `FakeVenue` returning `filled_quantity` at half the requested amount, and a `RecordingLedger` in the same conftest that satisfies `IntentLedger` in memory and exposes `appended: list[tuple[str, IntentState, Mapping[str, Any]]]`.

- [ ] **Step 2: Run and watch them fail**

```bash
UV_SYSTEM_CERTS=1 uv run pytest tests/unit/execution -q --no-cov
```

- [ ] **Step 3: Write the manager**

```python
class OrderManager:
    """Writes, sends, records. Never reconciles, never resends."""

    def __init__(self, ledger: IntentLedger, venue: VenueSubmitPort, clock: Clock) -> None:
        self._ledger = ledger
        self._venue = venue
        self._clock = clock

    def submit(self, intent: OrderIntent) -> IntentState:
        now = self._clock.now()
        self._ledger.append(intent.intent_id, IntentState.SUBMITTING, now, _snapshot(intent))
        try:
            outcome = self._venue.submit(intent)
        except Exception:
            # Deliberately broad: ANY failure to obtain a usable answer is
            # UNKNOWN. Narrowing this to the exceptions we predicted would let
            # an unforeseen one escape, and an escaping exception leaves the
            # ledger saying SUBMITTING with nobody recording why.
            self._ledger.append(
                intent.intent_id, IntentState.UNKNOWN, self._clock.now(), {}
            )
            return IntentState.UNKNOWN
        state = IntentState.CONFIRMED if outcome.accepted else IntentState.REJECTED
        self._ledger.append(
            intent.intent_id, state, self._clock.now(), _outcome_payload(intent, outcome)
        )
        return state
```

`_outcome_payload` records, as JSON-safe values: `accepted`, the venue ref's fields, `fill_price` and `filled_quantity` as **strings** (a `Decimal` must not become a float in JSONB), `requested_quantity`, and for a rejection both `reject_reason` and `recovery_for(reason).value`.

`_snapshot(intent)` records the intent's own fields the same way — every
`Decimal` as a string — **plus `strategy_id`**, which `OrderIntent` does not
carry and which Task 5's `reconcile()` needs to build a `PositionState`. Take
it as an argument to `submit()` alongside the intent rather than reaching for
the proposal: `execution/` has no access to `TradeProposal`'s origin.

The bare `except Exception` needs no `# noqa` — `BLE001` is not in this project's ruff select list. Do not add one; `RUF100` will fail the build.

- [ ] **Step 4: Run to green, then commit**

```bash
UV_SYSTEM_CERTS=1 uv run pytest tests/unit/execution -q --no-cov
UV_SYSTEM_CERTS=1 uv run ruff format . && UV_SYSTEM_CERTS=1 uv run ruff check . && UV_SYSTEM_CERTS=1 uv run mypy
git add src/trading_house/execution/manager.py tests/unit/execution
git commit -m "feat: write-before-send order manager that never resends"
```

---

### Task 4: The reconciler and the gate

**Files:**
- Create: `src/trading_house/execution/reconciler.py`
- Test: `tests/unit/execution/test_reconciler.py`

**Interfaces:**
- Consumes: `IntentLedger`, `NON_TERMINAL_STATES`, `Mt5Deal`-shaped rows (see below), `IntentState`.
- Produces:
  ```python
  MATCH_LOOKBACK = timedelta(seconds=60)
  RESOLUTION_TIMEOUT = timedelta(seconds=30)

  class Verdict(str, Enum):  # noqa: UP042
      CONFIRMED = "CONFIRMED"
      FAILED = "FAILED"
      STILL_UNKNOWN = "STILL_UNKNOWN"

  @dataclass(frozen=True, slots=True)
  class DealRecord:
      magic: int
      server_symbol: str
      volume: Decimal
      position_ticket: int
      dealt_at: datetime

  def verdict(*, magic: int, server_symbol: str, volume: Decimal,
              t_submit: datetime, deals: Sequence[DealRecord],
              now: datetime, terminal_healthy: bool) -> Verdict

  class UnresolvedIntentsError(TradingHouseError)   # ExitCode.UNRESOLVED_INTENTS = 12
  def require_clean_ledger(ledger: IntentLedger) -> None

  class DealSource(Protocol):
      def deals_since(self, start: datetime) -> Sequence[DealRecord]: ...
      def terminal_healthy(self) -> bool: ...

  def reconcile_all(ledger: IntentLedger, deals: DealSource,
                    clock: Clock) -> Mapping[str, Verdict]
  ```

`DealSource` is `execution/`'s own port, satisfied structurally by the MT5
adapter — `execution/` must not import `brokers/`.

`DealRecord` is `execution/`'s own neutral shape, deliberately **not** `Mt5Deal` — `execution/` must not import `brokers/`. The adapter maps one to the other.

- [ ] **Step 1: Write the failing verdict tests**

```python
BASE = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)


def _deal(**overrides: object) -> DealRecord:
    fields: dict[str, object] = {
        "magic": 110042,
        "server_symbol": "EURUSD",
        "volume": Decimal("0.25"),
        "position_ticket": 7,
        "dealt_at": BASE,
    }
    fields.update(overrides)
    return DealRecord(**fields)  # type: ignore[arg-type]


def _verdict(**overrides: object) -> Verdict:
    args: dict[str, object] = {
        "magic": 110042,
        "server_symbol": "EURUSD",
        "volume": Decimal("0.25"),
        "t_submit": BASE,
        "deals": (),
        "now": BASE + timedelta(seconds=5),
        "terminal_healthy": True,
    }
    args.update(overrides)
    return verdict(**args)  # type: ignore[arg-type]


def test_exactly_one_matching_deal_confirms() -> None:
    assert _verdict(deals=(_deal(),)) is Verdict.CONFIRMED


def test_two_matching_deals_never_guess() -> None:
    """This is what a magic collision actually produces. Adopting one attaches
    our ledger to a position that may not be ours, which is worse than staying
    unresolved."""

    assert _verdict(deals=(_deal(), _deal(position_ticket=8))) is Verdict.STILL_UNKNOWN


def test_a_deal_with_a_different_magic_does_not_match() -> None:
    assert _verdict(deals=(_deal(magic=119999),)) is Verdict.STILL_UNKNOWN


def test_a_deal_on_another_symbol_does_not_match() -> None:
    """Magic ranges are per book, not per symbol, so two intents in the same
    book can collide on magic while trading different instruments."""

    assert _verdict(deals=(_deal(server_symbol="GBPUSD"),)) is Verdict.STILL_UNKNOWN


def test_a_deal_of_a_different_volume_does_not_match() -> None:
    assert _verdict(deals=(_deal(volume=Decimal("0.50")),)) is Verdict.STILL_UNKNOWN


def test_a_deal_stamped_before_the_lookback_does_not_match() -> None:
    """MATCH_LOOKBACK is 60s of broker clock skew, not an open-ended history."""

    assert _verdict(deals=(_deal(dealt_at=BASE - timedelta(seconds=61)),)) is (
        Verdict.STILL_UNKNOWN
    )


def test_a_deal_stamped_slightly_before_submission_still_matches() -> None:
    """The broker's clock can run ahead of ours; a deal stamped 30 seconds
    before we think we sent it is still plausibly ours."""

    assert _verdict(deals=(_deal(dealt_at=BASE - timedelta(seconds=30)),)) is (
        Verdict.CONFIRMED
    )


def test_no_match_inside_the_timeout_stays_unknown() -> None:
    """Before RESOLUTION_TIMEOUT elapses, absence of a deal is not evidence.
    Declaring FAILED here would abandon an order still working its way
    through."""

    assert _verdict(now=BASE + timedelta(seconds=29)) is Verdict.STILL_UNKNOWN


def test_no_match_after_the_timeout_fails() -> None:
    assert _verdict(now=BASE + timedelta(seconds=31)) is Verdict.FAILED


def test_an_unhealthy_terminal_never_yields_failed() -> None:
    """"I cannot see the broker" is not "the order did not happen". A phase
    that conflated them would mark live positions FAILED and forget them."""

    assert _verdict(
        now=BASE + timedelta(seconds=600), terminal_healthy=False
    ) is Verdict.STILL_UNKNOWN
```

- [ ] **Step 2: Run and watch them fail, then write `verdict`**

```python
def verdict(
    *,
    magic: int,
    server_symbol: str,
    volume: Decimal,
    t_submit: datetime,
    deals: Sequence[DealRecord],
    now: datetime,
    terminal_healthy: bool,
) -> Verdict:
    """Pure. No broker, no clock, no ledger -- so every hard case is a table."""

    window_start = t_submit - MATCH_LOOKBACK
    matches = [
        deal
        for deal in deals
        if deal.magic == magic
        and deal.server_symbol == server_symbol
        and deal.volume == volume
        and window_start <= deal.dealt_at <= now
    ]
    if len(matches) == 1:
        return Verdict.CONFIRMED
    if len(matches) > 1:
        return Verdict.STILL_UNKNOWN
    if not terminal_healthy:
        return Verdict.STILL_UNKNOWN
    if now - t_submit >= RESOLUTION_TIMEOUT:
        return Verdict.FAILED
    return Verdict.STILL_UNKNOWN
```

Order matters: the ambiguity check precedes the health check, because two matches is a definite ambiguity regardless of terminal health.

- [ ] **Step 3: Write the gate tests, then the gate**

```python
def test_the_gate_passes_on_an_empty_ledger() -> None:
    require_clean_ledger(RecordingLedger())   # must not raise


def test_the_gate_refuses_while_an_intent_is_unresolved() -> None:
    """I-20. This is the check standing between a half-known position and a
    second order on top of it."""

    ledger = RecordingLedger()
    ledger.append("i-1", IntentState.UNKNOWN, BASE, {})

    with pytest.raises(UnresolvedIntentsError):
        require_clean_ledger(ledger)


def test_the_gate_names_the_intents_it_is_blocking_on() -> None:
    """An operator who cannot see which intent is stuck cannot clear it."""

    ledger = RecordingLedger()
    ledger.append("i-1", IntentState.UNKNOWN, BASE, {})
    ledger.append("i-2", IntentState.SUBMITTING, BASE, {})

    with pytest.raises(UnresolvedIntentsError) as caught:
        require_clean_ledger(ledger)

    assert "i-1" in str(caught.value) and "i-2" in str(caught.value)
```

Add `UNRESOLVED_INTENTS = 12` to `ExitCode` in `core/errors.py` and `UnresolvedIntentsError` to the error family — `tests/unit/test_cli.py` asserts every concrete `TradingHouseError` has an exit-code mapping, so omitting it fails that test.

- [ ] **Step 4: Write the sweep**

`verdict()` decides; `reconcile_all()` is what applies that decision to every
non-terminal intent and writes the result back. It is the single component the
gate and the `order reconcile` command both drive.

```python
def test_the_sweep_resolves_an_unknown_intent_to_confirmed() -> None:
    ledger = RecordingLedger()
    ledger.append("i-1", IntentState.SUBMITTING, BASE, _snapshot_payload())
    ledger.append("i-1", IntentState.UNKNOWN, BASE, {})

    results = reconcile_all(ledger, FakeDeals(deals=(_deal(),)), FixedClock(BASE_PLUS_5))

    assert results["i-1"] is Verdict.CONFIRMED
    assert ledger.current_state("i-1") is IntentState.CONFIRMED


def test_the_sweep_marks_reconciling_before_it_polls() -> None:
    """A sweep that dies mid-poll must be visible as such rather than looking
    untouched. RECONCILING is in the frozen IntentState enum for this."""

    ledger = RecordingLedger()
    ledger.append("i-1", IntentState.UNKNOWN, BASE, _snapshot_payload())

    reconcile_all(ledger, FakeDeals(deals=()), FixedClock(BASE_PLUS_5))

    states = [state for _, state in ledger.appended]
    assert IntentState.RECONCILING in states
    assert states.index(IntentState.RECONCILING) < len(states) - 1


def test_the_sweep_leaves_an_unresolved_intent_non_terminal() -> None:
    """Inside RESOLUTION_TIMEOUT with no matching deal, the honest answer is
    still \"I do not know\". Writing FAILED here would abandon an order that
    may yet appear."""

    ledger = RecordingLedger()
    ledger.append("i-1", IntentState.UNKNOWN, BASE, _snapshot_payload())

    results = reconcile_all(ledger, FakeDeals(deals=()), FixedClock(BASE_PLUS_5))

    assert results["i-1"] is Verdict.STILL_UNKNOWN
    assert ledger.current_state("i-1") in NON_TERMINAL_STATES


def test_the_sweep_writes_failed_once_the_timeout_has_passed() -> None:
    ledger = RecordingLedger()
    ledger.append("i-1", IntentState.UNKNOWN, BASE, _snapshot_payload())

    reconcile_all(ledger, FakeDeals(deals=()), FixedClock(BASE + timedelta(seconds=31)))

    assert ledger.current_state("i-1") is IntentState.FAILED


def test_the_sweep_ignores_intents_that_already_reached_a_terminal_state() -> None:
    """Re-reconciling a CONFIRMED intent could append a second, contradictory
    verdict on top of a settled one."""

    ledger = RecordingLedger()
    ledger.append("i-1", IntentState.SUBMITTING, BASE, _snapshot_payload())
    ledger.append("i-1", IntentState.CONFIRMED, BASE, {})

    assert reconcile_all(ledger, FakeDeals(deals=()), FixedClock(BASE_PLUS_5)) == {}
```

The sweep reads `ledger.non_terminal()`, and for each intent: appends
RECONCILING, reads the magic, symbol, volume and `t_submit` back out of that
intent's SUBMITTING payload, calls `deals.deals_since(t_submit - MATCH_LOOKBACK)`
and `deals.terminal_healthy()`, applies `verdict(...)`, and appends CONFIRMED or
FAILED — appending nothing further when the verdict is STILL_UNKNOWN, since the
RECONCILING event already records that an attempt was made.

Add `FakeDeals` to Task 3's `tests/unit/execution/conftest.py` beside
`FakeVenue`, taking a `deals` tuple and a `healthy` flag.

- [ ] **Step 5: Write the restart test — the second of the two tests that carry this phase**

This one spans the manager and the gate, which is exactly why it belongs here:
it proves the crash-recovery path and the lost-response path are the same code.

```python
def test_a_process_that_died_between_the_write_and_the_send_blocks_the_next_one() -> None:
    """The scenario I-20 exists for. A manager writes SUBMITTING, commits, and
    the process dies before any answer is recorded. A FRESH manager -- new
    object, same ledger, exactly as a restart would give you -- must refuse to
    send anything at all until that leftover intent is resolved.

    Without the gate, the second run opens a position on top of one that may
    already exist, and the ledger records both as if they were independent.
    """

    ledger = RecordingLedger()
    dying_venue = FakeVenue(behaviour="lost_after_execution")
    OrderManager(ledger, dying_venue, FixedClock(BASE)).submit(_intent(intent_id="i-1"))

    fresh_venue = FakeVenue()
    with pytest.raises(UnresolvedIntentsError):
        require_clean_ledger(ledger)

    assert fresh_venue.calls == [], "no order may be sent while i-1 is unresolved"


def test_the_gate_reopens_once_the_leftover_resolves() -> None:
    """Guard the guard. A gate that never let anything through would pass the
    test above, and would also stop the system trading forever."""

    ledger = RecordingLedger()
    OrderManager(ledger, FakeVenue(behaviour="lost_after_execution"), FixedClock(BASE)).submit(
        _intent(intent_id="i-1")
    )
    ledger.append("i-1", IntentState.CONFIRMED, BASE, {})

    require_clean_ledger(ledger)   # must not raise

    assert OrderManager(ledger, FakeVenue(), FixedClock(BASE)).submit(
        _intent(intent_id="i-2")
    ) is IntentState.CONFIRMED
```

`RecordingLedger`, `FakeVenue` and `_intent` all come from Task 3's
`tests/unit/execution/conftest.py` — import them, do not define second copies.
Two fixtures that drift apart is the defect that made Phase 2's flagship test
hollow.

- [ ] **Step 6: Run to green, then commit**

```bash
UV_SYSTEM_CERTS=1 uv run pytest tests/unit/execution -q --no-cov
UV_SYSTEM_CERTS=1 uv run ruff format . && UV_SYSTEM_CERTS=1 uv run ruff check . && UV_SYSTEM_CERTS=1 uv run mypy
git add src/trading_house/execution/reconciler.py src/trading_house/core/errors.py tests/unit/execution
git commit -m "feat: reconciliation verdict, sweep and the unresolved-intent gate"
```

---

### Task 5: The adapter's write path and closing R-8

**Files:**
- Modify: `src/trading_house/brokers/mt5/adapter.py`
- Modify: `src/trading_house/constitution/models.py`, `config/risk_constitution.yaml`
- Modify: `src/trading_house/risk/engine.py`
- Test: `tests/unit/brokers/mt5/test_adapter.py`, `tests/unit/risk/test_engine.py`, `tests/unit/constitution/test_models.py`

**Interfaces:**
- Consumes: `Mt5SendResult`, `Mt5Deal` (Task 1); `DealRecord` (Task 4).
- Produces: `Mt5Adapter.submit()`, `Mt5Adapter.close()`, `Mt5Adapter.deals_since(start)` returning `tuple[DealRecord, ...]`, a ledger-aware `Mt5Adapter.reconcile(book)` returning matched `PositionState`; `BookLimits.max_spread_fraction_of_stop`.

- [ ] **Step 1: Write the failing adapter tests**

Follow the existing `FakeTerminal` in `tests/unit/brokers/mt5/conftest.py` — extend it with `order_send` and `history_deals` rather than writing a second fake.

```python
def test_submit_builds_a_request_with_float_prices() -> None:
    """MT5 returns None with no useful error when sl or tp is an int. This is
    a documented, commonly-hit trap and the reason every price crossing the
    boundary is coerced to float."""

    terminal = FakeTerminal(send_result=Mt5SendResult(
        retcode=10009, order_ticket=1, position_ticket=2, deal_ticket=3,
        volume=0.25, price=1.10000, comment="Done",
    ))

    Mt5Adapter(...).submit(_intent())

    request = terminal.sent[0]
    assert isinstance(request["sl"], float)
    assert isinstance(request["volume"], float)


def test_submit_stamps_the_derived_magic() -> None:
    """Reconciliation finds the deal by magic. An unstamped order is
    unrecoverable after a lost response."""

    ...
    assert terminal.sent[0]["magic"] == derive_magic(intent.intent_id, (110000, 119999))


def test_a_success_retcode_yields_an_accepted_outcome() -> None: ...
def test_a_rejection_retcode_maps_through_the_taxonomy() -> None: ...


def test_a_none_result_raises_rather_than_returning_a_rejection() -> None:
    """A None result is NOT a rejection -- the order may have executed. If the
    adapter returned an ExecutionOutcome(accepted=False) here, the manager
    would record REJECTED and the position would be orphaned forever."""

    terminal = FakeTerminal(send_result=None)

    with pytest.raises(BrokerError):
        Mt5Adapter(...).submit(_intent())


@pytest.mark.parametrize("retcode", sorted(RETCODE_REJECT_REASON))
def test_every_mapped_retcode_produces_a_rejection_reason(retcode: int) -> None:
    """Exhaustive over the taxonomy, so a code added to the map without
    handling here fails immediately."""
    ...
```

That last test is the guard that the map and the adapter cannot drift apart.

- [ ] **Step 2: Implement `submit`, `close` and `deals_since`**

Replace the `NotImplementedError` bodies. `submit` builds the MT5 request dict — `action: TRADE_ACTION_DEAL`, `symbol`, `volume: float`, `type`, `sl: float`, `tp: float` or `0.0`, `magic`, `deviation` from `max_slippage_bps`, `type_filling` from the contract's supported fills — calls `terminal.order_send`, and:

- `None` result → raise `BrokerError`. **Never** an `ExecutionOutcome`.
- `retcode in SUCCESS_RETCODES` → accepted outcome, `filled_quantity` from `result.volume` via `decimal_of`.
- otherwise → rejected outcome with `RETCODE_REJECT_REASON.get(retcode, RejectReason.UNKNOWN)`.

`deals_since(start)` calls `terminal.history_deals(start, now)` and maps each `Mt5Deal` to a `DealRecord`, converting `volume` with `decimal_of`.

- [ ] **Step 3: Close R-8 — the spread-fraction gate**

Add to `BookLimits` in `constitution/models.py`, and to its `convert_integer_decimals` validator tuple:

```python
    # The absolute economic ceiling on cost, and the reason it exists: the
    # ratio gate compares the current spread to the MEDIAN, and a zero median
    # disables it entirely. This one never references the median.
    max_spread_fraction_of_stop: Percentage
```

Add `max_spread_fraction_of_stop: 25.0` to each of the four books in `config/risk_constitution.yaml`. The constitution must then be **re-signed** — that step is the controller's, not the implementer's.

In `risk/engine.py`, add `SPREAD_EXCEEDS_STOP_FRACTION = "spread_exceeds_stop_fraction"` to `RejectionReason` and gate on it **after** the stop distance is known:

```python
        spread_price = tick_spread_points * contract.point_size
        if spread_price * Decimal(100) > distance * book.max_spread_fraction_of_stop:
            return self._reject(proposal, [RejectionReason.SPREAD_EXCEEDS_STOP_FRACTION], passed)
        passed.append(RejectionReason.SPREAD_EXCEEDS_STOP_FRACTION)
```

Note it uses the **current tick** spread, not the median — the median is the cost term's input, this is the gate's.

- [ ] **Step 4: Write the tests proving R-8 is closed**

```python
def test_a_zero_median_no_longer_admits_an_enormous_spread(constitution) -> None:
    """R-8, carried from Phase 3 on condition it closed before any order could
    be placed. Before this gate, median 0 with a 100000-point tick spread was
    APPROVED at full size, because the ratio gate compares against the median
    and a multiple of zero admits everything."""

    decision = _engine(constitution).evaluate(
        _proposal(), contract=_contract(),
        **_facts(median_spread_points=Decimal("0"),
                 tick_spread_points=Decimal("100000")),
    )

    assert RejectionReason.SPREAD_EXCEEDS_STOP_FRACTION in decision.reasons


def test_an_ordinary_spread_passes_the_fraction_gate(constitution) -> None:
    """Guard the guard: a gate that rejected everything would also pass the
    test above."""

    decision = _engine(constitution).evaluate(_proposal(), contract=_contract(), **_facts())

    assert decision.verdict == "APPROVED"
```

- [ ] **Step 5: Make `reconcile()` match against the ledger**

Today `reconcile()` returns every venue position in `unmatched_venue_refs` with
`positions` always empty, and its docstring says the intent ledger is what turns
an unmatched ref into a matched `PositionState`. That ledger now exists.

```python
def test_a_position_whose_magic_matches_a_confirmed_intent_is_reported_matched() -> None:
    ...
    report = adapter.reconcile("fx_scalp")

    assert len(report.positions) == 1
    assert report.unmatched_venue_refs == ()


def test_a_position_with_no_matching_intent_stays_unmatched() -> None:
    """A manually opened position, or one from another system sharing the
    account. Adopting it would put a position we never sized under our own
    risk accounting."""

    ...
    assert report.positions == ()
    assert len(report.unmatched_venue_refs) == 1


def test_a_matched_position_carries_the_stop_the_intent_recorded() -> None:
    """initial_risk_distance is what every R-multiple derives from, and spec
    8.2 says it is fixed at entry and never changes. Reading it from the live
    position's current sl would silently redefine R the moment a stop moved."""

    ...
    assert report.positions[0].initial_risk_distance == Decimal("0.00300")
```

Match a venue position to an intent by magic, then build `PositionState` from
the ledger's SUBMITTING payload (`book`, `side`, `quantity`, `stop_loss`,
`strategy_id`) plus the venue position (`open_price`, tickets, `opened_at`):
`lifecycle="OPEN_PROTECTED"`, `initial_risk_distance = abs(open_price -
stop_loss)` **taken from the intent's recorded stop, not the position's current
one**, and `r_multiple_open`, `mae_r`, `mfe_r` all `0.0` — excursion tracking is
the next phase and inventing values here would be fabricating data, which this
project refuses everywhere else.

`OrderIntent` carries `proposal_id` but not `strategy_id`, so the SUBMITTING
payload written in Task 3 must include `strategy_id` from the originating
proposal. If it does not, add it there rather than inventing one here.

- [ ] **Step 6: Run, then commit**

```bash
UV_SYSTEM_CERTS=1 uv run pytest tests/unit -q --no-cov
UV_SYSTEM_CERTS=1 uv run ruff format . && UV_SYSTEM_CERTS=1 uv run ruff check . && UV_SYSTEM_CERTS=1 uv run mypy
git add src tests config
git commit -m "feat: adapter write path, ledger-aware reconcile and an absolute spread ceiling"
```

---

### Task 6: CLI, acceptance tests, the live test and docs

**Files:**
- Modify: `src/trading_house/cli.py`
- Create: `tests/acceptance/test_phase4.py`
- Modify: `tests/acceptance/test_architecture.py`
- Create: `tests/live/test_mt5_submit.py`
- Modify: `README.md`, `mt5-multi-agent-trading-house-spec.md`

**Interfaces:**
- Consumes: everything from Tasks 1-5.
- Produces: nothing further.

- [ ] **Step 1: Add the CLI commands**

An `order_app = typer.Typer(...)` registered as `order`, following the `data_app` pattern at `cli.py:98` exactly, with three commands: `submit`, `reconcile`, `status`. **Every one of them calls `require_clean_ledger` first except `reconcile`**, which is the command that clears the condition.

- [ ] **Step 2: Write the phase acceptance tests**

`tests/acceptance/test_phase4.py`:

```python
def test_no_execution_module_imports_a_broker() -> None:
    """execution/ declares the venue port it needs; the adapter satisfies it
    structurally. An import here would make the order manager untestable
    without MetaTrader5 installed."""

    for path in sorted(EXECUTION.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        for forbidden in ("trading_house.brokers", "trading_house.risk",
                          "trading_house.marketdata"):
            assert forbidden not in source, f"{path.name} imports {forbidden}"


def test_the_manager_cannot_resend() -> None:
    """Grep the manager for a retry loop. I-6 is that a lost response never
    doubles a position, and the simplest way to break it is a `for attempt in
    range(...)` around the send."""

    source = (EXECUTION / "manager.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    sends = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and node.func.attr == "submit"
    ]
    assert len(sends) == 1, "the venue must be called from exactly one place"
    for node in ast.walk(tree):
        if isinstance(node, ast.For | ast.While):
            assert not any(
                isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute)
                and inner.func.attr == "submit"
                for inner in ast.walk(node)
            ), "the venue is called inside a loop"
```

Add the `execution/` arrow to `test_architecture.py` beside the existing `features/` and `risk/` guards, with its parametrized guard-the-guard, following that file's `_reaches` helper.

- [ ] **Step 3: Write the opt-in live test**

`tests/live/test_mt5_submit.py`, using the shared `skip_reason()` from `tests/live/conftest.py` (which already checks platform, import, terminal, demo status and a live clock):

```python
@pytest.fixture
def close_everything_after(adapter):
    """Teardown runs even if the test body raises. A test that opens a real
    position and dies must not leave it open."""

    yield
    for position in adapter.open_positions():
        adapter.close(position.venue_ref, None)


@pytest.mark.mt5
def test_a_minimum_lot_position_opens_confirms_and_closes(adapter, close_everything_after):
    ...
```

It must assert the demo guard passed before sending anything.

- [ ] **Step 4: Document**

Add a "Phase 4 — execution" section to `README.md` in the shape of the Phase 3 section, stating the durability point, that `submit()` never reconciles, and that the gate runs on every order-placing command. Add I-20 to `mt5-multi-agent-trading-house-spec.md` §0.2, worded exactly as §7 of the design spec:

> **I-20** — No order is sent while any earlier intent is unresolved.

- [ ] **Step 5: Full gate, then commit**

```bash
UV_SYSTEM_CERTS=1 uv run ruff format --check .
UV_SYSTEM_CERTS=1 uv run ruff check .
UV_SYSTEM_CERTS=1 uv run mypy
UV_SYSTEM_CERTS=1 uv run pytest -q
git add src tests README.md mt5-multi-agent-trading-house-spec.md
git commit -m "test: pin I-20 and the execution import boundary"
```
