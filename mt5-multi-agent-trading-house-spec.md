# MT5 Multi-Agent Trading House — Consolidated Engineering Specification

**Document type:** Build specification for an autonomous coding agent
**Version:** 1.0 (consolidation of Research Report A "Multi-Agent AI Trading House: Engineering Blueprint" and Research Report B "Research and Design Blueprint for an AI Multi-Agent Trading House")
**Primary broker interface:** MetaTrader 5 (MetaQuotes `MetaTrader5` Python package)
**Asset classes:** FX, crypto (CFD via MT5, spot via optional adapter), equities/indices (CFD via MT5)
**Status of the 100%-in-5-days objective:** Rejected as an engineering requirement. Reframed in §1.3 as a bounded high-conviction sleeve. Read §1 before writing any code.

---

## 0. How a coding agent must use this document

### 0.1 Reading order

1. §1 — Objective function and risk constitution. **These are hard constraints. Every later section is subordinate to them.**
2. §2 — Architecture and repository layout. Build the skeleton first.
3. §3 — MT5 integration layer. This is the highest-risk, highest-detail section; most implementation bugs live here.
4. §4 — Canonical schemas. Implement these before any agent or strategy.
5. §5–§9 — Agents, orchestration, risk, sizing, trade lifecycle.
6. §10–§12 — Strategy layer, research, promotion gates.
7. §13–§16 — Ops, security, testing, roadmap.
8. §17–§18 — Governance and appendices.

### 0.2 Non-negotiable build invariants

These are assertions the codebase must satisfy at all times. A pull request that violates one is invalid regardless of backtest results.

| # | Invariant | Enforced by |
|---|---|---|
| I-1 | No LLM output ever reaches the broker without passing a deterministic risk gate that can reject or resize it. | `risk/engine.py`, unit-tested |
| I-2 | No LLM can modify, disable, or bypass a risk limit, kill switch, or circuit breaker at runtime. Limits are loaded from signed config, not from model output. | Config loader + tool permission model |
| I-3 | Position size is derived from a monetary loss budget and a stop distance. It is never derived from a profit target or a model's verbal confidence. | `risk/sizing.py` |
| I-4 | Every open position has a protective stop known to the broker, or the system is in `SAFE_MODE`. | `execution/position_guard.py` |
| I-5 | All MT5 calls occur in exactly one process and one thread (the MT5 Gateway actor). | `brokers/mt5/gateway.py` |
| I-6 | Order submission is never blindly retried. A lost response triggers reconciliation, not resend. | `execution/order_manager.py` |
| I-7 | The execution plane keeps running, protecting and closing positions, when every LLM provider is unreachable. | Integration test `test_llm_blackout.py` |
| I-8 | Martingale sizing, stop removal, and loss-recovery leverage increases are structurally impossible, not merely discouraged. | Risk engine rejects; unit test asserts rejection |
| I-9 | Every strategy promoted to live capital has an entry in the trial ledger recording how many configurations were tested. | `research/trial_ledger.py` |
| I-10 | All timestamps are stored as UTC with an explicit `availability_time` distinct from `event_time`. | Schema validation |
| I-17 | Every market-data read is filtered by `availability_time` against an explicit `as_of`. No read path can expose a bar before it was knowable. | `tests/acceptance/test_phase1_5.py` |
| I-18 | A feature is computed over a fixed lookback, so the same instrument, timeframe, period and `as_of` always yield the same value. | `tests/unit/features/test_engine.py` |
| I-19 | An approved decision's `risk_money` never exceeds the book's budgeted risk, and — unless the lot cap bound it — falls short by less than one lot step's worth. | `tests/property/test_risk.py` |
| I-20 | No order is sent while any earlier intent is unresolved. | `execution/reconciler.py` (`require_clean_ledger`) |

### 0.3 Definition of done for the whole system

The system is "done" for a given phase when the phase's acceptance tests in §15 pass on a fresh checkout, in CI, without manual intervention.

---
## 1. Objective function, target reckoning, and the risk constitution

### 1.1 What both source reports agree on

Report A and Report B were produced independently and converged on the same conclusions. Where they agree, treat the conclusion as settled:

| Point of agreement | Consolidated statement |
|---|---|
| Feasibility of the architecture | A multi-agent system can credibly reproduce the functions of a trading house: data stewardship, research, strategy generation, portfolio construction, execution, risk control, monitoring, and post-trade analysis. |
| Evidence quality of LLM trading agents | Weak. Report B cites a 2026 review of 77 agentic-trading studies in which only 19 met basic closed-loop criteria, only two reported time-consistent data splits, only one modelled transaction costs, only one documented survivorship handling, and none reached the highest reproducibility tier. Report A independently flags that published frameworks (TradingAgents, FinMem, FinAgent) report backtest-only results over 3–6 month windows with look-ahead risk. |
| Role of the LLM | Research, hypothesis generation, news/event extraction, adversarial debate, code drafting, post-trade explanation. **Not** sizing, not stop placement, not order validation, not the latency-critical path. |
| Risk authority | Must be independent of, and superior to, every profit-seeking agent. Absolute veto. |
| Overfitting | The primary silent failure mode. Requires Deflated Sharpe Ratio, Probability of Backtest Overfitting, purged/embargoed CV, walk-forward, and an immutable trial ledger. |
| The return target | Not achievable, and dangerous as a design input. |

### 1.2 The target, computed

Both reports independently ran the arithmetic. Consolidated:

- Doubling every 5 trading days over ~252 trading days per year = ~50 doublings.
- 2^50 ≈ 1.13 × 10^15. Report B's variant (2^(252/5)) ≈ 1.49 × 10^15. Same order of magnitude.
- Starting from E 20,000, one year of that schedule exceeds global GDP by several orders of magnitude.

Calibration against the best results ever recorded:

| Reference | Result | Implied per-trading-day geometric rate |
|---|---|---|
| Renaissance Medallion, 1988–2018 | ~66%/yr gross, ~39% net; one losing year | ~0.20%/day |
| Soros / Quantum | ~30%/yr over decades | ~0.10%/day |
| Larry Williams, 1987 World Cup Championship (single competition year, extreme leverage, never repeated) | 11,376% | ~1.9%/day |
| **This target** | **2^50 /yr** | **~15%/day, sustained** |

The single most extreme verified result in competitive trading history is roughly one-eighth of the requested run-rate, and it was a one-off. Report B's framing is the one to encode: **a backtest that reproduces this schedule should trigger an automatic presumption of data leakage, misaligned prices, unmodelled costs, unrealistic leverage, or overfitting, until independently disproved.**

Base rates, from both reports:
- CFTC: roughly two-thirds of customers at registered OTC forex dealers lose money after financing, fees and expenses.
- ESMA: 74–89% of retail CFD accounts typically lose money.
- Chague, De-Losso & Giovannetti (FEA-USP): of 1,551 Brazilian day traders persisting 300+ days, 97% lost money; 0.4% earned more than a bank teller; no evidence of learning.
- Barber, Lee, Liu & Odean (Taiwan, 1992–2006): under 1% of day traders predictably earn positive abnormal returns net of fees.

### 1.3 Reframing: the objective function the system actually optimises

Replace "100% in 5 days" with a lexicographic objective. The system optimises level *n* only subject to level *n−1* being satisfied.

```
1. SURVIVAL     — no ruin, no uncontrolled loss, no unprotected position, no operational failure
2. INTEGRITY    — every signal, order, fill, model version and data snapshot reproducible
3. NET EDGE     — positive expectancy after spread, commission, slippage, swap, funding, impact, infra
4. ROBUSTNESS   — acceptable across volatility, liquidity and macro regimes
5. SCALABILITY  — capital increases only where live evidence supports capacity
6. RETURN       — maximise risk-adjusted net return within the approved risk budget
```

**The aggression the user wants is preserved, but bounded, via the high-conviction sleeve:**

| Book | Capital share | Per-trade risk | Daily loss stop | Purpose |
|---|---|---|---|---|
| Core book | 85–95% | 0.25–0.50% of core equity | 1.5% of core equity | Multi-strategy, fractional-Kelly, survives regime shifts |
| **High-conviction sleeve** | **5–15%** | **1.0–2.0% of sleeve equity** | **8% of sleeve equity** | Asymmetric, higher-leverage opportunities. **Total loss of the sleeve must be survivable and pre-authorised.** |
| Sleeve refill rule | — | — | — | The sleeve is refilled from core profits only, never from core principal, and never more than once per quarter. |

This gives genuine risk-taking a home while making it structurally incapable of killing the operation. A coding agent must implement the sleeve as a **separate risk account** in the risk engine with its own limits, its own MT5 magic-number range, and its own kill switch.

### 1.4 The risk constitution (implement as `config/risk_constitution.yaml`, signed, load-once)

```yaml
# Values are STARTING POINTS derived from Report A and Report B. They are raised
# only by accumulated live evidence, never to chase a return target.
version: 1
signature_required: true          # loader refuses unsigned/modified files
books:
  core:
    capital_fraction: 0.90
    risk_per_trade_pct: 0.35        # of book equity
    max_concurrent_positions: 8
    daily_loss_stop_pct: 1.5
    max_drawdown_halt_pct: 8.0      # halts book, requires human unlock
    max_gross_leverage: 5.0
  sleeve:
    capital_fraction: 0.10
    risk_per_trade_pct: 1.5
    max_concurrent_positions: 2
    daily_loss_stop_pct: 8.0
    max_drawdown_halt_pct: 40.0     # sleeve may be lost; core must not fund it
    max_gross_leverage: 20.0
firm:
  max_total_drawdown_halt_pct: 10.0
  max_correlated_cluster_risk_pct: 1.0   # summed risk across a correlation cluster
  max_single_symbol_risk_pct: 0.7
  max_orders_per_minute: 30
  max_consecutive_rejects: 5
prohibitions:                      # enforced structurally, see I-8
  martingale_sizing: forbidden
  averaging_into_losers: forbidden_unless_declared_in_strategy_spec
  stop_removal: forbidden
  stop_widening: forbidden          # stops may only move toward profit
  leverage_increase_after_loss: forbidden
  trading_without_protective_stop: forbidden
safe_mode_triggers:                 # see §13.2
  max_tick_age_seconds: 5
  max_spread_multiple_of_median: 3.0
  max_clock_drift_ms: 500
  reconciliation_mismatch: true
  slippage_breach_sigma: 3.0
```

---
## 2. System architecture and repository layout

### 2.1 Four planes (consolidated from Report B's plane model plus Report A's hot/cold path split)

```
┌─ RESEARCH PLANE ────────────────────────────────────────────────┐
│ LLM agents · notebooks · feature research · backtests · RAG      │
│ NO live trading credentials. NO network path to the broker.      │
└───────────────────────┬─────────────────────────────────────────┘
                        │ signed, versioned strategy package
                        ▼
┌─ CONTROL PLANE ─────────────────────────────────────────────────┐
│ strategy registry · approvals · risk policy · deployment gates   │
│ Human unlock required for: limit increases, drawdown halt reset  │
└───────────────────────┬─────────────────────────────────────────┘
                        │ activated strategy + parameter set
                        ▼
┌─ LIVE TRADING PLANE (HOT PATH — no LLM) ────────────────────────┐
│ MT5 gateway → feature engine → signal engines → portfolio →      │
│ risk engine → order manager → MT5 → reconciliation → position    │
│ guard (stops, trailing, exits, circuit breakers)                 │
│ MUST function with zero LLM availability.                        │
└───────────────────────┬─────────────────────────────────────────┘
                        ▼
┌─ OBSERVABILITY PLANE ───────────────────────────────────────────┐
│ metrics · logs · traces · alerts · append-only audit · P&L attr  │
└─────────────────────────────────────────────────────────────────┘
```

### 2.2 Two clocks

This is the single most important structural decision, and both reports insist on it:

| Loop | Period | Contents | LLM allowed? |
|---|---|---|---|
| **Hot loop** | 100 ms – 1 s | tick ingest, feature update, signal evaluation, risk check, order send, stop/trail management, reconciliation, circuit breakers | **No** |
| **Warm loop** | 1 – 15 min | regime classification, news/event extraction, strategy enable/disable proposals, parameter proposals, anomaly investigation | Yes |
| **Cold loop** | hourly – daily | research, hypothesis generation, backtesting, post-trade attribution, agent scoring, strategy review | Yes |

Warm- and cold-loop outputs are **configuration deltas**, submitted to the control plane, validated against the risk constitution, and applied to the hot loop atomically. They are never order instructions.

### 2.3 Repository layout

```
trading-house/
├── pyproject.toml
├── config/
│   ├── risk_constitution.yaml          # §1.4, signed
│   ├── risk_constitution.yaml.sig
│   ├── brokers.yaml                    # MT5 terminals, accounts, magic ranges
│   ├── symbols.yaml                    # per-symbol overrides & eligibility
│   └── strategies/                     # one file per registered strategy version
├── core/
│   ├── schemas.py                      # §4 — Pydantic contracts, single source of truth
│   ├── clock.py                        # UTC discipline, server-time offset
│   ├── ids.py                          # intent UUIDs, magic-number allocation
│   └── errors.py
├── brokers/
│   ├── base.py                         # BrokerAdapter ABC
│   ├── mt5/
│   │   ├── gateway.py                  # SINGLE-THREADED actor, owns all mt5.* calls
│   │   ├── symbols.py                  # SymbolContract discovery & caching
│   │   ├── orders.py                   # request builders, retcode taxonomy, retry policy
│   │   ├── reconcile.py                # positions/deals reconciliation
│   │   └── mappings.py                 # enums, timeframes, filling modes
│   └── ccxt_spot/                      # OPTIONAL adapter for spot crypto (§3.9)
├── marketdata/
│   ├── ingest.py                       # tick & bar ingestion from gateway
│   ├── store.py                        # append-only parquet / QuestDB writer
│   └── quality.py                      # staleness, gaps, crossed quotes, outliers
├── features/
│   ├── engine.py                       # deterministic, point-in-time safe
│   └── indicators/                     # ATR, imbalance, VWAP, etc. — pure functions
├── strategies/
│   ├── base.py                         # Strategy ABC — §10.2
│   ├── registry.py
│   └── impl/                           # one module per strategy
├── portfolio/
│   ├── allocator.py                    # target exposures from expected edge + correlation
│   └── correlation.py                  # cluster definitions & live correlation
├── risk/
│   ├── engine.py                       # pre-trade + portfolio + session checks (§7)
│   ├── sizing.py                       # §8 — the ONLY place lot size is computed
│   ├── limits.py                       # loaded from constitution, immutable at runtime
│   └── circuit_breakers.py
├── execution/
│   ├── order_manager.py                # idempotent intent ledger, §3.6
│   ├── position_guard.py               # state machine, stops, trailing, §9
│   └── tca.py                          # slippage & implementation shortfall
├── agents/
│   ├── graph.py                        # LangGraph assembly
│   ├── roles/                          # one module per agent role, §5
│   ├── tools.py                        # permissioned tool registry
│   └── memory.py                       # structured observation/outcome store
├── research/
│   ├── backtest/                       # event-driven simulator, §11
│   ├── validation/                     # DSR, PBO, CPCV, walk-forward, bootstrap
│   ├── trial_ledger.py                 # immutable record of every trial
│   └── promotion.py                    # gate evaluation, §12
├── ops/
│   ├── watchdog.py
│   ├── safe_mode.py
│   ├── metrics.py
│   └── audit.py                        # append-only, hash-chained
└── tests/
    ├── unit/
    ├── integration/
    ├── property/                       # hypothesis-based invariant tests
    └── acceptance/                     # §15 phase gates
```

### 2.4 Deployment topology

Because the `MetaTrader5` Python package requires a Windows MT5 terminal (§3.1), there are two supported topologies. Choose one and record it in `config/brokers.yaml`.

**Topology A — All-Windows (recommended for first build).**
Windows VPS near the broker's server region. MT5 terminal + Python + full stack on one host. Simplest, fewest failure modes, lowest latency to the terminal.

**Topology B — Linux control plane + Windows execution edge.**
Linux host runs research, agents, storage, observability. Windows VPS runs MT5 terminal plus a thin gateway service exposing the MT5 surface over an authenticated RPC channel (`pymt5linux` / `mt5linux` use `rpyc` over Wine; a hand-rolled gRPC or ZeroMQ gateway is preferable for production because you control the timeout and reconnection semantics). Note that `pymt5linux` documents a Wine-based path (Wine + Windows Python + MT5 terminal inside Wine, client connecting to `MetaTrader5(host, port)`), and that Docker images running MT5 under x11/noVNC exist. **Treat Wine as a convenience for research, not as a production execution substrate**: an additional emulation layer between the strategy and the money is a failure mode you do not need.

Multi-account (e.g. one live account plus one prop-firm challenge account) requires **one MT5 terminal installation per account**, each launched in portable mode from its own directory, each with its own gateway process. One terminal serves exactly one account at a time.

---
## 3. The MT5 integration layer

Neither source report specified MT5. This section is the primary new contribution and the densest part of the build. Most production incidents in MT5 systems originate here.

### 3.1 Platform realities the coding agent must design around

| Reality | Consequence for the design |
|---|---|
| The official `MetaTrader5` package talks to a **running Windows MT5 terminal** via local IPC. There is no headless server mode. | The terminal is a stateful dependency with a GUI. It must be supervised, auto-restarted, and health-checked. |
| One terminal ⇒ one account. | Multi-account requires multiple portable-mode terminal installs + multiple gateway processes. |
| The API is **synchronous and not thread-safe**. Concurrent calls from multiple threads produce undefined behaviour. | **Invariant I-5**: one process, one thread, all `mt5.*` calls behind a queue (`MT5Gateway` actor). Every other component talks to it via async request/response. |
| There is **no native trailing stop** in the Python API. | Trailing must be implemented client-side by repeatedly issuing `TRADE_ACTION_SLTP` (§9). This means trailing stops **stop working if your process dies** — the broker only knows the last SL you set. |
| There is **no client order ID**. `order_send` has `magic` (ulong) and `comment` (short, broker may truncate or overwrite). | Idempotency must be achieved by intent ledger + reconciliation, never by resend (§3.6). |
| `order_send` returning `None` means the request never reached the server *or* the response was lost. It does **not** mean the order did not execute. | Timeout ⇒ reconcile, do not retry. |
| Broker rulebooks differ per symbol: filling modes, stops level, freeze level, volume step, execution mode, trading sessions. | Never hardcode. Discover at startup and cache as a `SymbolContract` (§3.3). |
| `AutoTrading` must be enabled in the terminal (Ctrl+E) and in Tools → Options → Expert Advisors, or every send fails with retcode 10027. | Startup preflight must assert this and refuse to start otherwise. |

### 3.2 Gateway actor — the only component that touches MT5

```python
# brokers/mt5/gateway.py  (skeleton — the coding agent expands this)
import queue, threading, time
from dataclasses import dataclass
from typing import Any, Callable
import MetaTrader5 as mt5

@dataclass
class _Job:
    fn: Callable[..., Any]
    args: tuple
    kwargs: dict
    result: "queue.Queue"

class MT5Gateway:
    """Owns the MT5 connection. Single thread. All mt5.* calls go through submit()."""

    def __init__(self, cfg):
        self.cfg = cfg
        self._q: "queue.Queue[_Job]" = queue.Queue(maxsize=1000)
        self._thread = threading.Thread(target=self._run, daemon=True, name="mt5-gateway")
        self._alive = threading.Event()
        self._last_ok = 0.0

    # ---- public, thread-safe -------------------------------------------------
    def submit(self, fn, *args, timeout=10.0, **kwargs):
        r: "queue.Queue" = queue.Queue(maxsize=1)
        self._q.put(_Job(fn, args, kwargs, r), timeout=timeout)
        ok, payload = r.get(timeout=timeout)
        if not ok:
            raise payload
        return payload

    def healthy(self) -> bool:
        return self._alive.is_set() and (time.time() - self._last_ok) < 5.0

    # ---- internals -----------------------------------------------------------
    def _connect(self):
        if not mt5.initialize(
            path=self.cfg.terminal_path,
            login=self.cfg.login,
            password=self.cfg.password,
            server=self.cfg.server,
            timeout=60_000,
            portable=self.cfg.portable,
        ):
            raise ConnectionError(f"mt5.initialize failed: {mt5.last_error()}")
        ti = mt5.terminal_info()
        if ti is None or not ti.trade_allowed:
            raise ConnectionError("AutoTrading disabled in terminal (Ctrl+E / Options)")
        ai = mt5.account_info()
        if ai is None:
            raise ConnectionError(f"account_info failed: {mt5.last_error()}")
        if ai.login != self.cfg.login:
            raise ConnectionError("terminal is logged into a different account")
        self._alive.set()

    def _run(self):
        backoff = 1.0
        while True:
            try:
                self._connect()
                backoff = 1.0
                while True:
                    job = self._q.get()
                    try:
                        out = job.fn(*job.args, **job.kwargs)
                        self._last_ok = time.time()
                        job.result.put((True, out))
                    except Exception as e:                    # noqa: BLE001
                        job.result.put((False, e))
                        if not self._ping():
                            raise                            # force reconnect
            except Exception:                                 # noqa: BLE001
                self._alive.clear()
                mt5.shutdown()
                time.sleep(min(backoff, 30.0))
                backoff *= 2

    def _ping(self) -> bool:
        return mt5.terminal_info() is not None
```

**Rules the coding agent must not violate:**
- `mt5.initialize()` and `mt5.shutdown()` are called only inside `_run`.
- No other module imports `MetaTrader5`. Enforce with a lint rule / import test.
- When `healthy()` is `False`, the system enters `SAFE_MODE` (§13.2): no new orders, existing broker-side stops are relied upon, alert fires.

### 3.3 Symbol contract discovery

Every symbol must be resolved into an immutable `SymbolContract` at startup and re-checked hourly. Never assume values.

```python
# brokers/mt5/symbols.py
from dataclasses import dataclass
import MetaTrader5 as mt5

SYMBOL_FILLING_FOK = 1
SYMBOL_FILLING_IOC = 2
SYMBOL_FILLING_BOC = 4          # newer builds; treat as unsupported for market orders

@dataclass(frozen=True)
class SymbolContract:
    name: str
    digits: int
    point: float
    tick_size: float             # trade_tick_size
    tick_value_loss: float       # trade_tick_value_loss  <-- use THIS for sizing
    tick_value_profit: float
    contract_size: float
    volume_min: float
    volume_max: float
    volume_step: float
    stops_level_points: int      # trade_stops_level
    freeze_level_points: int     # trade_freeze_level
    filling_mask: int            # filling_mode bitmask
    exec_mode: int               # trade_exemode: 0=REQUEST 1=INSTANT 2=MARKET 3=EXCHANGE
    trade_mode: int              # 0=DISABLED 1=LONGONLY 2=SHORTONLY 3=CLOSEONLY 4=FULL
    swap_long: float
    swap_short: float
    currency_profit: str

def load_contract(symbol: str) -> SymbolContract:
    if not mt5.symbol_select(symbol, True):
        raise ValueError(f"cannot select {symbol}: {mt5.last_error()}")
    si = mt5.symbol_info(symbol)
    if si is None:
        raise ValueError(f"symbol_info None for {symbol}: {mt5.last_error()}")
    if si.trade_mode == 0:
        raise ValueError(f"{symbol} trade_mode DISABLED at this broker")
    return SymbolContract(
        name=si.name, digits=si.digits, point=si.point,
        tick_size=si.trade_tick_size,
        tick_value_loss=si.trade_tick_value_loss,
        tick_value_profit=si.trade_tick_value_profit,
        contract_size=si.trade_contract_size,
        volume_min=si.volume_min, volume_max=si.volume_max, volume_step=si.volume_step,
        stops_level_points=si.trade_stops_level,
        freeze_level_points=si.trade_freeze_level,
        filling_mask=si.filling_mode,
        exec_mode=si.trade_exemode,
        trade_mode=si.trade_mode,
        swap_long=si.swap_long, swap_short=si.swap_short,
        currency_profit=si.currency_profit,
    )
```

**Filling mode selection.** `filling_mode` is a bitmask combined with OR; test with AND. MQL5 documents that if no filling type is specified, `ORDER_FILLING_RETURN` is set automatically, and that under Request/Instant execution FOK is always used for market orders while under Market/Exchange execution Return is always allowed. The practical, broker-agnostic algorithm — and the one to implement, because a hardcoded FOK against an IOC-only symbol produces retcode 10030 and *zero trades with no other symptom*:

```python
def filling_candidates(c: SymbolContract) -> list[int]:
    order = []
    if c.filling_mask & SYMBOL_FILLING_FOK:
        order.append(mt5.ORDER_FILLING_FOK)
    if c.filling_mask & SYMBOL_FILLING_IOC:
        order.append(mt5.ORDER_FILLING_IOC)
    if c.exec_mode in (mt5.SYMBOL_TRADE_EXECUTION_MARKET,
                       mt5.SYMBOL_TRADE_EXECUTION_EXCHANGE) or not order:
        order.append(mt5.ORDER_FILLING_RETURN)
    return order
```
On retcode 10030 (`INVALID_FILL`), advance to the next candidate and cache the working mode per symbol. Persist it; do not rediscover on every order.

**Stops level and freeze level.**
- `stops_level_points` = minimum distance in points between price and SL/TP. Violating it returns 10016 (`INVALID_STOPS`). A gold scalper with a 40-point stop on a broker whose XAUUSD stops level is 70 points simply never trades.
- `freeze_level_points` = distance within which an existing order/position **cannot be modified or closed**. Trailing logic must check this before every `TRADE_ACTION_SLTP`, or modifications silently fail near the money.

```python
def min_stop_distance(c: SymbolContract, spread_points: float, buffer_mult: float = 1.5) -> float:
    """Returns minimum SL distance in PRICE units."""
    pts = max(c.stops_level_points, c.freeze_level_points, spread_points * buffer_mult, 1)
    return pts * c.point
```

**Rounding.** Two independent roundings, both mandatory:
```python
def round_price(p: float, c: SymbolContract) -> float:
    return round(round(p / c.tick_size) * c.tick_size, c.digits)

def round_volume(v: float, c: SymbolContract) -> float:
    steps = round(v / c.volume_step)
    v = steps * c.volume_step
    v = min(max(v, c.volume_min), c.volume_max)
    return round(v, 8)
```
SL/TP must be sent as **floats**; integers cause `order_send` to fail or return `None` — a documented and frequently-hit trap.

### 3.4 Account mode: netting vs hedging

```python
ai = mt5.account_info()
# ai.margin_mode: 0 = RETAIL_NETTING, 1 = EXCHANGE, 2 = RETAIL_HEDGING
```

This changes the position model fundamentally and must be detected, not assumed:

| Mode | Behaviour | Design consequence |
|---|---|---|
| **Netting** (many EU/regulated brokers, exchange accounts) | One position per symbol. A buy against an existing sell reduces/reverses it. | Two strategies trading the same symbol **share one position and one SL**. You must either (a) restrict one strategy per symbol, or (b) implement a virtual-position layer that nets internally and manages a single broker SL at the *tightest* strategy requirement. Option (a) is strongly recommended for v1. |
| **Hedging** (most retail FX brokers) | Multiple independent positions per symbol, each with its own ticket, SL, TP, magic. | Clean per-strategy isolation. **Preferred for this system.** Select a hedging account where possible. |

The coding agent must implement `PositionModel` as an interface with `NettingPositionModel` and `HedgingPositionModel`, and fail loudly at startup if a strategy configuration is incompatible with the detected mode.

### 3.5 Order request construction

```python
# brokers/mt5/orders.py
def build_market_order(c: SymbolContract, side: str, volume: float, sl: float, tp: float | None,
                       magic: int, deviation_points: int, filling: int, comment: str) -> dict:
    tick = mt5.symbol_info_tick(c.name)
    price = tick.ask if side == "BUY" else tick.bid
    req = {
        "action":       mt5.TRADE_ACTION_DEAL,
        "symbol":       c.name,
        "volume":       round_volume(volume, c),
        "type":         mt5.ORDER_TYPE_BUY if side == "BUY" else mt5.ORDER_TYPE_SELL,
        "price":        round_price(price, c),
        "sl":           float(round_price(sl, c)),           # MUST be float
        "deviation":    int(deviation_points),
        "magic":        int(magic),
        "comment":      comment[:24],                        # brokers truncate ~31 chars
        "type_time":    mt5.ORDER_TIME_GTC,
        "type_filling": filling,
    }
    if tp is not None:
        req["tp"] = float(round_price(tp, c))
    return req
```

**Always call `mt5.order_check(request)` before `order_send`.** It returns an `MqlTradeCheckResult` with the same retcode taxonomy plus `margin_free`, `margin_level`, and `balance` after the hypothetical trade. This catches 10014/10016/10019/10030 without touching the market. The risk engine consumes `order_check` output as its final pre-trade validation.

### 3.6 Idempotency and the intent ledger — the hardest MT5 problem

MT5 has no client order ID, so **retrying a timed-out `order_send` can double your position.** Invariant I-6 exists for this reason.

Protocol:

```
1. Allocate intent_id = uuid4()  and  magic = strategy_magic_base + strategy_seq
2. WRITE intent{intent_id, symbol, side, volume, sl, magic, t_submit_utc, state=SUBMITTING}
   to a durable store (SQLite/Postgres, fsync'd) BEFORE calling order_send
3. result = gateway.submit(mt5.order_send, request)
4a. result.retcode == 10009 (DONE) or 10008 (PLACED):
       WRITE state=CONFIRMED, order_ticket=result.order, deal=result.deal, price=result.price
4b. result is None, or an exception/timeout occurred:
       WRITE state=UNKNOWN
       DO NOT RESEND.
       Enter RECONCILING: poll mt5.history_deals_get(from=t_submit-60s, to=now)
       and mt5.positions_get(symbol=...) filtered by magic.
       - deal found with matching magic/symbol/volume in window -> state=CONFIRMED (adopt ticket)
       - nothing found after N polls over 30s and terminal healthy -> state=FAILED
       - terminal unhealthy -> stay UNKNOWN, enter SAFE_MODE, alert human
4c. Rejection retcodes: classify per §3.7 and act accordingly
5. On process restart: every intent in SUBMITTING/UNKNOWN state is reconciled BEFORE
   any new order is permitted. This is a hard startup gate.
```

Magic-number allocation scheme (put in `core/ids.py`):
```
magic = BOOK_BASE + STRATEGY_ID * 100 + INSTANCE
  core book   BOOK_BASE = 10_000_000
  sleeve book BOOK_BASE = 20_000_000
```
This makes book membership, strategy identity and instance recoverable from any position the broker reports — essential for reconciliation and for the risk engine to attribute exposure after a restart.

### 3.7 Retcode taxonomy and response policy

Implement as a dict, not a chain of `if`s. Codes below are the MQL5 trade server return codes.

| Retcode | Name | Class | Action |
|---|---|---|---|
| 10009 | DONE | success | confirm |
| 10008 | PLACED | success | confirm (pending) |
| 10010 | DONE_PARTIAL | partial | adopt filled volume, cancel remainder, re-evaluate risk |
| 10004 | REQUOTE | transient | refresh tick, re-price, retry ≤2× with fresh intent state, then abort |
| 10020 | PRICE_CHANGED | transient | as REQUOTE |
| 10021 | PRICE_OFF | transient | no quotes — wait for fresh tick, do not retry blindly |
| 10006 | REJECT | broker | abort, log, count toward `max_consecutive_rejects` |
| 10013 | INVALID_REQUEST | bug | abort, alert — this is a code defect |
| 10014 | INVALID_VOLUME | bug/config | abort, re-run `round_volume`, alert |
| 10015 | INVALID_PRICE | bug/config | abort, re-run `round_price`, alert |
| 10016 | INVALID_STOPS | config | recompute against `stops_level`/`freeze_level`, retry once, then abort |
| 10017 | TRADE_DISABLED | broker/account | halt symbol, alert |
| 10018 | MARKET_CLOSED | session | halt symbol until session open |
| 10019 | NO_MONEY | risk | abort, force risk recheck, reduce exposure, alert |
| 10022/10023 | INVALID_EXPIRATION | bug | abort |
| 10027 | AUTOTRADING_DISABLED (client) | ops | SAFE_MODE, alert immediately |
| 10030 | INVALID_FILL | config | advance filling candidate (§3.3), retry once |
| 10031 | CONNECTION | transient | SAFE_MODE, reconnect, reconcile |
| 10033/10034 | LIMIT_ORDERS / LIMIT_VOLUME | risk | abort, reduce |

`order_send` returning `True`/a result object means the request was **accepted**, not that the deal executed. Always verify `result.retcode == 10009` and then confirm via deal history.

### 3.8 Market data from MT5

```python
rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M1, 0, 5000)   # newest-first index 0
ticks = mt5.copy_ticks_range(symbol, t_from, t_to, mt5.COPY_TICKS_ALL)
tick  = mt5.symbol_info_tick(symbol)     # .time, .bid, .ask, .last, .volume, .time_msc, .flags
```

Mandatory handling:
- **Bar 0 is incomplete.** Signal computation must use closed bars only (`rates[1:]` when using `copy_rates_from_pos`), otherwise every backtest silently look-ahead-leaks.
- **Server time is not UTC.** MT5 returns broker-server epoch seconds. Compute a `server_utc_offset` at startup by comparing `symbol_info_tick().time` against a trusted UTC source, store it, re-verify hourly, and convert everything to UTC at the boundary (Invariant I-10). Never do arithmetic on raw MT5 timestamps.
- **`time_msc` is the millisecond timestamp** — use it for microstructure work; `time` is second-resolution and will collapse distinct ticks.
- **Tick flags** (`TICK_FLAG_BID/ASK/LAST/VOLUME/BUY/SELL`) tell you what actually changed. A "tick" with only a volume flag is not a price update.
- **Weekend/holiday gaps** are normal; the staleness detector must be session-aware or it will fire every Saturday.
- MT5 history depth is broker-dependent and the terminal downloads lazily. Warm up history at startup and assert the returned array length before use.

### 3.9 Covering all three asset classes

The user requires FX, crypto and equities. Consolidated recommendation:

| Asset class | Primary route | Notes |
|---|---|---|
| FX majors/minors | **MT5** | Native fit. Watch swap (`swap_long`/`swap_short`) on multi-day swing holds. |
| Indices, equities | **MT5 CFDs** | Available at most MT5 brokers. Not exchange-traded shares: financing costs, dividend adjustments, broker-set stops levels, and wider out-of-session spreads apply. |
| Crypto | **MT5 CFDs** for v1; optional `ccxt` spot/perp adapter later | MT5 crypto CFDs carry overnight financing and often large `stops_level`. True spot/perp strategies (funding-rate basis, cross-exchange) are **not** implementable through MT5 and need the separate adapter. |

Build `BrokerAdapter` as an ABC from day one so the second adapter is additive, but **do not build the second adapter until Phase 6** (§16). One broker, one asset class, is the correct starting scope.

---
## 4. Canonical schemas

Both source reports insist that inter-agent messages be schema-bound, with free-form narrative permitted only *alongside* numeric fields, never instead of them. Implement in `core/schemas.py` with Pydantic v2. Every agent, strategy and engine imports from here; no ad-hoc dicts cross a module boundary.

```python
from __future__ import annotations
from datetime import datetime
from enum import Enum
from typing import Literal
from pydantic import BaseModel, Field, field_validator

class Book(str, Enum):
    CORE = "core"
    SLEEVE = "sleeve"

class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"

class Stamped(BaseModel):
    event_time: datetime        # when the market event occurred (UTC)
    availability_time: datetime # when the system could FIRST have received it (UTC)
    processing_time: datetime   # when the system ingested it (UTC)
    source: str
    revision_id: str | None = None
    quality_flags: list[str] = Field(default_factory=list)

class RegimeAssessment(Stamped):
    symbol: str
    volatility_state: Literal["low", "normal", "high", "extreme"]
    trend_state: Literal["down", "range", "up"]
    liquidity_state: Literal["thin", "normal", "deep"]
    probabilities: dict[str, float]
    uncertainty: float = Field(ge=0, le=1)

class TradeProposal(Stamped):
    """The ONLY structure a strategy or agent may submit toward execution."""
    proposal_id: str
    strategy_id: str
    strategy_version: str
    book: Book
    symbol: str
    side: Side
    horizon_seconds: int
    entry_condition: str                     # human-readable, for audit
    entry_price_ref: float                   # price at which the signal was evaluated
    invalidation_price: float                # structural stop level — WHERE the thesis dies
    max_holding_seconds: int
    expected_return_bps: float
    expected_return_stdev_bps: float
    expected_cost_bps: float                 # spread + commission + expected slippage + swap
    win_probability: float = Field(ge=0, le=1)   # MUST be calibrated, not verbal confidence
    calibration_id: str                      # reference to the reliability curve used
    required_liquidity_lots: float
    regime_ref: str
    features_snapshot_id: str
    rationale: str | None = None             # narrative, NOT used by risk or sizing

    @field_validator("win_probability")
    @classmethod
    def _no_verbal_confidence(cls, v: float) -> float:
        if v in (0.0, 1.0):
            raise ValueError("degenerate probability rejected")
        return v

class RiskDecision(BaseModel):
    proposal_id: str
    verdict: Literal["APPROVED", "RESIZED", "REJECTED"]
    approved_volume_lots: float
    stop_loss_price: float
    take_profit_price: float | None
    risk_money: float
    risk_pct_of_book: float
    reasons: list[str]                       # every check that fired
    checks_passed: list[str]
    constitution_version: int

class OrderIntent(BaseModel):
    intent_id: str
    proposal_id: str
    magic: int
    symbol: str
    side: Side
    volume: float
    sl: float
    tp: float | None
    deviation_points: int
    filling: int
    state: Literal["SUBMITTING", "CONFIRMED", "UNKNOWN", "RECONCILING", "FAILED", "REJECTED"]
    t_submit_utc: datetime
    broker_order_ticket: int | None = None
    broker_position_ticket: int | None = None
    fill_price: float | None = None
    retcode: int | None = None

class PositionState(BaseModel):
    position_ticket: int
    intent_id: str | None
    strategy_id: str
    book: Book
    symbol: str
    side: Side
    volume: float
    open_price: float
    current_sl: float
    current_tp: float | None
    opened_at_utc: datetime
    lifecycle: Literal["OPEN_PROTECTED", "BREAKEVEN_ELIGIBLE", "TRAILING",
                       "EXIT_PENDING", "CLOSED"]
    r_multiple_open: float                   # (price - open) / initial_risk_distance
    mae_r: float                             # maximum adverse excursion, in R
    mfe_r: float                             # maximum favourable excursion, in R
    initial_risk_distance: float             # price units — frozen at entry, never changes

class AgentOpinion(Stamped):
    agent_role: str
    subject_id: str                          # proposal_id or symbol
    stance: Literal["FOR", "AGAINST", "ABSTAIN"]
    evidence_for: list[str]
    evidence_against: list[str]
    missing_information: list[str]
    confidence: float = Field(ge=0, le=1)    # advisory only; NEVER a size multiplier
```

**Rule for the coding agent:** `RiskDecision.approved_volume_lots` is the only quantity the order manager may use. `TradeProposal` contains no volume field by design — a strategy proposes *what and where*, the risk engine decides *how much*.

---

## 5. Agent roster and authority matrix

Consolidated from Report A's trading-desk mapping and Report B's more granular desk model. Duplicates merged; authority made explicit and machine-enforceable.

| Agent / service | Loop | Responsibility | Authority | Tools permitted |
|---|---|---|---|---|
| **Chief investment officer** | warm | Session coordination, strategy-family selection, conflict resolution | Recommends portfolio targets; **cannot bypass risk** | read: features, regimes, opinions, performance |
| **Market-data steward** | hot | Validate quotes, bars, sessions, gaps, crossed quotes, corporate actions | **May quarantine a symbol or feed** | read: raw feed; write: quarantine flag |
| **Regime detector** | warm | Classify volatility / trend / liquidity / correlation | **May enable/disable strategy classes** (within registry-declared eligibility) | read: features; write: regime state |
| **Macro & fundamental analyst** | cold | Policy, releases, filings, valuation | research only | web/RAG read |
| **Technical analyst** | warm | Trend, breakout, volatility, S/R, momentum features | research only | read: features |
| **Microstructure analyst** | warm | Spread, imbalance, flow, short-horizon liquidity | research only | read: ticks |
| **News & sentiment analyst** | warm | Event type, surprise, entity, polarity, novelty → **schema-bound**, never free-form sizes | research only | news API read |
| **Strategy research agent** | cold | Generate testable hypotheses + specs | submits specs only | backtest sandbox (no live creds) |
| **Bull / bear challengers** | warm | Opposing interpretations of each proposal | research only | read all research |
| **Strategy allocator** | warm | Weight approved strategies by net edge, uncertainty, correlation | recommends target exposures | read: performance, correlation |
| **Independent risk officer** | hot | Enforce every limit in the constitution | **ABSOLUTE VETO. Deterministic code, not an LLM.** | risk engine only |
| **Execution agent** | hot | Convert approved targets into MT5 requests | may submit **only** risk-approved orders | gateway (write) |
| **Trade-monitoring / position guard** | hot | Fills, exposure, stops, trailing, spread, state machine | **may tighten risk; may never loosen a hard limit** | gateway (SLTP/close only) |
| **Post-trade analyst** | cold | Attribute P&L to signal, sizing, timing, slippage, market | no trading authority | read: deals, features |
| **Validation / red team** | cold | Hunt leakage, overfitting, hidden leverage, unrealistic assumptions | **may block promotion** | research sandbox |
| **Compliance & audit** | hot | Instrument eligibility, jurisdiction, recordkeeping | **ABSOLUTE VETO where rules are violated** | audit ledger (append) |
| **SRE / security service** | hot | Latency, clock drift, connectivity, credentials, terminal health | **may cancel orders, flatten, halt** | gateway, safe mode |

### 5.1 Two failure modes to design against explicitly

Report B raises a point Report A does not, and it is important: **more agents do not automatically create independent expertise.** Agents sharing a base model, data and prompt style produce *correlated* errors and can reinforce each other's biases. Therefore:

1. **Measure disagreement.** Log pairwise agreement rates between agents. An agent whose opinions are >90% correlated with another's is providing no information and should be pruned or re-prompted with a different evidence diet.
2. **Do not decide by majority vote.** The CIO agent must weight by *regime-conditioned historical reliability*, not headcount. A minority agent that has been reliable in high-volatility conditions must not be overruled merely because the majority agrees with itself. (Consensus mechanisms can suppress minority-but-correct evidence in fast-moving markets.)
3. **Score agents on realised outcomes.** Adopt the competitive pattern: agents are scored after outcomes are observable, and influence is reallocated toward agents with positive *predicted incremental* usefulness. Do not permanently trust an agent because it was once right.
4. **Run agent ablation.** For every agent, periodically measure system performance with it removed. An agent that does not improve net, out-of-sample results is deleted.

### 5.2 Tool permission model

```python
# agents/tools.py
ROLE_TOOLS = {
    "risk_officer":       [],                        # deterministic code, no LLM tools
    "research":           ["backtest_run", "data_read", "web_search"],
    "news_analyst":       ["news_read", "data_read"],
    "cio":                ["data_read", "performance_read"],
    "execution":          [],                        # deterministic
}
FORBIDDEN_FOR_ALL_LLM_ROLES = [
    "gateway_order_send", "gateway_sltp", "risk_limit_write",
    "constitution_write", "kill_switch_disable", "shell_exec",
]
```
An attempted call to a forbidden tool is a **circuit-breaker trigger** (§13.2), not a logged warning. It indicates either a prompt injection or a bug, and both warrant halting.

---
## 6. Orchestration and the decision flow

Consolidated flow (Report B's sequence, with Report A's calibration and cost-model steps inserted, and MT5 realities added):

```
Point-in-time market snapshot (MT5 gateway, closed bars only)
        ↓
Data validation · staleness · session · instrument eligibility        [steward, hot]
        ↓
Regime classification                                                 [warm]
        ↓
Parallel specialist analysis (technical · micro · macro · news)        [warm]
        ↓
Bull ↔ bear ↔ independent critic                                       [warm]
        ↓
Approved-strategy signal engines  ── deterministic, hot ──►  TradeProposal
        ↓
Probability calibration + expected-cost model (MT5 spread/swap/commission)
        ↓
Portfolio construction (target exposures, correlation budget)
        ↓
INDEPENDENT RISK VETO + RESIZING  ──►  RiskDecision                    [hot, deterministic]
        ↓
mt5.order_check()  ──►  pre-trade rejection without touching the market
        ↓
Deterministic execution engine (intent ledger → order_send)            [hot]
        ↓
Fill reconciliation (positions_get / history_deals_get by magic)
        ↓
Position guard: protection · breakeven · trailing · exits · breakers   [hot]
        ↓
Post-trade attribution + agent scoring                                 [cold]
        ↓
Research memory + strategy review                                      [cold]
```

Implement the warm/cold portion in LangGraph (checkpointing and conditional edges map cleanly onto debate and escalation). Implement the hot portion as plain Python with an explicit event loop — **no agent framework in the hot path**. The boundary between them is a single validated `StrategyConfigDelta` message applied atomically.

---

## 7. The risk engine

Deterministic. No LLM. This module is the reason the system survives.

### 7.1 Control hierarchy (merged from both reports)

| Level | Checks |
|---|---|
| Instrument | max order size, min liquidity, spread ceiling, `trade_mode` allows the side, session open, event blackout window |
| Trade | max monetary loss, **compulsory protective stop**, approved order type, time-to-live, stop respects `stops_level`/`freeze_level` |
| Strategy | capital allocation, turnover cap, live drawdown, realised-slippage budget, regime eligibility |
| Asset class | FX / crypto / equity exposure caps |
| Correlation cluster | USD risk, tech beta, crypto beta, index beta |
| Book | gross exposure, net exposure, leverage, VaR, expected shortfall, stress loss |
| Session | daily loss stop, max failed orders, max operational incidents |
| Firm | max drawdown, counterparty concentration, total capital at risk |
| Infrastructure | data freshness, clock drift, terminal connectivity, reconciliation status |

### 7.2 Engine contract

```python
# risk/engine.py
class RiskEngine:
    def __init__(self, constitution: Constitution, state: RiskState, clock: Clock): ...

    def evaluate(self, proposal: TradeProposal, contract: SymbolContract,
                 tick: Tick, book_state: BookState) -> RiskDecision:
        reasons, passed = [], []

        # --- ordered, fail-fast; every check appends to reasons on failure -------
        self._c(infra_healthy,           reasons, passed)
        self._c(not self.state.safe_mode, reasons, passed)
        self._c(session_open,            reasons, passed)
        self._c(symbol_not_quarantined,  reasons, passed)
        self._c(trade_mode_allows_side,  reasons, passed)
        self._c(spread_within_ceiling,   reasons, passed)
        self._c(tick_fresh,              reasons, passed)
        self._c(not_in_event_blackout,   reasons, passed)
        self._c(strategy_enabled_in_regime, reasons, passed)
        self._c(book_daily_loss_ok,      reasons, passed)
        self._c(book_drawdown_ok,        reasons, passed)
        self._c(firm_drawdown_ok,        reasons, passed)
        self._c(position_count_ok,       reasons, passed)
        self._c(symbol_risk_budget_ok,   reasons, passed)
        self._c(cluster_risk_budget_ok,  reasons, passed)
        self._c(leverage_ok,             reasons, passed)
        self._c(order_rate_ok,           reasons, passed)
        self._c(not_martingale,          reasons, passed)   # I-8
        if reasons:
            return RiskDecision(verdict="REJECTED", ..., reasons=reasons)

        stop = compute_stop(proposal, contract, tick)        # §8.2
        vol  = compute_volume(stop, proposal, contract, book_state, self.constitution)  # §8.1
        if vol < contract.volume_min:
            return RiskDecision(verdict="REJECTED", reasons=["below_min_lot"])

        vol, resized = self._apply_caps(vol, ...)            # may shrink, never grow
        chk = gateway.submit(mt5.order_check, build_request(...))
        if chk is None or chk.retcode != mt5.TRADE_RETCODE_DONE:
            return RiskDecision(verdict="REJECTED", reasons=[f"order_check:{chk.retcode}"])

        return RiskDecision(verdict="RESIZED" if resized else "APPROVED",
                            approved_volume_lots=vol, stop_loss_price=stop, ...)
```

### 7.3 Structural prohibitions (Invariant I-8)

These must be **impossible**, not policed:

```python
def not_martingale(proposal, book_state) -> bool:
    """Reject any proposal whose size or risk would exceed the prior loser's."""
    last = book_state.last_closed_trade(proposal.strategy_id, proposal.symbol)
    if last and last.pnl < 0:
        return proposal_risk_money <= last.risk_money   # never scale up after a loss
    return True
```
- `stop_widening`: the position guard exposes only `tighten_stop()`. There is no `set_stop()` public method after entry. A stop may move toward profit; the API makes the reverse unrepresentable.
- `stop_removal`: `TRADE_ACTION_SLTP` with `sl=0.0` is rejected at the order-manager layer before it reaches the gateway.
- `trading_without_protective_stop`: `build_market_order` requires a non-`None` `sl` argument; there is no overload without it.

---

## 8. Position sizing and stop placement — MT5 arithmetic

This is where generic blueprints go wrong. MT5 sizing must use **tick value**, not pip heuristics, because tick value already accounts for contract size and the account-currency conversion.

### 8.1 Volume from risk budget

```python
# risk/sizing.py
def compute_volume(stop_distance_price: float, book_equity: float,
                   risk_pct: float, c: SymbolContract) -> float:
    """
    risk_money        = book_equity * risk_pct / 100
    ticks_at_risk     = stop_distance_price / c.tick_size
    money_per_lot     = ticks_at_risk * c.tick_value_loss
    volume            = risk_money / money_per_lot
    """
    if stop_distance_price <= 0:
        raise ValueError("stop distance must be positive")
    risk_money = book_equity * (risk_pct / 100.0)
    ticks = stop_distance_price / c.tick_size
    money_per_lot = ticks * c.tick_value_loss
    if money_per_lot <= 0:
        raise ValueError("non-positive tick value; symbol contract invalid")
    return round_volume(risk_money / money_per_lot, c)
```

Use `trade_tick_value_loss` (not `trade_tick_value`): for symbols where profit and loss tick values differ, the loss value is the one that governs your worst case.

**Always verify margin before sending:**
```python
margin = gateway.submit(mt5.order_calc_margin, order_type, symbol, volume, price)
if margin is None or margin > account.margin_free * 0.5:
    reject("insufficient_free_margin_headroom")
```
Requiring 2× headroom on free margin prevents a single adverse move from cascading into forced liquidation across the book.

### 8.2 Stop distance

Consolidated formula (Report B's max-of-three, with MT5 broker constraints added):

```python
def compute_stop_distance(atr: float, spread_price: float, structural: float,
                          c: SymbolContract, k_sigma: float, k_spread: float) -> float:
    d = max(
        k_sigma * atr,                    # volatility term
        k_spread * spread_price,          # cost term — must clear the spread comfortably
        structural,                        # thesis-invalidation term
        min_stop_distance(c, spread_price / c.point),   # BROKER floor (stops/freeze level)
    )
    return d
```

ATR multipliers by horizon (empirical guidance from Report A; validate per symbol, do not adopt blindly):

| Horizon | `k_sigma` (× ATR) | Note |
|---|---|---|
| Scalp (sec–min) | 1.0–1.5 | Below 1.0× ATR, noise stop-out rates exceed ~65% |
| Intraday | 1.5–2.0 | |
| Swing (hours–days) | 2.5–3.0 | Chandelier-style trailing region |
| Position (days–weeks) | 3.5+ | |

The stop distance is computed **once** at entry and stored as `PositionState.initial_risk_distance`. All R-multiples derive from it and it never changes — this is what makes MAE/MFE and trailing policy comparable across trades and what makes I-3 auditable.

---

## 9. Trade lifecycle and the trailing-stop engine

### 9.1 State machine (Report B's, extended for MT5 reconciliation)

```
PROPOSED
   │ risk approval (RiskDecision.APPROVED/RESIZED)
APPROVED
   │ intent written, order_send issued
SUBMITTING
   ├── response lost ──► UNKNOWN ──► RECONCILING ──► CONFIRMED | FAILED
   │ retcode 10009 + deal confirmed
OPEN_PROTECTED            (broker holds SL; guard verifies every cycle)
   │ MFE >= breakeven_trigger_R
BREAKEVEN_ELIGIBLE
   │ trend/volatility condition satisfied
TRAILING
   │ target | stop | time limit | regime exit | risk event | manual halt
EXIT_PENDING
   │ close deal reconciled from history_deals_get
CLOSED
```

**A position is not open because an order was sent. It is open when the deal is reconciled.** Likewise a cancel request is not a cancellation. Both reports state this; MT5 makes it acute because `order_send` can return `None` after a successful execution.

### 9.2 Profit-protection policy (consolidated)

| Position state | Behaviour |
|---|---|
| Before `+1.0R` MFE | Hold the original validated stop. **No trailing.** |
| At ≈ `+1.0R` | Move to spread-aware breakeven: `entry ± (spread + k·point)`, or scale out a partial. Never to exact entry — the spread will stop you out at a small loss. |
| Strong trend, healthy liquidity | Trail by ATR (Chandelier), swing structure, or channel |
| Momentum deterioration | Tighten the trail or take partial profit |
| Spread or volatility shock | Suspend trailing modifications; reduce or exit per strategy policy |
| Broker or data failure | Rely on broker-native SL; enter SAFE_MODE; do not attempt modifications |
| `max_holding_seconds` reached | Exit unless the strategy explicitly permits extension |

**Honest note carried from Report A:** trailing stops do not universally improve expectancy. ATR/Chandelier trails tend to help on swing horizons by letting winners run; on scalping horizons tight trails frequently *degrade* expectancy by converting winners into scratches. Every strategy must A/B test trail-vs-fixed-target in the backtest harness, and the result must be recorded in the strategy spec. Do not apply a global trailing policy.

### 9.3 MT5 trailing implementation

```python
# execution/position_guard.py
def maybe_trail(pos: PositionState, c: SymbolContract, tick, policy) -> None:
    if pos.lifecycle not in ("BREAKEVEN_ELIGIBLE", "TRAILING"):
        return

    ref = tick.bid if pos.side is Side.BUY else tick.ask
    # 1) freeze level: modifications are refused inside this band
    if abs(ref - pos.current_sl) < c.freeze_level_points * c.point:
        return
    # 2) candidate stop from policy (ATR / structure / channel)
    cand = policy.candidate_stop(pos, tick)
    # 3) broker minimum distance from CURRENT price
    floor = min_stop_distance(c, spread_points(tick, c))
    cand = (min(cand, ref - floor) if pos.side is Side.BUY else max(cand, ref + floor))
    cand = round_price(cand, c)
    # 4) MONOTONIC: only ever toward profit  (Invariant I-8)
    improves = cand > pos.current_sl if pos.side is Side.BUY else cand < pos.current_sl
    if not improves:
        return
    # 5) hysteresis: avoid spamming the server on every tick
    if abs(cand - pos.current_sl) < policy.min_step_points * c.point:
        return

    req = {
        "action":   mt5.TRADE_ACTION_SLTP,
        "symbol":   c.name,
        "position": int(pos.position_ticket),   # REQUIRED: modifies a POSITION, not an order
        "sl":       float(cand),
        "tp":       float(pos.current_tp) if pos.current_tp else 0.0,
    }
    res = gateway.submit(mt5.order_send, req)
    if res is None or res.retcode != mt5.TRADE_RETCODE_DONE:
        handle_retcode(res, req)     # 10016 -> recompute floor; 10031 -> SAFE_MODE
    else:
        pos.current_sl = cand
        pos.lifecycle = "TRAILING"
```

MT5-specific traps this code addresses, each of which is a documented, commonly-hit failure:
- `sl`/`tp` **must be floats**. Passing ints causes `order_send` to return `None` with no useful error.
- The `position` field (position ticket) is required for `TRADE_ACTION_SLTP`. Omitting it modifies nothing.
- `freeze_level` blocks modification near price — check before sending, not after failing.
- `stops_level` blocks stops too close to price — retcode 10016.
- Rate-limit modifications. Sending an SLTP on every tick will get you throttled or disconnected by the broker.
- **Because trailing lives in your process, a crash freezes the stop where it was.** Broker-side SL still protects you at the last set level, which is exactly why every stop update must be persisted to the intent ledger before it is sent, and re-read on restart.

---
## 10. Strategy layer

### 10.1 Consolidated strategy catalogue

Merged from both reports, with an MT5-feasibility column added. Report A supplies realistic Sharpe/viability judgements; Report B supplies failure modes. Both are needed.

| Strategy family | Horizon | MT5-feasible? | Core signal | Dominant failure mode | Realistic assessment |
|---|---|---|---|---|---|
| Order-flow momentum scalp | sec–min | ⚠️ Partial | Aggressive-flow imbalance with depth support | False imbalance, queue error, spoofing, latency | MT5 gives no true L2 depth at most retail brokers. **Do not compete on speed.** |
| Liquidity-reversion scalp | sec–min | ⚠️ Partial | Displacement from microprice/VWAP | Displacement may be informed flow, not noise | Viable only on the most liquid FX majors with tight spreads |
| Opening-range breakout | min–hours | ✅ Yes | Break of initial range with volume/vol confirmation | Opening slippage, false breaks | Good v1 candidate |
| Session-based FX (London/NY open) | min–hours | ✅ Yes | Session-conditioned directional bias | Spread widening at session edges | **Recommended v1 candidate** |
| Intraday momentum | min–session | ✅ Yes | Early-session direction, volume surprise | Reversal days, event shocks, crowding | Good v1 candidate |
| Event-driven scalp | sec–min | ⚠️ Risky | Release vs consensus | Feed latency, spread explosion | Requires a low-latency calendar feed; high slippage |
| Volatility-breakout swing | hours–days | ✅ Yes | Break from compression + trend confirm | Failed break, vol collapse | **Recommended v1 candidate** |
| Time-series momentum | days–weeks | ✅ Yes | Sign of past excess returns / trend filters | Momentum crash, regime reversal | Long-run Sharpe ~0.5–1.0; robust, low capacity strain |
| Mean-reversion swing | hours–days | ✅ Yes | Standardised deviation from dynamic fair value | Structural repricing mistaken for noise | Viable; regime-gated |
| Pairs / cointegration | hours–weeks | ✅ Yes (CFD) | Spread divergence on linked instruments | Relationship breakdown, asymmetric liquidity | Sharpe ~1–2 historically, decaying |
| Cross-sectional momentum | days–weeks | ✅ Yes (CFD basket) | Long relative winners / short losers | Hidden factor concentration, turnover | Needs a universe; Phase 5+ |
| FX carry | days–months | ✅ Yes | Rate differential + macro regime | Funding shocks, crowded positioning, crash risk | Watch MT5 `swap_long`/`swap_short` — the swap **is** the carry |
| Funding-rate / basis arb | min–days | ❌ **No** | Spot vs perp funding | Exchange default, transfer delay, basis widening | **Not implementable via MT5.** Requires the spot/perp adapter. Report A rates this the single most realistic retail edge — schedule it for Phase 6. |
| Triangular / cross-exchange arb | sub-sec | ❌ No | Price dislocation | Latency, fees | Arbed out; retail loses the race |

**Phase-1 recommendation:** implement exactly one strategy. Choose **session-based FX momentum on EURUSD** or **volatility-breakout swing on a single liquid symbol**. Both are MT5-native, tolerate retail latency, and have enough trades in a year to validate.

### 10.2 Strategy interface

```python
# strategies/base.py
class Strategy(ABC):
    id: str
    version: str
    book: Book
    eligible_symbols: list[str]
    eligible_regimes: list[str]
    horizon_seconds: int
    max_holding_seconds: int
    declared_averaging_into_losers: bool = False   # must be True to be permitted at all

    @abstractmethod
    def required_features(self) -> list[str]: ...

    @abstractmethod
    def evaluate(self, snapshot: FeatureSnapshot) -> TradeProposal | None:
        """Pure function. No I/O. No randomness without a seeded RNG.
        Must use CLOSED bars only. Must not read anything with
        availability_time > snapshot.as_of."""

    @abstractmethod
    def trail_policy(self) -> TrailPolicy | None: ...

    @abstractmethod
    def cost_model(self, c: SymbolContract) -> CostModel: ...
```

`evaluate` being a **pure function of a point-in-time snapshot** is what makes the same code runnable in backtest, paper and live. Enforce with a property test that calls `evaluate` twice on identical snapshots and asserts identical output.

### 10.3 Mandatory strategy specification

Every registered strategy ships a spec file. Report B's required-item list, adopted verbatim and extended:

| Item | Example |
|---|---|
| Economic rationale | Forced liquidity-taking temporarily displaces price from estimated fair value |
| Universe | FX majors with median spread below X points |
| Trading horizon | 30 s – 5 min |
| Entry rule | Imbalance, displacement and volatility filters all exceed calibrated thresholds |
| Exit rule | Fair-value convergence, time stop, protective stop, or regime change |
| Cost model | Spread + commission + slippage + latency + swap + adverse selection |
| Capacity model | Max order as a fraction of expected depth |
| Invalidation | Edge disappears in rolling out-of-sample monitoring |
| Regime constraints | Disabled on stale feeds, extreme spread, declared event windows |
| Trail decision | Result of the mandatory trail-vs-fixed A/B test (§9.2) |
| **Trial count** | Number of configurations tested to arrive here (feeds DSR) |
| Versioning | Immutable code, data and parameter identifiers |

### 10.4 Allocation

The allocator uses net expected return, uncertainty and correlation — **never recent return alone**. Implement a **strategy half-life**: when live performance drifts from the validated distribution, allocation decays automatically unless new evidence justifies continuation.

```python
w_i ∝ (expected_edge_i / uncertainty_i) * regime_fit_i * decay(live_divergence_i)
subject to: cluster risk caps, book leverage cap, per-symbol risk cap
```

---

## 11. Research, backtesting and validation

### 11.1 Point-in-time discipline

Every dataset row carries the fields in `Stamped` (§4). Backtests must consume only rows where `availability_time <= as_of`. The classic MT5-specific violation is using `rates[0]` (the forming bar) or executing at the same close price used to generate the signal.

Preserve: event time, availability time, processing time, source, revision id, quality flags, licence/entitlement.

### 11.2 Cost model — the equation the simulator must implement

```
Net P&L = Gross P&L
        − spread
        − commission
        − slippage
        − market impact
        − swap / financing        (MT5: swap_long / swap_short, triple on rollover day)
        − exchange / broker fees
        − inference & infrastructure cost
```

Report B's stress requirement is the one to encode as a gate: **a strategy must remain profitable at 1.5×–2× expected costs.** A strategy that only works at zero slippage is not promoted. For scalping, bar-based simulation is insufficient — model bid/ask, event ordering, latency, partial fills, fill probability, gaps, and adverse selection.

### 11.3 Validation battery

| Test | Purpose |
|---|---|
| Walk-forward optimisation | Repeated retraining without future leakage |
| Purged + embargoed CV | Removes overlapping-label leakage |
| Combinatorial Purged CV (CPCV) | Multiple backtest paths, distribution rather than a point estimate |
| Locked unseen holdout | Opened **once**. If inspected and iterated on, it is no longer unseen. |
| Parameter-surface analysis | Rejects isolated "magic" parameter islands |
| **Deflated Sharpe Ratio** | Corrects for trial count, skew, kurtosis, sample length |
| **Probability of Backtest Overfitting** | Estimates chance the selection is an artefact |
| Block bootstrap | Preserves time dependence while testing uncertainty |
| Regime segmentation | Trend / crash / range / thin-liquidity / high-vol tested separately |
| Cost & latency stress | 1.5× and 2× cost sensitivity |
| Capacity stress | Larger sizes, market impact |
| Agent ablation | Does each agent add net value? |
| Counterfactual prompts | Do LLM decisions respond sensibly to opposing evidence? |
| Seed & model variation | Sensitivity to model stochasticity and provider choice |

### 11.4 The trial ledger

```python
# research/trial_ledger.py
@dataclass(frozen=True)
class Trial:
    trial_id: str
    strategy_family: str
    param_hash: str
    dataset_hash: str
    code_commit: str
    in_sample_sharpe: float
    oos_sharpe: float | None
    created_utc: datetime
```
**Failed trials are never deleted.** The count of trials is an input to the Deflated Sharpe Ratio: a Sharpe of 2.0 that was the best of 10,000 attempts is far weaker evidence than the same Sharpe from a single pre-registered hypothesis. An automated research agent is a trial-generating machine, so this ledger is what stands between it and self-deception.

---

## 12. Promotion gates

| Stage | Minimum evidence to advance | Capital |
|---|---|---|
| Research candidate | Economic rationale, reproducible code, complete data lineage | none |
| Validated backtest | Positive net expectancy at 1.5× costs, stable parameter surface, no known leakage | none |
| Independent replication | A separate validator reproduces from source data | none |
| Paper trading | Reliable order logic; **zero unexplained position mismatches** over ≥4 weeks | none |
| Shadow live | Simulated fills compared against obtainable live MT5 prices; slippage within modelled distribution | none |
| Canary | Small capital, compulsory stops, strict drawdown limit | 1–5% of intended |
| Limited production | Stable live slippage, expectancy, operational metrics | 10–25% |
| Scaled production | Sufficient live history across **more than one regime** | full risk budget |
| Retirement | Edge decay, correlation rise, structural change, or failed controls | → 0 |

**Hard rule, from both reports: a five-day burst of profits does not qualify a strategy for scaling.** For high-frequency strategies you need a large number of weakly-dependent trades; for swing strategies you need chronological coverage across regimes. Hundreds of trades compressed into one regime is one observation, not hundreds.

---
## 13. Operations, safe mode and circuit breakers

### 13.1 Kill switches

Four independent levels, each triggerable by a human and by the SRE service, none reachable by an LLM:

```
kill(strategy_id)   → stop new proposals, manage existing to exit
kill(symbol)        → stop new orders on symbol, keep stops active
kill(book)          → core or sleeve independently
kill(firm)          → flatten everything, cancel pendings, disconnect
```

### 13.2 SAFE_MODE triggers

Entering SAFE_MODE means: **no new orders; existing broker-side stops relied upon; alert fires; human acknowledgement required to exit.** Triggers (merged from both reports, MT5 specifics added):

- Tick age exceeds `max_tick_age_seconds` during an open session
- Bar/tick sequence gap detected
- Independent feed comparison diverges beyond tolerance
- Broker positions and internal `PositionState` fail to reconcile
- Realised slippage breaches the strategy's approved distribution by `slippage_breach_sigma`
- Spread, volatility or liquidity crosses emergency thresholds
- Daily or portfolio drawdown limit reached
- **An agent attempts a forbidden tool call** (§5.2)
- Model output changes materially with no corresponding data change
- Clock drift exceeds `max_clock_drift_ms`, or MT5 server-time offset shifts unexpectedly
- **`mt5.terminal_info()` returns `None`, `trade_allowed` is `False`, or retcode 10027/10031 is seen**
- The system cannot confirm that protective orders are active on every open position
- `max_consecutive_rejects` exceeded

### 13.3 Position guard heartbeat

Every hot cycle (≤1 s), for every open position:
1. Confirm the position still exists at the broker (`positions_get` by ticket).
2. Confirm `position.sl != 0` and matches `PositionState.current_sl` within one tick.
3. If SL is missing → attempt to set it immediately; if that fails twice → SAFE_MODE + alert + consider market-closing the position.
4. Update MAE/MFE in R.
5. Evaluate breakeven / trail / time-stop / regime-exit conditions.

Orphan detection runs the reverse direction: any broker position whose magic belongs to this system but which has no `PositionState` is an orphan — adopt it, protect it with a default stop, and alert. Any position with a foreign magic is logged and ignored.

### 13.4 Production scorecard

Return alone is insufficient. Track:

| Category | Metrics |
|---|---|
| Performance | Net return, expectancy, profit factor, Sharpe, Sortino, drawdown |
| Trade quality | Win rate, payoff ratio, MAE, MFE |
| Execution | Spread paid, slippage vs model, fill rate, reject rate, latency, requote rate |
| Risk | Gross/net exposure, leverage, stress loss, concentration, cluster risk |
| Strategy health | Live-vs-backtest divergence, feature drift, regime fit, half-life decay |
| Agent value | Calibration, pairwise disagreement, incremental P&L, research hit rate |
| Operations | Feed gaps, terminal restarts, reconciliation breaks, recovery time |
| **MT5-specific** | retcode histogram, filling-mode fallbacks, SLTP rejection rate, freeze-level blocks, server-time offset drift |
| AI cost | Tokens, inference latency, provider errors, cost per accepted signal |
| Compliance | Blocked orders, audit completeness, rule exceptions |

---

## 14. Security

- **Credential isolation.** MT5 login/password/server live in a secret manager, injected into the gateway process only. Research plane has no access. Investor-password (read-only) accounts for any analytics that do not need to trade.
- **No withdrawal rights** anywhere in the system. On MT5 this is broker-side; on any future crypto adapter, API keys must have withdrawals disabled.
- **Append-only, hash-chained audit ledger** covering every decision, prompt, model version, config delta, order, fill and limit check. Report B is right that this is a first-class requirement, not logging hygiene.
- **Prompt-injection containment.** News and web content reaching LLM agents is untrusted input. Agents that read it may not hold any write tool. Structured extraction only; schema validation on output.
- **Signed configuration.** `risk_constitution.yaml` is verified against its signature at load. Unsigned or modified → refuse to start.
- **Research plane has no network route to the gateway.** Enforce at the network layer, not just in code.

---

## 15. Acceptance tests

The coding agent advances phases only when these pass in CI on a fresh checkout.

### Phase 1 — MT5 gateway
- `test_single_thread_invariant`: static analysis proves no module besides `brokers/mt5/gateway.py` imports `MetaTrader5`.
- `test_reconnect`: kill the terminal mid-run; gateway reconnects with exponential backoff; system enters and exits SAFE_MODE correctly.
- `test_symbol_contract`: contracts load for all configured symbols; `trade_mode == 0` raises; values are never defaulted.
- `test_filling_fallback`: simulated 10030 advances through FOK → IOC → RETURN and caches the winner.
- `test_rounding`: property test — `round_price`/`round_volume` outputs always satisfy `digits`, `tick_size`, `volume_step`, `volume_min/max`.
- `test_server_time_offset`: offset computed, applied, and all persisted timestamps are UTC-aware.

### Phase 2 — Risk and sizing
- `test_sizing_math`: for a table of (symbol, stop distance, equity, risk%) the realised loss at the stop equals the budgeted risk within one tick value.
- `test_no_stop_no_order`: constructing a market order without `sl` is a type error.
- `test_stop_monotonic`: property test — no sequence of guard cycles can move a stop away from profit.
- `test_martingale_rejected`: a proposal sized above the prior loser's risk is rejected.
- `test_daily_loss_halt`: synthetic losses trip the book stop and block all new orders.
- `test_constitution_tamper`: modified YAML without a matching signature refuses to load.

### Phase 3 — Execution and idempotency
- `test_lost_response_no_double_send`: mock `order_send` to raise after the broker executed; assert the system reconciles to exactly one position and never resends.
- `test_restart_reconciliation`: kill the process with an intent in `SUBMITTING`; on restart, reconciliation runs **before** any new order is permitted.
- `test_orphan_adoption`: a broker position with a system magic but no local state is adopted and protected.
- `test_partial_fill`: 10010 adopts filled volume and re-evaluates risk.

### Phase 4 — Agents
- `test_llm_blackout` (**Invariant I-7**): with every LLM provider returning errors, open positions are still monitored, trailed and closed; no unprotected position exists at any point.
- `test_forbidden_tool_call`: an agent attempting `gateway_order_send` trips SAFE_MODE.
- `test_agent_output_schema`: malformed agent output is rejected, not coerced.

### Phase 5 — Research
- `test_no_lookahead`: property test — `Strategy.evaluate` given a snapshot cannot access any row with `availability_time > as_of`.
- `test_forming_bar_excluded`: signals computed from `copy_rates_from_pos` never use index 0.
- `test_dsr_uses_trial_count`: DSR falls as the trial ledger grows for the same raw Sharpe.
- `test_cost_stress_gate`: a strategy profitable only at 1.0× costs fails promotion.

### Phase 6 — End to end
- 4 weeks continuous paper trading with zero unexplained position mismatches, zero unprotected positions, and complete audit coverage.

---

## 16. Build roadmap

| Phase | Deliverables | Exit criterion | Capital |
|---|---|---|---|
| **0. Foundation** | Repo skeleton, schemas, clock/UTC discipline, audit ledger, config loader + signing, CI | Schemas frozen; CI green | none |
| **1. MT5 gateway** | Gateway actor, symbol contracts, retcode taxonomy, market-data ingest, storage | Phase-1 tests pass against a **demo account** | none |
| **2. Risk core** | Constitution, risk engine, sizing, circuit breakers, safe mode | Phase-2 tests pass | none |
| **3. Execution** | Intent ledger, order manager, reconciliation, position guard, trailing engine | Phase-3 tests pass; demo trades open/trail/close correctly for 1 week | demo |
| **4. One strategy** | Session-FX or volatility-breakout strategy + event-driven backtester + cost model | Positive net expectancy at 1.5× costs; walk-forward stable | demo |
| **5. Validation** | DSR, PBO, CPCV, walk-forward, trial ledger, promotion gates | Phase-5 tests pass; strategy passes gates or is rejected honestly | demo |
| **6. Agent layer** | LangGraph warm/cold loops, roles, memory, scoring, ablation | Phase-4 tests pass; agents do not degrade paper P&L | paper |
| **7. Paper house** | Full workflow on live data, simulated orders | 4 weeks clean (Phase-6 test) | paper |
| **8. Shadow** | Compare theoretical orders vs obtainable live prices | Slippage within modelled distribution | paper |
| **9. Canary** | One broker, 1–2 symbols, 1–2 strategies, real money | 30 days within limits, no incidents | 1–5% |
| **10. Controlled expansion** | More strategies, more symbols, sleeve activated | Live evidence per strategy across ≥2 regimes | scaled |
| **11. Second adapter** | Spot/perp crypto (`ccxt`) for funding-basis strategies | Adapter passes Phases 1–3 equivalents | scaled |

**Do not start at Phase 6.** The agent layer is an amplifier and a researcher; it is not a substitute for an edge, and building it before the execution plane is trustworthy produces an impressive system that loses money reliably.

---

## 17. Governance and jurisdiction

### 17.1 Eswatini and broker selection

- The **Financial Services Regulatory Authority (FSRA)** supervises non-bank financial services including capital markets. There is no dedicated local retail FX/CFD licensing regime, so the practical route is a **South African FSCA-regulated broker** or an offshore-regulated one (FCA/CySEC/ASIC).
- **Common Monetary Area:** the lilangeni is pegged 1:1 to the rand and the rand circulates locally. Transfers **within** the CMA are largely unrestricted; transfers **outside** it require exchange-control approval through an Authorised Dealer under Central Bank of Eswatini Financial Surveillance rules, against your own CBE-administered allowance. South African SARB allowances do not apply to Eswatini residents.
- **Crypto:** the most recent official CBE material located stated cryptocurrencies are not legal tender and that, at time of publication, there was no dedicated crypto legislation, with participants trading at their own risk. **Confirm current status directly with CBE and FSRA before launch.**
- **Practical implication:** prefer an FSCA-regulated MT5 broker (ZAR funding, in-CMA transfer, local recourse) over an offshore-only one (higher leverage, no recourse if withdrawals are frozen). Verify the broker offers a **hedging** account (§3.4) and check `stops_level` on your intended symbols *before* committing.

### 17.2 Broker-side selection checklist (run before writing strategy code)

Write a one-off `scripts/broker_audit.py` that connects to the demo account and dumps, for every intended symbol: `trade_mode`, `trade_exemode`, `filling_mode` bits, `trade_stops_level`, `trade_freeze_level`, `volume_min/step/max`, `trade_tick_value_loss`, `swap_long/short`, median spread over a session, and `account_info().margin_mode`. This single artifact determines which strategies are even implementable at that broker, and Report-A-style backtests built without it are fiction.

### 17.3 Capital efficiency

Prop-firm funded accounts (FTMO, FundedNext, The5ers, Funding Pips) are MT5-native and let you access larger capital with your downside capped at the challenge fee. Their rules — typically ~5% daily loss and ~10% total drawdown — **are essentially the risk constitution in §1.4**. Treat them as a feature, not an obstacle. Note that pass rates are low and the model earns primarily from evaluation fees, so budget for multiple attempts and never fund a challenge with money you need.

### 17.4 If other people's money is involved

Managing third-party capital, publishing signals, marketing automated trading, or taking performance fees triggers licensing, client-money, disclosure, AML, custody and audited-reporting obligations in essentially every jurisdiction. **Trade only your own capital or prop-firm simulated capital until you have specialist legal advice.**

---

## 18. Appendices

### 18.1 Consolidated ten principles

Both reports converged on nearly identical closing lists. Merged:

1. Let AI research continuously, but promote nothing without independent evidence.
2. Run multiple strategy families; never rely on one scalping method.
3. Keep LLMs out of the latency-critical and safety-critical paths.
4. Size from a predefined loss limit, never from a desired profit.
5. Protect every live trade with a broker-held stop, plus redundant local logic.
6. Score agents and strategies on net, out-of-sample, live performance.
7. Treat extraordinary backtest returns as errors until proven otherwise.
8. Scale capital slowly enough to detect regime, capacity and implementation failure.
9. Maintain a non-negotiable risk veto and kill switch that no model can touch.
10. Optimise for survival and compounding, not a five-day doubling promise.

### 18.2 MT5 quick reference

```python
mt5.initialize(path=, login=, password=, server=, timeout=60000, portable=)
mt5.last_error() -> (code, description)
mt5.terminal_info()            # .trade_allowed, .connected, .build
mt5.account_info()             # .balance .equity .margin_free .margin_mode .currency
mt5.symbol_select(sym, True)
mt5.symbol_info(sym)           # see SymbolContract, §3.3
mt5.symbol_info_tick(sym)      # .bid .ask .last .time .time_msc .flags
mt5.copy_rates_from_pos(sym, mt5.TIMEFRAME_M1, 0, n)   # index 0 = FORMING bar
mt5.copy_ticks_range(sym, t0, t1, mt5.COPY_TICKS_ALL)
mt5.order_calc_margin(action, sym, volume, price)
mt5.order_calc_profit(action, sym, volume, open, close)
mt5.order_check(request)       # ALWAYS before order_send
mt5.order_send(request)
mt5.positions_get(symbol=|ticket=|group=)
mt5.orders_get(...)
mt5.history_deals_get(t0, t1, group=|ticket=|position=)
mt5.shutdown()
```

Trade actions: `TRADE_ACTION_DEAL` (market), `TRADE_ACTION_PENDING`, `TRADE_ACTION_SLTP` (modify position SL/TP), `TRADE_ACTION_MODIFY` (modify pending order), `TRADE_ACTION_REMOVE`, `TRADE_ACTION_CLOSE_BY`.

Account margin modes: `0 = RETAIL_NETTING`, `1 = EXCHANGE`, `2 = RETAIL_HEDGING`.

### 18.3 What changed relative to the two source reports

| Change | Rationale |
|---|---|
| Added §3 in full | Neither report addressed MT5. This is where the system actually breaks. |
| Merged the two agent rosters | Report B's was more granular; Report A's authority/veto framing was crisper. Kept both. |
| Converted risk guidance into a signed, machine-readable constitution | Both reports gave numbers as prose. A coding agent needs a loadable artifact. |
| Introduced the high-conviction sleeve | Honours the user's appetite for risk without letting it threaten survival — neither report offered a constructive home for aggression. |
| Added Invariants I-1…I-10 and the acceptance-test suite | Turns advisory guidance into things CI can enforce. |
| Reconciled the risk-per-trade numbers | Report A suggested 0.5–1.0%; Report B suggested 0.10–0.25%. Adopted 0.35% core / 1.5% sleeve as a defensible midpoint, explicitly marked as a starting point to be derived from strategy volatility. |
| Reconciled trailing-stop guidance | Report A supplied ATR/Chandelier multipliers and the honest finding that trails can hurt scalping; Report B supplied the state machine and broker-native caveats. Both retained, with the A/B test made mandatory. |
| Marked funding-rate arbitrage as MT5-infeasible | Report A rated it the most realistic retail edge; it cannot be done through MT5 and is deferred to Phase 11. |

### 18.4 Standing caveats

- No credible technology can guarantee 100% profit every five working days. This document does not attempt to deliver that, and any component that appears to is a bug.
- All published multi-agent LLM trading results cited here are backtest-only, short-window and not independently audited. Do not extrapolate.
- Regulatory facts change. Verify FSRA/CBE status, broker terms, leverage caps and exchange-control allowances directly before acting.
- The largest risk to this project is not a crash but a spuriously good backtest that fails live. The validation battery in §11 is the part that separates a real system from an expensive way to lose money.
- This is an engineering specification, not financial advice.
