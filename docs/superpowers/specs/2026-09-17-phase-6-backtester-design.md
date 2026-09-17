# Phase 6 — The Backtester and Cost Model

**Status:** approved design
**Date:** 2026-09-17
**Predecessor:** Phase 5, the position guard (`docs/superpowers/specs/2026-09-09-phase-5-position-guard-design.md`)

Section references are to the **master specification**
(`mt5-multi-agent-trading-house-spec.md`) unless prefixed with "this spec".

## 1. What this phase is for

The system can now open a position exactly once, size it by signed rules, and
keep a stop on it. Nothing can yet answer the only question that matters before
any of that is pointed at money: **does this strategy make money after costs?**

This phase builds the instrument that answers it — an event-driven simulator
that replays recorded bars in point-in-time order, puts a strategy's proposals
through the *real* risk engine and the *real* sizing arithmetic, simulates the
fill and the stop, and produces a deterministic, hashable result.

It deliberately builds **no strategy with an edge**. Its own correctness is
established against toy strategies whose profit and loss can be computed by
hand.

## 2. Scope, and why the spec's roadmap row is split

§16's roadmap bundles "one strategy + event-driven backtester + cost model" into
a single phase. That is three subsystems, and building the simulator and its
first real strategy together has a specific failure mode: **every simulator bug
that flatters the strategy looks like edge, and there is no independent way to
tell.**

So the row is split:

| Phase | Deliverable |
|---|---|
| **6 (this)** | The simulator, the cost model, the `Strategy` interface. Validated by known-answer toys. |
| **7** | The first real strategy, its §10.3 spec file, its capacity model, and the trail-vs-fixed A/B test §9.2 demands — which is what finally unblocks Phase 5's deferred trailing engine. |
| **8** | The validation battery: walk-forward, purged and embargoed CV, CPCV, DSR, PBO, a concrete trial store, and §12's promotion gates. |

A simulator validated against strategies whose answer is known in advance is the
only version that can be trusted to judge one whose answer is not.

## 3. Decisions

| | Decision | Why |
|---|---|---|
| **D-1** | **Bar resolution, and a strategy whose declared `horizon_seconds` is under ten bars of the run's timeframe is refused.** | §11.2 says bar simulation is insufficient for scalping, and we have bars only — Phase 1.5 ingested `copy_rates_range` and nothing finer. Both §10.1 v1 candidates (session FX, volatility breakout) are min–hours or longer and are fine here. Making the limit a refusal rather than a documented caveat is what stops someone validating a 30-second scalp against M1 bars and believing it. Ten is a judgement, not a derivation: below roughly that, most trades open and close inside a couple of bars, so D-2's pessimism dominates the result and the number being measured is the simulator's convention rather than the strategy. It is a named constant, not a literal. |
| **D-2** | **On a bar whose range touches both the stop and the target, the stop happened first. Always.** | The bar cannot say which came first, and this is the largest single source of self-deception in bar simulation. Systematic pessimism costs some genuinely winning trades; the opposite assumption lets a losing strategy look profitable indefinitely. Wrong in the recoverable direction. |
| **D-3** | **The real `RiskEngine` and the real `compute_volume` sit in the decision path.** | §8 says sizing has exactly one home. A simulator with its own arithmetic validates a strategy the live system will never trade. `evaluate_for_execution`'s own docstring anticipated this — "so the pure path stays callable from a backtester that has no terminal" — and margin is already behind `MarginPort`, so a simulated port costs nothing. |
| **D-4** | **Constant notional equity per run.** | Expectancy is a per-trade quantity, and the system already reasons in R (`initial_risk_distance`, `mae_r`, `mfe_r`). Constant equity measures edge; compounding measures capital dynamics and makes two runs over different dates incomparable. Compounding and ruin belong with the validation battery. |
| **D-5** | **`CostModel` is a required input with no default.** | Commission, swap rates and market impact cannot be sourced from anything in the repo (see §6). A simulator that defaulted them to zero would produce the classic flattering result. You should not be able to run a backtest without stating what you believe costs are. |
| **D-6** | **Fills are pessimistic in both directions, by one rule:** the simulator never gives a strategy a better price than it asked for, and always gives a worse one when the bar allows. | One rule is auditable; a collection of per-case conventions is not. It also makes the gap rule and the never-better-than-target rule the same decision rather than two. |
| **D-7** | **One open position at a time.** | `max_concurrent_positions` is enforced *nowhere* at decision time (see §6). A simulator that opened many would report risk the live engine would have permitted while nothing in the live path limits it — divergence in the flattering direction. One position keeps simulator and live identical for the case covered. |
| **D-8** | **Rejections are recorded, not dropped.** | How often the constitution vetoes a strategy is a result. A strategy whose edge lives in trades the constitution would refuse has no edge in this system. |

## 4. Boundaries

```
research/backtest/
  snapshot.py   FeatureSnapshot -- the point-in-time bundle a strategy reads
  strategy.py   the Strategy protocol, TradeProposal in, and TrailPolicy
  costs.py      §11.2's cost equation and the stress multiplier
  fills.py      the bar fill model, including the gap and ambiguity rules
  engine.py     the replay loop
  result.py     BacktestResult -- trades, equity curve, cost breakdown, refusals
```

**The backtester is a composition root**, and saying so is the point. It may
import `core/`, `marketdata/`, `features/` and `risk/` — it is the one place
that legitimately composes all of them.

There is no `strategies/` package yet and this phase does not create one: the
`Strategy` protocol lives in `research/backtest/strategy.py`, and §2.3's
`strategies/impl/` arrives with Phase 7's first real strategy. The acceptance
test is written so that adding it later widens the allowlist deliberately
rather than by accident.

It may **not** import `brokers/` or `execution/`. There is no venue in a
backtest and no idempotent order ledger; a simulator reaching for either is
simulating the wrong thing. This arrow is enforced by an acceptance test with
its own guard-the-guard case, like the four already in
`tests/acceptance/test_architecture.py`.

### What already exists and is not rebuilt

- `TradeProposal` (§4) — the strategy's output, frozen in Phase 0, including its
  `invalidation_price` validator
- `RiskEngine.evaluate` / `evaluate_for_execution`, and `MarginPort`
- `compute_volume`, `compute_stop_distance`, `stop_price` (§8)
- `FeatureEngine.atr()` and `median_spread_points()` over the Phase 1.5 store
- `Bar`, with `availability_time > event_time` enforced at the schema level
- `InstrumentContract.point_size`, whose comment anticipates this exact use:
  "a stored `Bar.spread` is an integer count of these, so without it a spread
  cannot be converted to a price distance"
- `research/trial_ledger.py` — `Trial`, `deflation_trial_count` and the
  `TrialLedger` Protocol. Phase 8 supplies the concrete store; this phase only
  makes results hashable.

### What this phase defines for the first time

`FeatureSnapshot` does not exist anywhere in the repo. Its shape is largely
forced: `RiskEngine.evaluate` already dictates the bundle — `atr`,
`median_spread_points`, `tick_spread_points`, `tick_time` — and `as_of` is what
makes it point-in-time.

Two of those fields are named for a tick stream this phase does not have, so
their mapping is stated rather than left to an implementer: **`tick_spread_points`
is the closing bar's own `spread`**, and **`tick_time` is that bar's
`availability_time`** — the same value as `as_of`. The engine uses them to judge
spread blowout and feed staleness, and the closing bar is the freshest evidence
of either that exists at bar resolution. Any strategy whose edge depends on the
difference between a bar's spread and a true tick spread is one D-1 refuses
anyway.

## 5. The cycle

Per bar, in this order, **and the order is the design**:

1. **Resolve exits against the closing bar's range.** Reversed, a strategy could
   be opening while it is in fact already stopped out.
2. **Set `as_of = bar.availability_time`**, never `event_time`. The schema
   enforces the gap, so "usable only after it closed" is structural rather than
   a rule the simulator has to remember.
3. **Build the snapshot** from rows with `availability_time <= as_of`.
4. **`evaluate(snapshot) -> TradeProposal | None`** — pure, no I/O, the same call
   live will make.
5. **`evaluate_for_execution(...)`** with a simulated `MarginPort` and constant
   `firm_equity`. The execution-time entry point rather than the pure one,
   because the value of driving the real engine is seeing the same vetoes live
   would, §8.1's free-margin headroom included.
6. **Size through `compute_volume` and `stop_price`.**
7. **Queue the entry to fill at the next bar's open** — never this bar's close,
   which is §11.1's named violation.

## 6. Three things the repo cannot currently supply

Stated here rather than discovered during implementation, because each would
otherwise become a silent assumption.

**There is no `commission` field anywhere in `src/`.** Zero occurrences.

**`FinancingModel` is an enum tag only** (`SWAP` / `DIVIDEND_ADJUSTMENT`), with
no rate values, so swap cannot be computed from the contract. It is not
skippable: the volatility-breakout candidate holds positions for days.

Both therefore arrive through `CostModel` as declared inputs, taken from the
broker's published contract specification and recorded in the result. This is
also what §10.2 already expects, where the strategy supplies
`cost_model(contract) -> CostModel` — the spec treats costs as a declared
artifact, not a discovered one.

**`max_concurrent_positions` is enforced nowhere at decision time.** It appears
only in a constitution self-consistency validator
(`max_concurrent × risk_per_trade ≤ cap`), and neither risk-engine entry point
takes open-position state. This is a live-system gap, not a backtest one. D-7
sidesteps it; closing it belongs to whichever phase gives the risk engine a
portfolio view.

## 7. Fills and costs

**Entry** at the next bar's open, plus half that bar's spread for a buy and
minus it for a sell, converted through `point_size`.

**Stop** fills at the worse of the stop price and the bar's open. This is the
gap rule: gapping through a stop and filling *at* the stop invents liquidity
that was never there.

**Target** fills at exactly the target, never better, even when the bar gaps
past it.

**Time stop** (`max_holding_seconds`) fills at the next bar's open with spread.

Both gap cases are D-6's single rule, not two conventions.

### 7.1 The cost equation

```
Net P&L = Gross P&L
        − spread          (in the fill prices above, from Bar.spread × point_size)
        − commission      (CostModel, per lot per side)
        − slippage        (CostModel, points per side)
        − swap            (CostModel, long/short, tripled on the rollover weekday)
        − market impact   NOT MODELLED -- see below
        − inference cost  NOT MODELLED -- see below
```

**Market impact and inference cost are not modelled, and this spec says so
rather than omitting them.** At one position in the minimum lot size, impact is
not distinguishable from slippage; infrastructure cost is not per-trade. Both
become real at the capacity stage, and a spec that listed them while modelling
nothing would be the defect this project has repeatedly found in its own prose.

**The stress gate is a multiplier over the whole model**, so §11.2's "profitable
at 1.5×–2× expected costs" is one parameter rather than a second code path.

## 8. Refusals

Four, because each is a distinct way a backtest can lie:

| Refusal | What it prevents |
|---|---|
| Coverage gap in the requested range | Silently simulating across missing data |
| Any bar with `quality != OK` | Trading on a bar the ingester already flagged as wrong |
| `proposal.as_of > snapshot.as_of` | The point-in-time leak, caught cheaply because `TradeProposal` is `Stamped` |
| Horizon shorter than D-1's bar threshold | Validating a scalp at a resolution that cannot judge one |

## 9. Testing

**Known-answer cases carry this phase.** A toy strategy — "buy every twentieth
bar, exit twelve bars later" — over a synthetic series whose net P&L is
computable by hand. Twelve rather than three because D-1 refuses a horizon under
ten bars, and a spec whose own example test would be rejected by its own guard
is the kind of contradiction this project has spent five phases finding. That validates the *simulator*, which is the entire reason Phase 6 is
split from Phase 7.

Around it: the gap rule, where filling at the stop instead of the open must fail
the test; D-2's ambiguity rule; the peeking refusal; and the cost multiplier
changing net P&L by exactly the modelled amount at 2× and no more.

**Determinism is tested across processes, not within one.** Two subprocess runs
under different `PYTHONHASHSEED`, compared byte for byte. Calling a function
twice in one process proves almost nothing, and this project shipped exactly
that hollow fixture in Phase 2 — set and dict ordering reaching the output is
the realistic leak, and only a different hash seed exposes it.

Every guard above is proven by a mutation: break the code it protects, watch
that test and only that test fail.

## 10. Deliberately excluded

| Item | Where it lands |
|---|---|
| Any strategy with a real edge | Phase 7 |
| The trail-vs-fixed A/B test, and the trailing engine it unblocks | Phase 7 (§9.2) |
| Walk-forward, CPCV, DSR, PBO, promotion gates | Phase 8 |
| A concrete `TrialLedger` store | Phase 8 |
| Compounding equity, drawdown and ruin analysis | Phase 8 (D-4) |
| Tick-resolution simulation, queue position, adverse selection | Not scheduled; needs tick ingest and serves families §10.1 advises against |
| Market impact and capacity modelling | The capacity stage |
| Portfolio allocation across strategies | §10.4, with the allocator |

## 11. Handoff to Phase 7

A strategy author receives: a `Strategy` interface whose `evaluate` is a pure
function of a point-in-time snapshot, a simulator that refuses the four ways a
backtest lies, a cost model that cannot be silently zeroed, and a result that is
byte-reproducible across processes and therefore hashable into a trial.

What Phase 7 must supply: the economic rationale, the entry and exit rules, the
declared cost and capacity models, the trail-vs-fixed evidence, and the trial
count that feeds the Deflated Sharpe Ratio.
