# Phase 1 MT5 Gateway Design

**Status:** Approved design
**Date:** 2026-08-25
**Source specification:** `mt5-multi-agent-trading-house-spec.md` (§2.4, §3, §17.2)
**Builds on:** `docs/superpowers/specs/2026-08-22-trading-house-architecture-revision-design.md`
**Delivery scope:** Phase 1 only — a read-only MT5 gateway. No orders are sent.

## Purpose

Phase 0.5 defined `BrokerAdapter` as the complete surface a venue must present, deliberately without an implementation. This phase writes the first one: a MetaTrader 5 gateway that connects to a demo terminal, describes instruments in neutral terms, snapshots quotes, reconciles positions, and reports health.

It sends no orders. Order submission requires the idempotency spine — the intent ledger, deterministic magic, and `UNKNOWN`-state recovery — which is Phase 3. Building `submit()` before that machinery exists means sending orders with no crash-recovery story, and a lost `order_send` response is the highest-consequence failure in the system.

This is also where `MetaTrader5` enters the codebase for the first time. The architecture tests currently forbid that import anywhere in `src/`, so relaxing that ban — narrowly, and provably — is part of the deliverable.

## Decisions Record

| # | Decision | Chosen | Consequence |
|---|---|---|---|
| D-1 | Account | A confirmed demo account, plus a hard `trade_mode == DEMO` guard inside `start()` | The gateway has no state in which it is connected to a live account and merely "not trading yet" |
| D-2 | Adapter scope | Read-only: `describe_instrument`, `snapshot`, `precheck`, `reconcile`, `health` | `submit` / `amend_protection` / `close` raise a typed error until Phase 3. Demo testing carries zero order risk |
| D-3 | CI strategy | Pure-logic split, platform-conditional dependency, marker-gated live tests | The untested-on-CI surface shrinks to `terminal.py`'s IPC calls |
| D-4 | Gateway shape | Full actor now — thread plus priority queue — with only P1–P3 traffic | Phase 3 adds P0 traffic without restructuring; latency and queue-depth data start accruing immediately |
| D-5 | Unclassifiable retcodes | Add `RejectReason.UNKNOWN`, classified `AUTHORITY` | An unrecognised broker response enters safe mode rather than being guessed as retryable |

D-5 is an additive change to a contract Phase 0.5 froze. It was raised explicitly and approved.

## Scope

### Included

- `src/trading_house/brokers/mt5/` — actor, read-only adapter, three pure translators, a thin terminal boundary.
- `MetaTrader5` as a platform-conditional dependency.
- A narrowed import ban: one path-scoped exemption, with the two enforcing test files split by responsibility.
- `BrokerUnavailableError` and `NonDemoAccountError`, with exit codes 8 and 9.
- `RejectReason.UNKNOWN`.
- The health gate's fifth step: reconcile every book against the venue.
- `scripts/broker_audit.py` and its committed report.
- Gateway lifecycle events appended through the Phase 0 audit interface.

### Excluded

- `submit`, `amend_protection`, `close` — Phase 3, with the intent ledger.
- Market-data ingest and storage — its own spec (Phase 1.5).
- Features, strategies, risk evaluation, sizing, position guard.
- Multi-account or multi-terminal operation.
- Any write to the broker. This phase is read-only by construction.

### Topology

**Topology A (all-Windows)**, per source spec §2.4: the MT5 terminal, Python, and the stack run on one Windows host. Recorded here as the project's topology decision. The source spec warns against Wine as a production execution substrate, and Topology B's thin-gateway split is not needed while there is one account on one machine.

---

## 1. Module Layout and the Import Boundary

**Principle:** exactly one module in the codebase may `import MetaTrader5`. Everything else takes plain data.

```text
src/trading_house/brokers/
├── base.py            (exists — the eight-method BrokerAdapter)
└── mt5/
    ├── __init__.py    import-safe on Linux; no eager terminal import
    ├── terminal.py    the ONLY module importing MetaTrader5
    ├── gateway.py     the actor: thread, priority queue, heartbeat
    ├── adapter.py     Mt5BrokerAdapter implements BrokerAdapter
    ├── contracts.py   PURE: symbol info -> InstrumentContract
    ├── retcodes.py    PURE: retcode -> RejectReason
    └── magic.py       PURE: intent_id + book range -> magic
```

`contracts.py`, `retcodes.py` and `magic.py` accept dataclasses and integers, never MT5 objects. That is what makes them testable on Linux, and it is what shrinks the CI-untestable surface to the IPC layer alone.

### 1.1 The dependency

```toml
"MetaTrader5>=5.0.45,<6 ; sys_platform == 'win32'"
```

Platform-conditional, so `uv sync` on the Linux CI job omits it and `uv lock --check` still passes.

### 1.2 Relaxing the ban

`MetaTrader5` is currently banned tree-wide, and the forbidden set is duplicated in `tests/acceptance/test_architecture.py` and `tests/acceptance/test_phase0.py`. Rather than loosen both, the two files are given different and more precise responsibilities:

- **`test_architecture.py`** owns the tree-wide rule and gains one path-scoped exemption: `MetaTrader5` is importable only from `brokers/mt5/terminal.py`. It also gains the companion "prove the guard fires" test this file already uses for every other guard — a synthetic import from any other path must be caught.
- **`test_phase0.py`** stops asserting a tree-wide ban it no longer owns. It instead asserts that the Phase 0 packages — `core`, `audit`, `constitution`, `database`, `ops`, `settings`, `cli` — never import it. That is the guarantee Phase 0 actually made, and it remains true as later phases add venues.

This is stricter than the status quo where it matters. Today, deleting one entry from a shared frozenset would permit `core/schemas.py` to import MT5. After this change, the exemption names one file.

### 1.3 Import safety on Linux

`brokers/mt5/__init__.py` must not import `terminal.py` at module level, or `import trading_house.brokers.mt5` raises `ModuleNotFoundError` on Linux. It exposes the adapter through a lazy `__getattr__`, and a test imports the package on every platform to prove it.

---

## 2. The Gateway Actor

One thread owns every `mt5.*` call. Callers submit a request with a priority and wait on a result.

```python
class Priority(IntEnum):
    PROTECTION = 0    # amend/close — Phase 3
    ORDER = 1         # precheck now; submit in Phase 3
    RECONCILE = 2
    MARKET_DATA = 3
```

Queue items are `(priority, sequence, request)` so ties break FIFO within a level. Phase 1 generates only P1–P3 traffic; P0 exists and is exercised synthetically.

### 2.1 The demo guard runs before anything else

On `start()`: initialise the terminal, read `account_info()`, and if `trade_mode` is not `ACCOUNT_TRADE_MODE_DEMO`, shut the connection down and raise `NonDemoAccountError`. No request is served before that check passes.

The consequence is deliberate: there is no gateway state in which the process is connected to a live account and merely not trading yet. A mis-pointed terminal fails closed.

Two new typed errors follow the existing discipline in `core/errors.py` — no-argument constructors, fixed public messages:

| Error | Exit code | Public message |
|---|---|---|
| `BrokerUnavailableError` | `ExitCode.BROKER = 8` | `broker terminal unavailable` |
| `NonDemoAccountError` | `ExitCode.ACCOUNT_MODE = 9` | `refusing to operate a non-demo account` |

`tests/unit/test_cli.py::test_every_typed_error_has_a_stable_exit_code` walks `TradingHouseError.__subclasses__()` dynamically, so adding an error without its exit code fails that test automatically. The guard already exists.

### 2.2 Server time is not UTC

Source spec §3 is explicit: MT5 returns broker-server epoch seconds. At `start()` the gateway computes `server_utc_offset` by comparing `symbol_info_tick().time` against `SystemClock`, stores it, and re-verifies periodically.

Every timestamp crossing out of `terminal.py` is converted to timezone-aware UTC at that boundary. Nothing downstream ever sees a raw broker timestamp, so invariant I-10 holds by construction rather than by discipline.

### 2.3 Instrumentation

Per-priority queue depth, actor loop period, MT5 call latency, and time since last successful poll per instrument are collected into a `GatewayMetrics` model inside `brokers/mt5/`.

They do **not** widen `VenueHealth`, which stays the three-field neutral summary the adapter contract defines. Queue depth is gateway-operational data, not venue-neutral fact.

P0 queue depth exceeding its threshold is a safe-mode signal. It is collected now and wired in Phase 3, when P0 traffic exists.

### 2.4 Reconnection never silently resumes

Any connectivity gap marks gateway state stale, and it remains stale until a `reconcile()` succeeds. In a read-only phase this gates only reads, but encoding the rule now means Phase 3 inherits it rather than having to remember it.

### 2.5 A stated limitation

`mt5.*` calls are blocking C calls. If one hangs, Python cannot kill it, and the actor thread is stuck. What the design can do is time out the caller's wait, stop the heartbeat advancing, and report unhealthy so an operator or supervisor acts. What it cannot do is recover in-process.

This is recorded rather than papered over. Genuine isolation requires the separate-process topology, which was considered and declined for a read-only phase.

---

## 3. The Pure Translation Layer

### 3.1 Symbol info to `InstrumentContract`

The neutral identity comes from the signed venue binding, not from the broker's symbol string: `server_symbol` is reverse-mapped to `instrument_id`. The binding is authoritative, so a broker renaming a symbol becomes a config change rather than a code change.

| `InstrumentContract` field | MT5 source | Note |
|---|---|---|
| `price_increment` | `trade_tick_size` | not `point` — the source spec warns about this confusion |
| `value_per_price_increment` | `trade_tick_value_loss` | source spec §3: "use THIS for sizing" |
| `quantity_increment` / `min` / `max` | `volume_step` / `volume_min` / `volume_max` | |
| `min_stop_distance` | `trade_stops_level × point` | points converted to price units |
| `freeze_distance` | `trade_freeze_level × point` | points converted to price units |
| `base_currency` / `quote_currency` | `currency_base` / `currency_profit` | |
| `shortable` | `trade_mode ∈ {FULL, SHORTONLY}` | |
| `financing` | swap for FX and metals; dividend adjustment for equity CFDs | |
| `supported_fills` | `filling_mode` bitmask **and** `trade_exemode` | see 3.1.3 |

#### 3.1.1 `min_stop_distance` cannot express "no minimum"

Some brokers report `trade_stops_level = 0`, meaning no enforced distance. `InstrumentContract.min_stop_distance` is `PositiveDecimal`, so zero is unrepresentable.

Rather than widen the contract, the value is floored:

```text
min_stop_distance = max(trade_stops_level × point, price_increment)
```

A stop must be at least one tick from the market regardless of what the broker enforces, so this is a truthful floor rather than a fudge, and it preserves the positivity guarantee the risk engine will depend on.

#### 3.1.2 Float to Decimal

MT5 returns floats; the canonical contracts are `Decimal`. `Decimal(0.1)` is `0.1000000000000000055511151231257827…`.

Every conversion goes through `Decimal(str(value))` and quantizes to the symbol's `digits`. This gets a dedicated test with pathological values, because a silent float artifact in `value_per_price_increment` propagates directly into position sizing.

#### 3.1.3 `supported_fills` needs two fields

The `filling_mode` bitmask carries FOK and IOC. `RETURN` availability depends on `trade_exemode` being MARKET or EXCHANGE. Deriving from the bitmask alone silently drops a valid policy.

`supported_fills` has `min_length=1`, so a symbol yielding an empty set is rejected as untradeable rather than turned into an invalid contract. The same applies to `trade_mode == DISABLED`.

### 3.2 Retcode to `RejectReason`

An explicit table maps the documented retcodes:

| Retcode | `RejectReason` |
|---|---|
| 10004 REQUOTE | `REQUOTE` |
| 10014 INVALID_VOLUME | `INVALID_QUANTITY` |
| 10016 INVALID_STOPS | `INVALID_STOPS` |
| 10018 MARKET_CLOSED | `MARKET_CLOSED` |
| 10019 NO_MONEY | `INSUFFICIENT_FUNDS` |
| 10020 PRICE_CHANGED | `PRICE_CHANGED` |
| 10024 TOO_MANY_REQUESTS | `TIMEOUT` |
| 10026 SERVER_DISABLES_AT | `TRADE_DISABLED` |
| 10027 CLIENT_DISABLES_AT | `TRADE_DISABLED` |
| 10030 INVALID_FILL | `UNSUPPORTED_FILL` |
| 10031 NO_CONNECTION | `DISCONNECTED` |
| anything unrecognised | `UNKNOWN` |

The mapping cannot be total over all possible retcodes — brokers add them. An unrecognised retcode must not raise `KeyError`, and must not be guessed as transient, which could produce a retry loop.

`RejectReason.UNKNOWN` is therefore added and classified `RejectClass.AUTHORITY`, so an unclassifiable broker response enters safe mode and reaches a human. The raw retcode still lands in `Mt5VenueRef.retcode` for forensics, which is what that field exists for.

`REJECT_CLASS`'s existing totality test forces the new member to be classified, so this cannot be added halfway.

**Retcodes with no clean neutral equivalent map to `UNKNOWN` pending evidence.** `10015 INVALID_PRICE` is the clearest example: it means the price *we sent* was malformed, which is contractual rather than transient — but `RejectReason` has no `INVALID_PRICE` member, and mapping it to `PRICE_CHANGED` would classify it transient and produce a retry loop on a genuinely bad price. Rather than invent a member or guess a class, such retcodes map to `UNKNOWN` until the broker audit and live precheck testing show which ones actually occur and what they mean at this broker. Adding a member later is additive and cheap; mis-classifying one now is a live retry loop.

Note that in Phase 1 retcodes can only arrive from `order_check()`, since nothing is submitted. A precheck rejection is not a house-wide event, so the implementation must ensure `UNKNOWN`'s `AUTHORITY` classification drives safe mode only where a real order was at stake — in Phase 3. Phase 1 surfaces the reason and the raw retcode, and stops there.

### 3.3 Deterministic magic derivation

```text
magic = range_start + (int(sha256(intent_id).hexdigest()[:8], 16) % range_size)
```

with `range_start` and `range_size` taken from the signed venue binding for that book.

Determinism is the point: after a crash, an intent's magic can be recomputed with no persisted mapping to lose. Sequential allocation would require persisting that mapping, which is one more thing that can go missing at exactly the wrong moment.

**Stated limitation.** Magic is not a unique key. A 10,000-wide range collides by birthday after roughly 120 concurrent intents. Bounded by `max_concurrent_positions` — now 3 per book — collision probability is negligible for open intents, but two historical intents in the same book can share a magic.

Magic is therefore a **book-and-intent locator, not an identity**. Phase 3's recovery scopes queries by magic and time window, with the intent ledger authoritative. This is recorded now so Phase 3 does not discover it.

---

## 4. Health Gate, Testing, and the Broker Audit

### 4.1 The health gate's fifth step

Phase 0.5 §4.3 specified: constitution, database, migration, audit chain, **reconcile every book against the venue**, then ready. `HealthService` currently stops at four.

It gains one injected dependency, matching its existing style:

```python
class BookReconciler(Protocol):
    def __call__(self) -> Mapping[BookId, ReconciliationReport]: ...
```

`HealthReport` gains `books_reconciled` and `open_positions`.

**Phase 1 reconciliation reports; it does not fail readiness.** Phase 0.5 §3.5 makes a reconciliation mismatch a house-wide safe-mode trigger, which is correct once the system owns positions. In Phase 1 it has created none and there is no intent ledger to compare against, so "mismatch" is not yet computable. Any position found is a manual demo trade, and failing readiness on those would make `health` permanently red for no useful reason.

Mismatch detection arrives with the intent ledger in Phase 3. Phase 1 delivers the reconcile call and the report that detection will consume.

### 4.2 Three test layers

| Layer | Runs on | Needs a terminal |
|---|---|---|
| Pure functions — `contracts`, `retcodes`, `magic` | Linux and Windows, in coverage | no |
| Actor behaviour against a fake terminal | Linux and Windows, in coverage | no |
| Live terminal | this machine only, marked `mt5` | yes |

Layer 2 carries most of the risk — priority ordering, the demo guard firing, heartbeat stall, caller timeout, reconnect-marks-stale — and needs no terminal, because `terminal.py` is a protocol boundary. This is the same technique that made `BrokerAdapter` fault-injectable.

`pyproject.toml` gains `markers = ["mt5: requires a running MetaTrader 5 terminal"]`. Live tests skip when no terminal is present, so CI on both platforms runs layers 1 and 2 unconditionally.

### 4.3 The coverage problem, and its guard

`terminal.py` cannot run on CI. The suite currently measures 97.42% over 1549 statements with 40 missed. Adding roughly 60 uncovered statements yields approximately 93.8%, which breaks the 95% gate.

`terminal.py` is therefore added to coverage `omit`. Omitting a file from coverage is exactly how untested logic quietly accumulates, so the omission gets a guard: an architecture test asserting **`terminal.py` stays under 80 statements**.

That cap forces logic out into the pure modules where it is covered. If someone later wants to put a branch in there, the test says no.

### 4.4 The broker audit

Source spec §17.2 calls for `scripts/broker_audit.py`. It is read-only, so it belongs in this phase.

It connects to the demo account and dumps, for every intended symbol: `trade_mode`, `trade_exemode`, `filling_mode` bits, `trade_stops_level`, `trade_freeze_level`, `volume_min` / `volume_step` / `volume_max`, `trade_tick_value_loss`, swap rates, median spread over a session, and `account_info().margin_mode`.

In the source spec's own words: *"This single artifact determines which strategies are even implementable at that broker, and Report-A-style backtests built without it are fiction."*

Its output is committed to `docs/` as evidence. It runs **first** in the implementation, because its findings feed the contract-mapping tests with real values rather than invented ones — including concrete facts currently being guessed, such as whether the broker's EURUSD is named `EURUSD`, what its real `stops_level` is, and whether XAUUSD is shortable there.

---

## 5. Deliverables and Roadmap

### 5.1 Deliverables

| Area | Deliverable |
|---|---|
| Package | `brokers/mt5/` — actor, read-only adapter, three pure translators, thin terminal boundary |
| Dependency | `MetaTrader5 ; sys_platform == "win32"` |
| Boundary | import ban narrowed to a single-file exemption; the two test files split by responsibility |
| Contracts | `BrokerUnavailableError`, `NonDemoAccountError`, exit codes 8 and 9, `RejectReason.UNKNOWN` |
| Health | fifth step — reconcile every book, reported not enforced |
| Evidence | `scripts/broker_audit.py` and its committed report |
| Audit | gateway lifecycle events through the Phase 0 audit interface |

The audit row is a Phase 0.5 handoff obligation: gateway, configuration and operational events must be appended through the Phase 0 interface. Terminal-connected, demo-verified, disconnected and reconciled become audit events, giving a hash-chained forensic history of every terminal connection.

### 5.2 Revised roadmap position

| Phase | |
|---|---|
| **1 — this spec** | Read-only MT5 gateway |
| **1.5 — new** | Market-data ingest and storage (`marketdata/`), its own spec |
| 2 | Risk core |
| 3 | Execution — intent ledger, order manager, position guard; completes `BrokerAdapter` |

Splitting 1.5 out is deliberate. Storage format, retention and point-in-time correctness are a separate design surface, and bolting them onto the gateway spec would weaken both.

### 5.3 Exit criteria

1. All three test layers green; the coverage gate still met with `terminal.py` omitted and under its statement cap.
2. The broker audit report committed to `docs/`.
3. `uv run trading-house health` reports ready against the demo terminal, including reconciliation.
4. The demo guard proven to fire, via the fake terminal reporting a non-demo `trade_mode` — this cannot be safely tested live.
5. No `MetaTrader5` import anywhere outside `brokers/mt5/terminal.py`, enforced by test.
6. Invariants I-1 through I-16 all still hold; the existing acceptance suites stay green.

### 5.4 Risks

| Risk | Mitigation |
|---|---|
| A hung `mt5.*` call wedges the actor thread | Caller timeout, heartbeat stall, unhealthy report. In-process recovery is not possible; recorded in 2.5 |
| Broker reports a symbol spec we did not anticipate | The broker audit runs first and feeds real values into the mapping tests |
| `terminal.py` becomes a home for untested logic | Coverage omission paired with an 80-statement cap enforced by test |
| Magic collision across historical intents | Magic is a locator, not an identity; Phase 3 recovery scopes by magic and time window with the intent ledger authoritative |
| The demo terminal is repointed at a live account | Hard `trade_mode` guard inside `start()`, before any request is served |
| An unrecognised retcode is guessed as retryable | `RejectReason.UNKNOWN` classified `AUTHORITY` — fail closed to safe mode |
