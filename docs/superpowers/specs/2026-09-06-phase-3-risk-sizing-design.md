# Phase 3 — Risk and Position Sizing

**Status:** approved design
**Date:** 2026-09-06
**Predecessor:** Phase 2, the feature engine (`docs/superpowers/specs/2026-09-02-phase-2-feature-engine-design.md`)

## 1. What this phase is for

Phase 2 produced two numbers. Phase 3 is what consumes them, and it is the
last purely deterministic component before anything can reach a broker.

It converts a `TradeProposal` plus a handful of market facts — firm equity, the
instrument contract, the latest tick, and the ATR and median spread for that
instrument — into a `RiskDecision`: an approval carrying a lot size and a stop
price, or a refusal carrying reasons.

Section references below are to the **master specification**
(`mt5-multi-agent-trading-house-spec.md`) unless prefixed with "this spec".

**Nothing here is advisory.** A wrong lot size is a real loss on a real
account, and the failure is silent: an over-sized position looks exactly like a
correctly sized one until the stop is hit. The correctness bar is the execution
path's.

## 2. Decisions

| | Decision | Why |
|---|---|---|
| **D-1** | Ship §8 in full plus those §7.1 checks whose inputs exist today. **Every check needing book history is excluded.** | Daily loss, drawdown, position count, cluster risk, order rate and the martingale prohibition all read `BookState`. Nothing has ever placed an order, so `BookState` would have no writer — the model would be invented now and first exercised a phase later, which is how a fictional interface calcifies. |
| **D-2** | **`evaluate()` is pure; the margin gate sits behind a narrow port.** Two entry points, not one. | Sizing must run identically in live trading and in a backtester that has no terminal. A backtest whose sizing diverges from production lies about expectancy. |
| **D-3** | **Quantise the stop distance first, then size from the quantised distance.** | Both roundings then push the same way, so realised risk is bounded above by the budget rather than straddling it. See §4. |
| **D-4** | **`k_sigma` and `k_spread` live in the signed constitution, per book.** | Halving `k_sigma` halves every stop and roughly doubles every position. That is a risk parameter, and it belongs behind the signature alongside `risk_per_trade_pct`, not in a caller's argument list. |
| **D-5** | **`InstrumentContract` carries the point size.** | `Bar.spread` is an integer in MT5 points. Without the point size, a stored spread cannot be converted to a price by anything downstream — the backtester included. |
| **D-6** | **The broker floor is `max(min_stop_distance, freeze_distance)`.** | §8.2's comment says "stops/freeze level" while its code uses only the first. A stop inside the freeze band is legal to place and illegal to modify, which would hand the position guard an untouchable stop on a live position. |
| **D-7** | **Gates collect every reason; rejection reasons are a `StrEnum`.** | One rejection listing three faults is one diagnosis; three sequential rejections are three. The schema field is `NonEmptyStr`, so without an enum a typo could drift between the engine and its test undetected. |
| **D-8** | **`risk_money` on the decision is the realised figure, not the budget.** | Recording the budget would make the audit ledger agree with itself by construction, which is the one thing an audit record must not do. |

## 3. Boundaries

```
risk/
  sizing.py   pure    stop distance, tick quantisation, volume, stop price
  engine.py   RiskEngine(constitution, clock) — ordered gates, MarginPort
```

`risk/` imports `core/` and `constitution/` and **nothing else from this
project** — not `brokers/`, not `marketdata/`, not `features/`. ATR and median
spread arrive as `Decimal` arguments. `MarginPort` is a Protocol defined in
`risk/engine.py` and satisfied structurally by the venue adapter, the same
shape as Phase 2's `BarReader`. An acceptance test pins the arrow, as
`features/` is pinned against `brokers/`.

### What Phase 0 already froze

This phase consumes these rather than reinventing them:

- the discriminated `RiskDecision` union — `Approved` / `Resized` / `Rejected`
- `ExecutableRiskDecision.stop_loss_price: Price`, non-optional. **§7.3's
  "trading without a protective stop" is therefore already structurally
  impossible at the decision layer.** Phase 3's obligation is not to build it
  but not to undo it.
- `RejectedRiskDecision`'s `risk_money` pinned to exactly zero
- `TradeProposal.invalidation_price`, validated onto the loss side of
  `entry_price_ref` — this **is** §8.2's structural term; no placeholder needed
- `Quantity(amount, unit)`, `Clock`

### What changes outside `risk/`

| Change | Where | Consequence |
|---|---|---|
| `k_sigma`, `k_spread` | `BookLimits` and `config/risk_constitution.yaml` | the YAML must be re-signed |
| point size | `InstrumentContract`, populated in `contracts.py` from the `point` it already reads and currently discards | no migration, no stored data changes |
| account equity, required margin | one venue operation behind `MarginPort` | the only new I/O in this phase |

## 4. The arithmetic

### 4.1 Stop distance

```
d_raw = max(
    k_sigma  * atr,                                   # volatility
    k_spread * median_spread_points * point_size,     # cost
    |entry_price_ref - invalidation_price|,           # structural, from the proposal
    max(min_stop_distance, freeze_distance),          # broker floor (D-6)
)
d = ceil(d_raw / price_increment) * price_increment
```

`min_stop_distance` is already in price units and already floored to one
increment by `contracts.py`, so it takes no points conversion.

**Two spreads are in play and they are not interchangeable.** The cost term
takes the **median** spread from Phase 2 — a single spike must not widen the
stop, which is why Phase 2 returns a median at all. The `spread_exceeds_ceiling`
gate in §5 of this spec takes the **current** tick's spread and compares it to a
multiple of that median. Using the current spread in the cost term would make
the stop distance, and therefore the lot size, a function of one quote.

### 4.2 Volume

From the **effective** distance `d_eff = |entry_price_ref - stop_price|` — the
distance to the stop that is actually emitted, defined in §4.3 — never from the
raw distance and never from §4.1's `d`:

```
book_equity   = firm_equity * book.capital_fraction
budget        = book_equity * book.risk_per_trade_pct / 100
ticks         = d_eff / price_increment               # fractional when entry is off the grid
money_per_lot = ticks * value_per_price_increment
volume        = floor((budget / money_per_lot) / quantity_increment) * quantity_increment
```

`ticks` is **not** an integer in general. `d` is a whole number of increments
by construction, but `d_eff` is not, because `entry_price_ref` need not sit on
the tick grid (§4.3). The loss is proportional to the true price distance, so
`d_eff` is divided as it stands; quantising it back onto the grid would
understate it and restore the very overshoot §4.4 forbids. `risk_money` is
computed from the same `d_eff`, which is what makes D-8's claim true.

The `capital_fraction` multiply is load-bearing. `BookLimits`'s own docstring
states its percentages are relative to that book's slice of firm equity, so
passing firm equity straight in would over-risk the core book by 11% and the
sleeve by **ten times**.

`volume < quantity_min` is a rejection (`below_min_lot`), never a round up into
existence. `volume > quantity_max` is clamped, and the verdict becomes
`RESIZED` — the only source of `RESIZED` in this phase, since every cap that
would shrink it further needs book state (D-1).

### 4.3 Stop price

**The stop is anchored on `proposal.entry_price_ref`, not on the live tick.**
`BUY: entry_price_ref - d`, `SELL: entry_price_ref + d`, each then quantised
**away from entry**, because `entry_price_ref` is a strategy's reference price
and is not guaranteed to sit on the tick grid. A BUY whose stop could not land
on a positive tick — `entry_price_ref - d < price_increment` — is a rejection,
not a clamp. SELL needs no such check: `entry_price_ref` is positive and `d`
is at least one increment, so the sum cannot be non-positive.

Away-from-entry only ever widens — and a wider stop is a **larger** loss at that
stop, not a smaller one, so this rounding does not survive §4.4 on its own. The
guarantee holds only if the sizing is derived from the effective
post-quantisation distance `d_eff = |entry_price_ref - stop_price|`, which is
what §4.2 divides. Sizing from §4.1's `d` while emitting a stop `d_eff` away
overshoots the budget by `d_eff / d` — up to 50% on a one-tick stop — while
`risk_money` still reports the budget figure, breaking I-19 and falsifying D-8.
The stop price is therefore computed **before** the volume, not alongside it.
Because `d_eff >= d` always, sizing from `d_eff` can only shrink the position,
so the guarantee is strengthened rather than weakened.

Anchoring on the reference rather than the tick is what makes the decision
replayable: the same proposal and the same stored bars must yield the same stop
in a backtest a year later, and a live quote is not available then. The gap
between the reference price and the actual fill is slippage, which belongs to
execution — the tick reaches this phase only through the staleness and spread
gates.

### 4.4 The guarantee — invariant I-19

**I-19** — *An approved decision's `risk_money` never exceeds the book's
budgeted risk, and — unless the lot cap bound it — falls short by less than one
lot step's worth.*

```
budget - money_per_lot * quantity_increment  <  risk_money  <=  budget
```

Both roundings in §4.1–4.3 push the same direction: the distance rounds up, so
the stop is never nearer than the risk model demanded; the volume floors, so
the position never carries more risk than the budget allows.

This is deliberately one-sided, and deliberately stronger than §15's
`test_sizing_math`, which asks that realised loss "equals the budgeted risk
within one tick value". The real bound is one *lot* step, and it is not
symmetric. A symmetric tolerance would pass against an implementation that
overshoots the budget half the time — which is the failure this phase exists to
prevent.

## 5. The gates

A short prelude must pass before anything can be computed: `unknown_book`,
`instrument_mismatch`. The remaining gates are independent, and every failing
one contributes a reason (D-7).

| Reason | Source |
|---|---|
| `side_not_permitted` | `contract.can_open_long` / `can_open_short` |
| `asset_class_not_permitted` | `book.asset_classes` |
| `spread_exceeds_ceiling` | see below |
| `tick_stale` | `safe_mode_triggers[horizon]` against `Clock.now()`, bounded on BOTH sides: `max_tick_age_seconds` behind, `max_clock_drift_ms` ahead — a tick stamped in the future is as unusable as a stale one |
| `below_min_lot` | §4.2 |
| `stop_price_not_positive` | §4.3 |

**The spread ceiling resolves a genuine ambiguity in the constitution.**
`safe_mode_triggers[horizon].max_spread_multiple_of_median` is defined for every
horizon; `ScalpLimits.max_spread_multiple_at_entry` exists only for scalp and is
entry-specific. The gate takes the **tightest applicable**: the horizon trigger
always, and additionally the scalp entry multiple when the book's horizon is
scalp. Choosing one and ignoring the other would leave a signed ceiling with no
enforcement anywhere.

## 6. The margin seam

```python
class MarginPort(Protocol):
    def free_margin(self) -> Decimal: ...
    def required_margin(self, instrument_id, side, quantity, price) -> Decimal: ...
```

§8.1 requires free margin to be at least twice the requirement, so that a single
adverse move cannot cascade into forced liquidation across the book.

The risk of simply forgetting that call is handled structurally rather than by
convention:

```python
def evaluate(...) -> RiskDecision:                   # pure — the backtester calls this
def evaluate_for_execution(..., margin: MarginPort)  # evaluate() plus the headroom gate
```

Skipping the margin check stops being a forgotten line and becomes calling a
differently named function, and the live order manager cannot reach the second
entry point without supplying a port.

## 7. Testing

**Two tests carry this phase.**

**Realised loss against hand-worked examples.** A table of (contract, stop
distance, equity, risk %) where the lot size and the loss at the stop are
computed longhand in a comment, so a reader verifies the numbers without running
the code. This project has now found five cases where a test's expected value
and its implementation were authored from the same belief and neither was ever
checked against anything external.

**The I-19 property.** Generated contracts, equities and multipliers: realised
risk never exceeds the budget, and sits within one lot step below it whenever
the lot cap did not bind. This is the one that can actually fail.

Beyond those:

- **Each of the four stop terms must be shown winning the `max()` in turn.** A
  term never proven load-bearing is a term that could be deleted with the suite
  still green — which is how the Phase 1 timeframe double-translation and the
  Phase 2 hollow determinism fixture both survived their first review.
- Boundary tests one lot step either side of `quantity_min`.
- A book at `capital_fraction` 0.1, pinning the equity derivation. Passing firm
  equity instead must fail this test by ten times, not by a rounding artifact.
- `freeze_distance` winning the broker floor.
- The margin gate in both directions.
- An acceptance test pinning `risk/`'s imports.

## 8. Deliberately excluded

| Item | Where it lands |
|---|---|
| Every check reading `BookState` — daily loss, drawdown, position count, cluster risk, order rate, martingale, `safe_mode` | The execution phase, which gives `BookState` a writer |
| Session open, event blackout, regime eligibility, symbol quarantine | Later; each needs a calendar, a feed or a registry that does not exist |
| `take_profit_price` | Stays `None`. An exit thesis belongs to the strategy, not to risk |
| `order_check` before submission, the intent ledger | The execution phase |
| `tighten_stop` and the trailing engine | The position guard, execution phase |

## 9. Handoff to execution

The execution phase receives a decision that is either executable or refused,
never degraded: an `ApprovedRiskDecision` or `ResizedRiskDecision` always
carries a positive quantity on the lot grid, a positive stop price on the tick
grid, and a `risk_money` that is what will actually be lost if that stop is hit.

What execution must supply itself: the `MarginPort` implementation, the
`order_check` round trip before submission, the intent ledger and its
idempotency, and every gate excluded in §8 of this spec that reads book state — including the
martingale prohibition, which cannot be enforced until there is a prior closed
trade to compare against.
