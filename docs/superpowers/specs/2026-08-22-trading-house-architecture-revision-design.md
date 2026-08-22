# Trading House Architecture Revision Design

**Status:** Approved design
**Date:** 2026-08-22
**Source specification:** `mt5-multi-agent-trading-house-spec.md`
**Revises:** `docs/superpowers/specs/2026-08-03-phase-0-foundation-design.md`
**Delivery scope:** Phase 0.5 only — contracts and boundaries, not implementations

## Purpose

Phase 0 delivered a safety foundation whose canonical contracts are MT5-shaped.
This revision makes the core venue-neutral, adds horizon-scoped books so
scalping and swing trading can coexist under separate risk budgets, admits
equity CFDs as a third asset class, and defines the boundaries for two new
capabilities: an autonomous strategy foundry driven by coding agents, and a
learning memory.

The goal is a system that analyses markets and executes scalping and swing
trades across FX, metals and equity CFDs through MetaTrader 5, with a contract
surface that makes a second provider cheap to add later.

### Why now

Phase 0 declared `core/schemas.py` frozen, but `OrderIntent` carries `magic`,
`deviation_points`, `filling`, `retcode`, `broker_order_ticket` and
`broker_position_ticket` — all MT5-native. Phase 1 builds the MT5 gateway
against whatever contract exists. The branch is unmerged, has no dependents,
and has 215 passing tests that turn this rework into a verifiable refactor.
This is the cheapest this change will ever be.

## Decisions Record

Six decisions shaped this design. They are recorded because reversing any one
of them invalidates specific sections rather than the whole document.

| # | Decision | Chosen | Consequence |
|---|---|---|---|
| D-1 | Multi-provider driver | Clean adapter boundary now; MT5 the only live venue near-term; second provider later | No cross-provider portfolio aggregation is designed or built |
| D-2 | Meaning of "stocks" | MT5 share CFDs through the existing adapter | No settlement, locate or PDT model. Stocks are swing-only: spread plus commission makes CFD scalping uneconomic |
| D-3 | Scalp/swing isolation | Horizon-scoped books on one MT5 account, segregated by magic range | Books account for risk but do not ring-fence margin (see 3.4) |
| D-4 | Phase 0 schema rework | Break and redo now | Roughly 250 lines of schema plus their tests rewritten |
| D-5 | Scalp breadth | Up to about 5 instruments | One gateway actor with a priority queue suffices; instrumented so the real ceiling is measured rather than assumed |
| D-6 | Foundry autonomy | Auto-deploy to paper; human signature for live capital | The learning loop closes unattended; capital keeps a human gate |

## Scope

### Included

- Venue-neutral canonical contracts and the `BrokerAdapter` protocol.
- A neutral `InstrumentContract` replacing MT5's `SymbolContract`.
- `Quantity` as a `Decimal`-denominated, unit-tagged value.
- Horizon-scoped books, a generalised risk constitution, and a signed venue binding.
- `AgentProvider` protocol, `AgentRun` audit record, and `RunBudget`.
- `StrategyPackage`, and the generalisation of Phase 0 signing to any artifact.
- Trial-ledger schema and the deflation rule that depends on it.
- Memory Store A and Store B schemas, and the point-in-time read API.
- Gateway actor queue discipline, the idempotency spine, and the error taxonomy.
- Six new invariants, I-11 through I-16.

### Excluded

- Cross-provider portfolio aggregation, netting, or currency consolidation.
- Equity scalping; real-equities settlement (T+1), short locate, and PDT rules.
- Order routing or smart order routing across venues.
- The crypto adapter (roadmap Phase 11, unchanged).
- Concrete provider implementations, the foundry orchestration loop, the
  sandbox container, and the learned cost model.
- Any change to LangGraph orchestration beyond adding the provider seam.

The excluded implementations are deliberate. This phase designs the parts that
touch core schemas, the audit ledger and the strategy registry, because those
cannot be bolted on afterwards. Everything else is built in its own phase.

### Preserved without change

The review that produced this document found the following sound: the
four-plane model, the two-clock hot/warm/cold split, the rule that no LLM sits
in the hot path, the deterministic risk veto, the append-only hash-chained
audit ledger, and invariants I-1 through I-10.

---

## 1. The Venue-Neutral Core

**Principle:** canonical schemas describe intent and outcome. They never
describe venue mechanics. Anything a specific broker needs in order to act on
an intent lives behind the adapter.

### 1.1 OrderIntent

```python
class OrderIntent(CanonicalModel):
    intent_id: NonEmptyStr              # the idempotency key
    proposal_id: NonEmptyStr
    book: BookId
    instrument_id: InstrumentId         # neutral identity, not a broker symbol
    side: Side
    quantity: Quantity
    stop_loss: Price
    take_profit: Price | None
    time_in_force: TimeInForce          # GTC | DAY | IOC | FOK
    max_slippage_bps: NonNegativeFiniteDecimal
    state: IntentState
    t_submit_utc: datetime
    venue_ref: VenueRef | None          # written by the adapter
    outcome: ExecutionOutcome | None
```

Where each MT5 field went, and why it is genuinely venue-specific:

| Removed | Relocated to | Rationale |
|---|---|---|
| `magic` | `Mt5VenueRef.magic` | MT5's client-order-ID encoding. `intent_id` is the real idempotency key; the adapter owns the mapping |
| `deviation_points` | derived from `max_slippage_bps` | Points are an MT5 unit; basis points survive any venue |
| `filling` | adapter negotiates from `supported_fills` and TIF | Fill-mode bitmasks are pure MT5 |
| `retcode` | `ExecutionOutcome.reject_reason` (neutral enum) | Raw retcode retained in `Mt5VenueRef` for forensics |
| `broker_order_ticket` | `Mt5VenueRef.order_ticket` | Ticket identity is per-venue by definition |
| `broker_position_ticket` | `Mt5VenueRef.position_ticket` | As above |

### 1.2 VenueRef

A discriminated union, using the pattern already proven by `RiskDecision`:

```python
class Mt5VenueRef(CanonicalModel):
    venue: Literal["mt5"]
    magic: PositiveInt
    server_symbol: NonEmptyStr          # e.g. "EURUSD.raw"
    order_ticket: PositiveInt | None = None
    position_ticket: PositiveInt | None = None
    retcode: int | None = None

# One variant today. Pydantic requires a genuine union for a discriminator,
# so this stays a bare alias until a second venue exists, at which point it
# widens without any consumer changing:
#   VenueRef = Annotated[Mt5VenueRef | XVenueRef, Field(discriminator="venue")]
VenueRef = Mt5VenueRef
```

Adding a provider means adding one variant and one adapter. It touches nothing
in risk, portfolio, strategies, or audit.

### 1.3 Quantity

Sizing computes risk money, then price distance, then quantity. The formula is
neutral; the contract values are venue-supplied.

```python
class Quantity(CanonicalModel):
    """Zero-permitting. Used only where zero is a valid outcome."""
    amount: NonNegativeDecimal
    unit: Literal["lots", "shares", "base_units", "contracts"]


class PositiveQuantity(Quantity):
    """The only quantity type an executable intent may carry."""
    amount: PositiveDecimal
```

Two types rather than one runtime check. `OrderIntent`, `PositionState` and the
executable risk decisions take `PositiveQuantity`; only `RejectedRiskDecision`
takes the zero-permitting `Quantity`. A zero-volume live order is therefore
unrepresentable rather than merely rejected — continuing the discipline the
Phase 0 commit history already established under "make unsafe execution states
unrepresentable".

`Decimal`, not `float`. Float arithmetic against `quantity_increment` produces
broker-rejected orders, and the constitution already uses `Decimal` throughout.

### 1.4 InstrumentContract

Replaces MT5's `SymbolContract`. All distances are expressed in **price units,
not points**. Points are an MT5 encoding, and the source specification's own
warning about confusing `trade_tick_value_loss` with `tick_value` becomes
unrepresentable once the field is named for what it does.

```python
class InstrumentContract(CanonicalModel):
    instrument_id: InstrumentId
    asset_class: AssetClass                 # FX | METAL | EQUITY_CFD
    base_currency: NonEmptyStr
    quote_currency: NonEmptyStr
    price_increment: Decimal                # was: point / tick_size
    quantity_increment: Decimal             # was: volume_step
    quantity_min: Decimal
    quantity_max: Decimal
    value_per_price_increment: Decimal      # was: trade_tick_value_loss
    min_stop_distance: Decimal              # was: stops_level, now in price
    freeze_distance: Decimal                # was: freeze_level, now in price
    session_calendar_id: NonEmptyStr        # NEW - required for equity CFDs
    financing: FinancingModel               # swap | dividend_adjustment
    shortable: bool                         # NEW - share CFDs are often long-only
    supported_fills: frozenset[FillPolicy]
```

### 1.5 BrokerAdapter

Eight methods, total:

```python
class BrokerAdapter(Protocol):
    def describe_instrument(self, iid: InstrumentId) -> InstrumentContract: ...
    def snapshot(self, iids: Sequence[InstrumentId]) -> MarketSnapshot: ...
    def precheck(self, intent: OrderIntent) -> PrecheckResult: ...
    def submit(self, intent: OrderIntent) -> ExecutionOutcome: ...
    def amend_protection(
        self, ref: VenueRef, sl: Price, tp: Price | None
    ) -> ExecutionOutcome: ...
    def close(self, ref: VenueRef, qty: Quantity | None) -> ExecutionOutcome: ...
    def reconcile(self, book: BookId) -> ReconciliationReport: ...
    def health(self) -> VenueHealth: ...
```

Retcode taxonomy, filling negotiation, magic allocation, portable-mode terminal
lifecycle and server-time offset all live behind this surface.

---

## 2. The Cognition Layer

### 2.1 AgentProvider

Coding and reasoning models are providers behind one interface, exactly as
brokers are.

```python
class AgentProvider(Protocol):
    def capabilities(self) -> ProviderCapabilities: ...
    def run(
        self, task: AgentTask, sandbox: SandboxHandle, budget: RunBudget
    ) -> AgentRun: ...
```

| Shape | Examples | Used for |
|---|---|---|
| CLI-driven | Claude Code, Codex CLI | Subprocess inside the sandbox with shell and filesystem access to a scratch clone. Code synthesis |
| API-driven | Messages API tool-loops | Structured classification, extraction, and opinion generation in the warm loop |

`ProviderCapabilities` declares `can_write_code`, `can_run_shell`,
`supports_tools`, `max_context`, and `deterministic_seed`.

`RunBudget` caps wall-clock time, tokens, cost and tool calls. Any breach kills
the run and records `BUDGET_EXCEEDED`. An unbounded coding-agent loop is a cost
incident, not a hypothetical.

`AgentRun` is a forensic record appended to the Phase 0 audit ledger: provider
id, model id, prompt hash, tool-transcript hash, produced-diff hash, budget
consumed, and outcome. Agent behaviour is therefore reconstructible after the
fact using machinery that already exists and is integration-tested.

### 2.2 Per-plane tool authority

Section 5.2 of the source specification places `shell_exec` in
`FORBIDDEN_FOR_ALL_LLM_ROLES` and makes any attempt a circuit-breaker trigger.
A code-writing agent requires shell. This is a genuine conflict in the source
specification, resolved here by making the prohibition per-plane rather than
global.

| Plane | Shell | Rationale |
|---|---|---|
| Foundry sandbox (research) | Permitted | Container with no network egress except a package mirror, no broker credentials, no live database, and a read-only point-in-time data replica |
| Warm/cold agents touching the control plane | Forbidden; circuit-breaker trigger | These agents can influence live configuration |
| Hot path | No agents at all | Unchanged |

This yields a stronger and more testable rule than the blanket prohibition:

> **I-11.** No process holding broker credentials may execute agent-authored
> code or shell commands.

### 2.3 The Strategy Foundry

```text
Hypothesis --[research agent]--> StrategySpec (schema-bound)
                                        |
                    +-------------------v-------------------+
                    | SANDBOX - no network, no credentials  |
                    | coding agent writes Strategy vs ABC   |
                    +-------------------+-------------------+
                                        v
              Static gate - ABC conformance, no I/O, no clock,
              AST-checked; plus determinism replay (byte-identical x2)
                                        v
                  Event-driven backtest at 1.5x modelled cost
                                        v
        === TRIAL LEDGER - register EVERY trial, including abandoned ===
                                        v
              Red-team agent - leakage, lookahead, hidden leverage
                                        v
         Promotion gate - DSR / PBO / CPCV / walk-forward,
         deflated by the ledger's TOTAL trial count, not the shortlist
                                        v
                  Signed StrategyPackage -> control plane
                                        v
                     AUTO-DEPLOY -> paper / shadow plane
                                        v
              ---- human signature ----> live capital
```

**Generated code is data until signed.** A `StrategyPackage` is source, spec,
trial-ledger reference, validation report, and signature. The live plane loads
only signed packages. Phase 0's `signing.py` generalises from "sign the
constitution" to "sign any artifact"; it is already hardened with
`O_EXCL|O_NOFOLLOW` descriptor-only writes and verify-before-parse ordering.

**Determinism replay is a hard gate.** Two runs over identical data must
produce byte-identical output. Non-determinism indicates hidden state or
wall-clock access. Reject; do not debug.

**The static gate is AST-checked, not trusted.** `Strategy` is a pure function
of features and position state returning `TradeProposal | None`. No network, no
file I/O, no wall-clock access. This is cheap and catches most agent mistakes
before a backtest consumes an hour.

**The trial ledger registers everything.** Deflated Sharpe and PBO correct for
the number of trials attempted. A human researcher runs fifty backtests; an
agent loop runs fifty thousand. If the foundry registers only the candidates it
favours, every downstream statistic is fiction.

> **I-12.** Every strategy trial, including abandoned ones, is registered in
> the trial ledger before its results may inform any promotion decision.

### 2.4 Memory

**The rule: memory changes proposals and cost estimates. It never changes
limits.** I-1 and I-2 hold unchanged.

Memory is split by epistemic status, and that split is the whole design.

#### Store A — Observed Facts

Written only by deterministic post-trade code from realised fills.

- realised slippage distribution by instrument, session and size bucket
- spread percentiles by instrument and session
- fill rates by fill policy
- stop-distance versus adverse-excursion distributions
- modelled-versus-actual cost error

Store A feeds the cost model and sizing. It is adaptive statistics rather than
inference, so it can safely influence live trading while still passing through
the risk engine like everything else. This is where learning from past trades
to improve profitability actually lives, and it is the higher-value half of the
memory feature: unmodelled cost is the most common cause of death for
systematic retail strategies.

#### Store B — Agent Beliefs

Hypotheses, rationales, debate transcripts, regime narratives, and post-mortems.
Always labelled with the generating `AgentRun`. Never feeds the hot path.
Consumed by foundry hypothesis seeding and by regime-conditioned agent
reliability weighting.

#### Three guards, one per failure mode

1. **Poisoning and self-reinforcement.** An agent writing a belief and later
   reading it back as evidence produces the correlated-error spiral that
   section 5.1 of the source specification warns about. Store A is
   write-restricted to deterministic code.

   > **I-13.** Memory Store A is writable only by deterministic post-trade
   > code. No agent may write a fact.

2. **Recency overfitting.** Memory that chases last week's regime is worse than
   no memory. All memory-derived parameters shrink toward priors on an explicit
   half-life and are themselves walk-forward validated.

3. **Lookahead.** Every read is point-in-time, keyed by `availability_time`.
   Backtests read memory as of the simulated instant. This is precisely what
   Phase 0's three-timestamp `Stamped` discipline was built for.

   > **I-14.** Every memory read is point-in-time by `availability_time`.

#### Storage

Both stores are append-only and hash-chained, generalising the Phase 0 audit
ledger to additional chained tables. Memory is never updated in place;
corrections are new entries superseding old ones. This keeps the question
"why did the system do that in March" answerable.

---

## 3. Books, Horizons, and the Risk Constitution

### 3.1 Books generalise from a fixed pair to a declared map

Today `constitution/models.py` hardcodes exactly two fields:

```python
class Books(ConstitutionModel):
    core: BookLimits
    sleeve: BookLimits
```

This becomes `Mapping[BookId, BookLimits]`, with the existing exact-`Decimal`
sum-to-one validator generalised across the map. This deliberately avoids a
cross-product: rather than forcing every combination of risk tier, asset class
and horizon into existence, the operator declares exactly the books they want,
and each book states what it may trade.

```yaml
books:
  fx_scalp:
    capital_fraction: 0.30
    horizon: scalp
    asset_classes: [fx, metal]
    risk_per_trade_pct: 0.25
    max_concurrent_positions: 3
  fx_swing:
    capital_fraction: 0.45
    horizon: swing
    asset_classes: [fx, metal]
  equity_swing:
    capital_fraction: 0.15
    horizon: swing
    asset_classes: [equity_cfd]
  sleeve:
    capital_fraction: 0.10
    horizon: swing
    asset_classes: [fx, metal]
    risk_per_trade_pct: 1.5
```

The core/sleeve distinction is not lost, it is subsumed. Sleeve was always
"a book with a different risk appetite", which is exactly what a book now is.
Capital fractions still sum to exactly `1`.

Note that "equity CFDs are swing-only" (D-2) is a property of *this
configuration*, not a constraint in code. No `equity_scalp` book is declared,
so no equity scalping can occur. Nothing prevents an operator from declaring
one later — the constraint that actually binds is economic
(`min_expected_edge_after_cost_bps` in 3.2), which a share-CFD scalp would fail
on spread and commission alone. The design encodes the reasoning, not a
hard-coded prohibition.

### 3.2 Horizon-conditional limits

`BookLimits` gains a discriminated horizon block. The two horizons need
genuinely different limits, and flattening them into one set is how a swing
limit ends up silently applied to a scalp.

| Scalp-only | Swing-only |
|---|---|
| `max_orders_per_minute` (previously firm-level only) | `max_overnight_positions` |
| `max_spread_multiple_at_entry` | `max_weekend_exposure_pct` |
| `min_expected_edge_after_cost_bps` | `max_swap_cost_pct_of_expected_edge` |
| `max_position_duration_seconds` (auto-flatten) | `gap_risk_multiple` — stop sizing must assume a gap |
| `flat_by_session_close` | `earnings_blackout_days` (equity CFD) |

`min_expected_edge_after_cost_bps` matters most for scalping: at scalp horizons
cost is the strategy. A scalp proposal whose modelled edge does not clear
spread, commission and expected slippage by a declared margin is rejected
before it reaches sizing.

`SafeModeTriggers` becomes per-horizon for the same reason. A scalp book should
enter safe mode on a two-second tick age; a swing book should not care.

### 3.3 The venue binding is a second signed config

The constitution must stay venue-neutral. It is signed, and switching brokers
should not invalidate risk limits. But the mapping from `BookId` to magic range
and from `InstrumentId` to server symbol consists of MT5 facts, and
reconciliation integrity depends on them absolutely.

Therefore `config/venue_binding.mt5.yaml`, signed through the same Ed25519
loader Phase 0 already provides:

```yaml
venue: mt5
books:
  fx_scalp:     { magic_range: [110000, 119999] }
  fx_swing:     { magic_range: [120000, 129999] }
  equity_swing: { magic_range: [130000, 139999] }
  sleeve:       { magic_range: [140000, 149999] }
instruments:
  fx.eurusd:    { server_symbol: "EURUSD.raw" }
  metal.xauusd: { server_symbol: "XAUUSD" }
```

Book identity is therefore recoverable from any live position via its magic,
which is what lets the position guard determine which limits apply to a
position it discovers after a restart.

### 3.4 One account means books share margin

This is the cost of D-3, stated explicitly rather than discovered later.
**Books are risk-accounting boundaries, not margin-isolated pools.** A margin
call hits everything simultaneously. Per-book `max_gross_leverage` is therefore
necessary but not sufficient, and the firm block must bind a hard aggregate:

- `max_aggregate_open_risk_pct` — summed across all books
- `max_gross_leverage` — firm-wide, over the shared margin pool
- `max_correlated_cluster_risk_pct` — computed across books, never within one

The last point is the subtle one. An `fx_scalp` EURUSD long and an `fx_swing`
EURUSD long are the same exposure wearing two hats. The correlation budget must
be firm-level, or the book split hides risk instead of containing it.

> **I-16.** Correlation and leverage budgets are computed firm-wide across
> books, never per-book.

### 3.5 Circuit-breaker scoping

| Trigger | Scope | Action |
|---|---|---|
| Daily loss stop, book drawdown, consecutive rejects | Book | Halt that book. Scalp books flatten; swing books stop new entries and keep protection running |
| Aggregate risk, firm drawdown, firm leverage | House | Halt all new entries |
| Reconciliation mismatch, clock drift, gateway death, forbidden-tool attempt (I-11) | House | SAFE MODE — flatten everything |

The asymmetry is deliberate. A scalp book breaching its daily stop must not
force-close healthy swing positions. But a reconciliation mismatch must halt
everything, because at that moment the system no longer knows what it owns, and
every limit above is computed from a position state that can no longer be
trusted.

---

## 4. Gateway, Data Flow, and Failure

### 4.1 The gateway actor uses a priority queue, not FIFO

One terminal, one account, one thread owning every `mt5.*` call. The
non-obvious part is the queue discipline.

| Priority | Operations | Rule |
|---|---|---|
| P0 | amend protection, close | Never queued behind anything. Bounded depth |
| P1 | precheck, submit | |
| P2 | reconciliation | |
| P3 | quote and bar polls | Starvable by design |

FIFO would let a stop modification sit behind five quote polls. At scalp
horizons that is the difference between a managed stop and a blown one. If P0
depth exceeds its threshold, that is itself a safe-mode trigger: the gateway
can no longer keep up with protecting open positions, which is the one thing it
must always be able to do.

Instrumented from day one, per D-5: per-class queue depth, actor loop period,
MT5 call-latency histogram, and time since last successful poll per instrument.
These are the numbers that reveal when five instruments has become too many.

The actor publishes a heartbeat. A hung `mt5.*` call is otherwise
indistinguishable from a quiet market; a stalled heartbeat triggers safe mode.

Reconnection never auto-resumes trading. Any connectivity gap forces
reconciliation first.

### 4.2 Data flow

```text
poll (P3) -> quality gate -> features -> strategy eval [CLOSED BARS ONLY]
   -> TradeProposal -> cost model [Memory Store A] -> portfolio
   -> RISK ENGINE -> precheck (P1) -> submit (P1) -> intent ledger
   -> reconcile (P2) -> position guard (P0)
```

Strategy evaluation sees closed bars only. Ticks feed spread state and the
position guard, never bar-based signals. This is what prevents repainting.

The quality gate is book-aware: quarantining EURUSD blocks new entries in both
`fx_scalp` and `fx_swing`, but does not close existing swing positions.
Quarantine is an entry gate, not an exit trigger.

Warm and cold loops emit `StrategyConfigDelta` to the control plane, applied
atomically. They never emit order instructions. This is unchanged from the
source specification.

### 4.3 The idempotency spine

`order_send` can succeed at the broker while its response is lost. This is the
highest-consequence failure in the system, so the mechanism is stated exactly.

1. The intent is persisted before submission, in state `SUBMITTING`.
2. The adapter derives `magic` deterministically from `intent_id` within the
   book's magic range. The same intent always maps to the same magic. This is
   what makes recovery possible at all.
3. Any uncertainty — timeout, crash, unrecognised retcode — moves the intent to
   `UNKNOWN`. Never blind-retry.
4. Recovery queries `positions_get`, `history_orders_get` and
   `history_deals_get` by that magic. Found means `CONFIRMED`. Absent, with
   market data proving the submission window closed, means `FAILED`.
5. No new intent is created for that proposal until the old one resolves.

Phase 0's `IntentState` literal already enumerates exactly
`SUBMITTING | CONFIRMED | UNKNOWN | RECONCILING | FAILED | REJECTED`. The work
is making the transition function total and proving it.

This adds a fifth step to the health gate that Phase 0 Task 11 will build:
constitution, database, migration, audit chain, **reconcile every book against
the venue**, then ready. A process that starts without reconciling is a process
that does not know what it owns.

### 4.4 Error taxonomy

Phase 0's typed, redacted error boundary extends to venue failures.

| Class | Examples | Response |
|---|---|---|
| Transient | requote, price changed, timeout, no connection | Bounded retry with a fresh price, only after idempotency resolution |
| Contractual | invalid stops, invalid volume, market closed, unsupported filling | The instrument model is stale. Reject the intent and re-fetch `InstrumentContract` |
| Authority | insufficient funds, trade disabled, account disabled | Safe mode. Never retry |

The contractual class hides a trap. MT5 retcode `10016 invalid stops` means
`min_stop_distance` is out of date. The tempting fix — widen the stop and
resubmit — directly violates the constitution's `stop_widening: forbidden`. The
correct response is to refresh the contract, recompute size from the new
distance, and drop the trade if the resulting risk no longer fits the book.

> **I-15.** Error recovery may not violate a constitution prohibition.

---

## 5. Migration, Roadmap, and Invariants

### 5.1 Concrete changes to the Phase 0 code

| File | Change |
|---|---|
| `core/schemas.py` | The bulk. `OrderIntent` rewritten; new `Quantity`, `Price`, `TimeInForce`, `ExecutionOutcome`, `RejectReason`, `VenueRef` union, `InstrumentId`, `AssetClass`, `BookId`. `Book` enum becomes a validated `BookId`. Lot-denominated fields across `TradeProposal`, `RiskDecision` and `PositionState` become `Quantity` |
| `core/instruments.py` | New. `InstrumentContract`, `FinancingModel`, `FillPolicy`, `SessionCalendar` |
| `constitution/models.py` | `Books` becomes a mapping; horizon block on `BookLimits`; `AssetClassLimits`; per-horizon `SafeModeTriggers`; firm aggregate limits |
| `constitution/loader.py`, `constitution/signing.py` | Generalise to `load_signed(artifact, sig, key) -> VerifiedArtifact`. Constitution, venue binding and strategy packages then share one verified-load path |
| `config/` | New `venue_binding.mt5.yaml` and its signature; constitution rewritten and re-signed |
| `brokers/base.py` | New. The eight-method `BrokerAdapter` |
| `agents/providers/base.py` | New. `AgentProvider`, `AgentRun`, `RunBudget` |
| `memory/` | New. Store A and Store B schemas, point-in-time read API |
| `migrations/0002_*` | Memory chains and trial ledger, reusing the proven `audit.append_event` pattern |
| `tests/` | The 215 existing tests updated; `tests/property/` created |

Two findings from the pre-revision code review are fixed as a by-product rather
than as separate cleanup:

- `FrozenDict` and `FrozenList` move from `audit/models.py` into `core` and are
  applied to `RegimeAssessment.probabilities`, closing the shallow-freeze hole
  where a frozen model still permitted mutation of a contained dict.
- `RejectedRiskDecision`'s `Literal[0.0]` becomes a zero-valued `Quantity`,
  retiring the strict-mode wart where integer `0` was silently accepted.

`core/schemas.py` roughly doubles, from 241 to about 450 lines. The work is
mechanical and test-guarded throughout.

### 5.2 Revised roadmap

| Phase | Status |
|---|---|
| 0 Foundation | Finish Tasks 11-14: health gate, CLI, property tests, CI |
| **0.5** | **New — this revision.** Contracts, books, adapter and provider protocols, memory schemas |
| 1 MT5 gateway | Now implements `BrokerAdapter` rather than defining the shape ad hoc |
| 2 Risk core | Now book-aware and horizon-aware |
| 3 Execution | Idempotency spine, position guard |
| **3.5** | **New — Memory Store A** |
| 4 One strategy | One scalp or one swing, not both |
| 5 Validation | Plus the trial ledger |
| **5.5** | **New — Strategy Foundry**, auto-deploying to paper |
| 6 Agent layer | Plus Memory Store B |
| 7-11 | Unchanged |

Two sequencing arguments are load-bearing.

**Memory Store A lands at 3.5, before any strategy exists.** It improves
execution quality independently of strategy, it is deterministic so it carries
no agent risk, and by the time Phase 4 arrives the cost model is calibrated
against real fills rather than assumptions.

**The Foundry cannot precede Phase 5.** It requires the promotion gates and
trial ledger to already exist. Without them it is a machine for generating
unvalidated candidates at scale, which is worse than having no foundry.

### 5.3 Invariants

I-1 through I-10 are preserved unchanged. Six are added.

| # | Invariant | Enforced in |
|---|---|---|
| I-11 | No process holding broker credentials may execute agent-authored code or shell commands | Plane-scoped tool registry; process capability test |
| I-12 | Every strategy trial, including abandoned ones, is registered in the trial ledger before its results may inform any promotion decision | `research/trial_ledger.py`; promotion gate reads ledger count |
| I-13 | Memory Store A is writable only by deterministic post-trade code. No agent may write a fact | Database role separation, mirroring the audit ledger |
| I-14 | Every memory read is point-in-time by `availability_time` | Memory read API; backtest replay test |
| I-15 | Error recovery may not violate a constitution prohibition | Error handler unit tests against each prohibition |
| I-16 | Correlation and leverage budgets are computed firm-wide across books, never per-book | `risk/engine.py`; property test across book combinations |

### 5.4 Testing strategy

- **Deterministic replay harness.** Recorded tick and bar streams through the
  whole hot path, producing byte-identical output. Shares machinery with the
  foundry determinism gate in 2.3.
- **Fault injection at the adapter seam.** Because `BrokerAdapter` is eight
  methods, a `FaultyAdapter` can inject timeouts, lost confirmations, partial
  fills and requotes. This is the payoff for the narrow interface: the worst
  execution bugs become unit-testable with no broker involved.
- **Idempotency property test.** Hypothesis is already a declared dependency
  with zero usages. Its first job: for every interleaving of crash points
  during submission, exactly zero or one position exists for a given
  `intent_id`.
- **Constitution property tests.** Capital fractions sum to one across any
  declared book set; horizon limits never cross-apply.
- **Demo-account integration.** The roadmap's existing Phase 1 exit criterion,
  unchanged.

### 5.5 Risks

| Risk | Mitigation |
|---|---|
| Books share margin, so a breach in one can margin-call the others | Firm-wide aggregate risk and leverage limits (3.4); revisit separate accounts if ring-fencing becomes necessary |
| An autonomous foundry inflates multiple-testing risk | I-12 plus deflation by total ledger trial count |
| Memory drives recency chasing | Shrinkage toward priors on an explicit half-life; walk-forward validation of memory-derived parameters |
| Agent cost runaway | `RunBudget` ceilings on wall-clock, tokens, cost and tool calls |
| The five-instrument scalp ceiling proves wrong | Gateway instrumented from day one; P0 queue depth is a safe-mode trigger, so the ceiling fails loudly rather than silently |
| Coverage gate sits 0.19% above its 95% threshold and needs Docker to pass | CI must gate coverage on the Linux job only; Windows runs unit tests without the coverage gate |
