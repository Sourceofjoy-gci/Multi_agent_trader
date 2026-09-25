# Phase 7 — The First Real Strategy

**Status:** approved design
**Date:** 2026-09-22
**Predecessor:** Phase 6, the backtester and cost model (`docs/superpowers/specs/2026-09-17-phase-6-backtester-design.md`)

Section references are to the **master specification**
(`mt5-multi-agent-trading-house-spec.md`) unless prefixed with "this spec".

## 1. What this phase is for

Six phases built an instrument and nothing to measure. The system can read a
demo terminal, store bars point-in-time, compute two indicators, size a trade by
signed rules, submit it exactly once, keep a stop on it, and replay the whole
path deterministically against an explicit cost model. The only thing it has
ever evaluated is a toy that buys every twentieth bar.

This phase supplies the thing being measured: **one strategy, with the economic
rationale and the evidence §10.3 demands**, and the machinery a real strategy
needs that a toy did not — session features, a fixed target, a trailing stop,
and the A/B test that decides between them.

It also makes the declared economics load-bearing for the first time. Until now
a strategy could claim any expected return it liked and nothing would look.

## 2. What the repository cannot supply today

Four gaps, each verified against the source rather than assumed. They are
recorded here because each one shapes a decision below.

**The feature engine computes two things.** `wilder_atr` and
`median_spread_points` are the entire indicator library
(`src/trading_house/features/indicators/`). `FeatureSnapshot` carries one bar
plus those two numbers. No strategy in §10.1 can be expressed from that.

**The proposal economics are consumed nowhere.** `win_probability`,
`expected_return_bps` and `expected_cost_bps` appear in
`core/schemas.py` and, as hard-coded constants, in the toy at
`ops/backtest.py:137-140`. `grep` over `src/` finds no other reader. The risk
engine never looks at them.

**The constitution's per-book limits layer is enforced nowhere.**
`min_expected_edge_after_cost_bps`, `max_position_duration_seconds`,
`flat_by_session_close`, `max_swap_cost_pct_of_expected_edge`,
`max_overnight_positions`, `max_weekend_exposure_pct`, `gap_risk_multiple` and
`earnings_blackout_days` appear only in `constitution/models.py` as fields.
Every one is parsed, validated, and then ignored.

**`ExitKind.TARGET` is structurally unreachable.** `risk/engine.py:235`
hard-codes `"take_profit_price": None` on every executable decision, so the
simulator's target branch — fully implemented and tested in `fills.py` — has
never executed. Phase 6's D-2, its headline pessimism rule, is therefore proven
only in unit tests and never end to end.

**A note on numbering.** This spec's decisions are D-1 to D-9. Phase 6 has its
own D-1 to D-8, and several are referenced below; every such reference names
Phase 6 explicitly. An unqualified "D-n" always means this spec's.

## 3. Decisions

| # | Decision | Why |
|---|---|---|
| D-1 | One strategy: session-conditioned directional bias on EURUSD, prior-session carry-over | §10.1's recommended v1. Roughly 250 trades a year, which is the decisive argument: Phase 8's Deflated Sharpe cannot validate a strategy with thirty |
| D-2 | Sessions are fixed UTC windows, never timezone-aware | `zoneinfo` would make a result depend on the machine's tzdata version, and tzdata changes several times a year. A phase whose contract is byte-reproducibility cannot have a drifting clock definition |
| D-3 | The snapshot grows named typed fields, not a feature map or a bar window | This codebase is strict models and mypy strict throughout. A `Mapping[str, Decimal]` would be its first stringly-typed interface, and a misspelled feature would surface at runtime instead of as a type error |
| D-4 | The entry rule has exactly one knob | Every configuration is a trial and every trial deflates the Sharpe. The hold is a session boundary, the direction is a sign, and the timeframe is forced by Phase 6's own horizon guard |
| D-5 | The strategy spec is a validated artifact, not a markdown file | A strategy that cannot produce all twelve §10.3 items cannot register. The trial count becomes a required field rather than a line somebody remembers |
| D-6 | The edge floor is amended into `SwingLimits` and the constitution re-signed | An edge floor is not a horizon-specific idea, and without it every gate this phase adds would be inert against the strategy it ships |
| D-7 | The A/B runs three arms with one pre-declared parameter each, no sweep | Three trials, not thirty. Each arm's value comes from convention or the literature, never from looking at the result |
| D-8 | `DEFECTIVE_BAR` gains a declared tolerance | The alternative is choosing date ranges that happen to be clean, which is selection on the data — a worse sin than the one the refusal prevents |
| D-9 | Phase 6's one-bar fill-stamp offset is corrected here | The numbers it would move belong to the toy, and this phase replaces the toy. A strategy whose holding period is its central parameter cannot be evaluated against a holding period that is silently wrong |

## 4. The strategy

### 4.1 Economic rationale

Information arriving outside European hours is priced into EURUSD in thin
conditions, by participants who are neither the marginal price-setter nor able
to move size. When London opens and the deepest liquidity of the day returns,
that partial repricing continues to be absorbed. The claim is narrow and
falsifiable: **the sign of the overnight move carries information about the
London session's direction, and it carries enough of it to survive a retail
spread.**

The claim may be false. That is a completed phase (§10, this spec).

### 4.2 The rule, stated completely

> On the first closed M15 bar whose session is `LONDON` and whose
> `bars_since_session_open` is zero, if `prior_session_return` is non-zero,
> propose a trade in the direction of its sign. Hold until 16:00 UTC.

Windows, in UTC: Asian `00:00–07:00`, London `07:00–16:00`, New York
`12:00–21:00`, everything else `OFF`. The London and New York windows overlap;
`session` resolves a bar to London during the overlap, because the London
window is the one this strategy trades.

Three edges the rule must answer explicitly, because leaving them to the
implementer is how a strategy acquires behaviour nobody specified:

- **The prior window has no completed bars** — a holiday, a data gap, or the
  first window in the store. `prior_session_return` is then `None` and no
  proposal is made. It is not zero: zero is a real return that also makes no
  proposal, and conflating the two would hide a data problem inside a
  no-signal day.
- **`prior_session_return` is exactly zero.** No proposal. A sign of zero is not
  a direction, and picking one would be inventing signal.
- **`max_holding_seconds` is the seconds from the fill bar's `event_time` to
  16:00 UTC that day.** The 07:00 closed bar produces a 07:15 fill, so the
  declared value is 31500 seconds; 32400 would hold until 16:15. It is derived
  from the fill time and session boundary, not declared, so it cannot drift
  from the rule it implements.

**The timeframe is forced, not chosen.** Phase 6's D-1 refuses a horizon shorter
than ten bars of the timeframe. A nine-hour hold is thirty-six M15 bars and nine
H1 bars, so H1 is rejected by the system's own guard. M15 is the coarsest
timeframe that passes.

**The book is forced, not chosen.** `fx_scalp` caps a position at 300 seconds
(`max_position_duration_seconds`), so a strategy holding a session is
`fx_swing`. Its `k_sigma` of 2.5 sets the volatility stop component, its
`risk_per_trade_pct` of 0.75 sets the size, and both are read off the
constitution rather than declared by the strategy.

**The structural invalidation is a fixed 10 pips from entry** (`0.00100` for
EURUSD), not a tuned parameter and not an additional A/B arm. The proposal states
that level so `TradeProposal` has a loss-side invalidation; `RiskEngine` still
computes the executable stop and may widen it for volatility, spread, contract,
or broker-distance constraints. This value is an economic prior declared before
the three planned trials, not a value selected after observing them.

### 4.3 Declared items (§10.3)

| Item | Value |
|---|---|
| Economic rationale | §4.1 above |
| Universe | `fx.eurusd` only |
| Trading horizon | Nine hours, 07:00–16:00 UTC |
| Entry rule | §4.2 above |
| Exit rule | Engine stop, the 16:00 UTC time stop, and whichever of target or trail the A/B selects |
| Cost model | Spread, commission, slippage, swap — all declared, none defaulted (Phase 6 D-5) |
| Capacity model | Not modelled. Stated as a non-promise (§10, this spec) |
| Invalidation | Rolling out-of-sample monitoring; Phase 8 owns the gate |
| Regime constraints | The risk engine's spread ceiling, spread-to-stop fraction and tick-staleness gates. Plus the stated DST limitation of D-2 |
| Trail decision | The result of §8's A/B, recorded after the run |
| Trial count | Three, unless a rerun occurs; see §9.3 |
| Versioning | Strategy id and version, constitution hash, contract file digest, and the result digest Phase 6 already produces |

## 5. Features

Four new fields on `FeatureSnapshot`, each computed by `FeatureEngine` from
closed bars only, so the strategy is structurally incapable of reading past
`as_of` (I-17).

| Field | Type | Definition |
|---|---|---|
| `session` | `Session` | The window containing the bar's `event_time` |
| `prior_session_return` | `Decimal \| None` | Close-to-close return of the immediately preceding completed window. `None` when that window holds no completed bars, which is distinct from a return of zero (§4.2) |
| `session_open_price` | `Decimal` | Open of the current window's first bar |
| `bars_since_session_open` | `int` | Zero on the window's first closed bar |

Only `prior_session_return` carries signal. The other three exist so the entry
condition is expressible without the strategy reasoning about timestamps itself
— a strategy that computes its own session boundaries is a strategy that can get
point-in-time wrong privately.

A session window is usable only when every expected bar from its start through the
reference bar is present at the requested timeframe. An interior gap makes
`prior_session_return` return `None` and makes the two current-session methods
raise `InsufficientHistoryError`; a partial window must never be reported as a
complete overnight return, open, or bar count.

`features/sessions.py` holds the pure functions; `FeatureEngine` holds
the only reference to the bar store, unchanged from Phase 2.

## 6. The strategy package

```
src/trading_house/strategies/
    spec.py        StrategySpec: the twelve §10.3 items, validated
    registry.py    id -> strategy, replacing the toy registry in ops/backtest.py
    impl/
        session_momentum.py
```

**Import boundary.** `strategies/` may import `core/`, `features/` and `risk/`.
It may not import `brokers/`, `execution/` or `research/`. `FeatureSnapshot` and
`ExitPolicy` move to `core/snapshot.py` and `core/exits.py` so the concrete
strategy can implement the shared protocols without depending on the backtester;
their old research modules re-export them for compatibility. The backtester never
imports `strategies/` — it takes the `Strategy` Protocol and the composition
root wires the concrete strategy in, exactly as it wires the toy today. The
arrow is AST-checked in `tests/acceptance/test_architecture.py` with a
guard-the-guard case, like the five existing ones.

`StrategySpec` is a `CanonicalModel`. A strategy registers with one or does not
register.

## 7. The gates

Three limits bind a single proposal with no portfolio state, and each becomes
one comparison in `RiskEngine.evaluate` with its own `RejectionReason`:

| Limit | Compared against | New reason |
|---|---|---|
| `min_expected_edge_after_cost_bps` | `expected_return_bps - expected_cost_bps` | `EDGE_BELOW_FLOOR` |
| `max_position_duration_seconds` | `proposal.max_holding_seconds` | `HOLDING_EXCEEDS_BOOK_LIMIT` |
| `max_swap_cost_pct_of_expected_edge` | Estimated swap over the hold, as a fraction of declared edge | `SWAP_EXCEEDS_EDGE_FRACTION` |

`expected_swap_cost_bps` is required on every `TradeProposal`. Zero is a valid
explicit declaration for an intraday strategy; omission is not, because it lets a
swing proposal bypass the swap-fraction gate by leaving the field out.

The remaining limits — `flat_by_session_close`, `max_overnight_positions`,
`max_weekend_exposure_pct`, `gap_risk_multiple`, `earnings_blackout_days` — need
portfolio, session or calendar state that no risk-engine entry point takes. They
stay unenforced and stay recorded (§11).

**The constitution is amended and re-signed.** `min_expected_edge_after_cost_bps`
moves from `ScalpLimits` onto both limit models, because "do not take a trade you
expect to lose money on" is not horizon-specific. Without this the edge gate
would be declared for scalp, inert for the only strategy this phase ships, and
tested only against a synthetic proposal — enforcement code that cannot fire,
which is this project's signature defect one level up.

**The signing key never leaves the main checkout.** Amending the constitution
requires the Ed25519 private key at `.local/keys/risk_constitution.private.pem`.
No implementer subagent receives it. The amendment's re-sign is performed in the
main checkout by the controller or the operator, outside the plan's tasks, and
the plan must state the amended-but-unsigned state its tasks work against.

## 8. Exits: target and trail

**The target is unblocking, not building.** `resolve_exit` already implements
target fills on both sides with the gap rule and stop-before-target ordering,
fully tested. It has been unreachable only because `risk/engine.py:235` returns
`None`.

`TradeProposal` gains `target_r_multiple: Decimal | None`. The proposal is not
persisted — no migration references it, and it lives only between the strategy,
the Protocol and the risk engine — so this costs no schema migration and no
ledger change. The engine computes `take_profit_price = entry ± r_multiple ×
stop_distance` from **its own** stop distance, so every price level is computed
in one place, the principle Phase 6's D-3 enforces for sizing. The strategy
declares intent; the engine sets the price.

**`TrailPolicy` becomes `ExitPolicy`.** A fixed target is not a trail, and
keeping both under a type named for trailing would misname one of them. Phase
6's docstring already reserves this widening. Three variants, discriminated on
`kind`:

| Variant | Fields |
|---|---|
| `none` | — |
| `fixed_target` | `r_multiple` |
| `chandelier` | `atr_multiple`, `min_step_points` |

**Trailing in the simulator** is the one genuinely new mechanism. §9.3 gives the
live algorithm; the simulator needs its pure analogue, and three properties make
it not a toy:

- **Monotonic.** A stop never moves away from profit. This is I-8, and it gets a
  Hypothesis test over arbitrary bar sequences rather than examples.
- **Hysteresis.** A candidate closer than `min_step_points` is ignored, or the
  stop is re-modified on every bar.
- **Distance floor.** The candidate is clamped to the broker's minimum distance
  from current price.

A `fixed_target` arm and the proposal's `target_r_multiple` must agree; a
mismatch refuses the run rather than recording a result under the wrong arm. A
non-positive Chandelier candidate likewise refuses the run rather than masquerading
as an unchanged trail.

## 9. The A/B, and its price

§9.2 makes the trail-vs-fixed result a mandatory item in the strategy spec, and
carries an honest note: trails tend to help on swing horizons and to degrade
expectancy on scalping ones by converting winners into scratches. This strategy
is nominally swing but entirely intraday, so the answer is genuinely unknown.
That is what makes the test worth its price.

### 9.1 Three arms

| Arm | Parameter | Source of the value |
|---|---|---|
| `none` | — | The baseline: engine stop plus the 16:00 time stop |
| `fixed_target` | `r_multiple = 1.0` | Convention |
| `chandelier` | `atr_multiple = 3.0` | The standard Chandelier value |

### 9.2 No sweeps

Each arm's parameter is declared before the run and not tuned after it. Sweeping
target multiples from 0.5 to 3.0 against ATR multiples from 2 to 5 would be
thirty trials rather than three, and a Deflated Sharpe computed over thirty
trials of a 250-trade-a-year strategy is close to worthless. The trial-count
field exists to make exactly that visible.

### 9.3 Reruns are trials

Any rerun after seeing the numbers is a new trial and is counted. This is stated
here so that the count is maintained from the first run rather than
reconstructed from memory afterwards.

## 10. Data and evidence

1. **Backfill** EURUSD M15 from the demo terminal, taking whatever depth the
   broker serves. Record the actual span. If it is one year rather than three,
   the spec says one year and the conclusion is correspondingly weak.
2. **Run the three arms once each**, recording net P&L after costs, trade count,
   defective-bar fraction, and the ranking.
3. **Write all of it into the `StrategySpec`.**

**Defective bars get a declared tolerance (D-8).** `BacktestResult` carries a
`defective_bars` count, and a run refuses only when the fraction exceeds a
ceiling the request states explicitly. Phase 6's refusal exists because dropping
a bar silently leaves the run shorter than the period it claims; a declared
tolerance keeps that honesty while surviving contact with real data, because the
run now says by exactly how much it fell short.

**Capacity is not modelled.** Order size against expected depth needs depth data
MT5 does not give at retail. Stated as a non-promise in the strategy spec and
the README rather than filled with an invented number.

Declared slippage must be non-negative. Costs may make a fill worse; a negative
slippage declaration would reverse that invariant and let a model improve entry
or exit prices.

**A negative result is a completed phase.** If the strategy does not make money
after costs, that is recorded in the spec with its trial count and becomes Phase
8's input. Requiring a positive result would put the pressure on tuning until
the number looked right, which is the failure the Deflated Sharpe, the trial
count and this entire spec file exist to prevent.

## 11. Testing

- **Known-answer**, carried the way Phase 6 was: a synthetic session series whose
  prior-session return is engineered to a chosen sign, with the entry bar, fill
  price and exit derived from a closed form defined once in the test module —
  never pasted from what the code printed.
- **Purity**, which §10.2 mandates: `evaluate` called twice on identical
  snapshots produces identical output.
- **Monotonic trailing under Hypothesis**: over arbitrary bar sequences, the
  stop never moves away from profit.
- **The `strategies/` import arrow**, AST-checked with a guard-the-guard.
- **Spec completeness**: a registered strategy missing any of the twelve §10.3
  items fails an acceptance test.
- **Each new gate fires**: a proposal below the edge floor, one exceeding the
  duration cap, one whose swap eats the declared edge.
- **Mutation on every new test.** Break the code the test protects, watch that
  test and only that test fail. Thirteen tests in this repository have passed
  for reasons unrelated to their claims; this is the practice that found them.

## 12. Deliberately excluded

| Item | Where it lands |
|---|---|
| Walk-forward, CPCV, DSR, PBO, promotion gates | Phase 8 |
| A concrete `TrialLedger` store | Phase 8 |
| Compounding equity, drawdown and ruin analysis | Phase 8 |
| A second strategy, and allocation across strategies | §10.4, with the allocator |
| `flat_by_session_close`, `max_overnight_positions`, `max_weekend_exposure_pct`, `gap_risk_multiple` | Whichever phase gives the risk engine a portfolio and session view |
| Capacity and market-impact modelling | The capacity stage; needs depth data MT5 does not serve |
| Live or paper execution of this strategy | Unscheduled; the demo-only guard stands |
| The digest's scale sensitivity, and the `gross_pnl` field split | Phase 8, carried from Phase 6 |

## 13. Handoff to Phase 8

Phase 8 receives: one registered strategy with a complete §10.3 spec, a trial
count maintained from the first run rather than reconstructed, a result digest
per arm, and an A/B outcome that is evidence rather than a preference. It also
receives the honest answer to whether this strategy makes money after costs —
which is the input a promotion gate needs, in whichever direction it points.

What Phase 8 must supply: the trial ledger, the walk-forward and combinatorial
purged cross-validation the master spec's §11 describes, the Deflated Sharpe
Ratio that reads the trial count, and the promotion gate that decides whether
this strategy ever sees a paper account.
