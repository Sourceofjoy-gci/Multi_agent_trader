# Phase 8B2a — Cost Correctness and Per-Trade Attribution Design

**Status:** approved design, pending plan
**Date:** 2026-09-27
**Predecessor:** Phase 8B1, Mark-to-Market Equity and Canonical Daily Returns
**Umbrella:** `docs/superpowers/specs/2026-09-25-phase-8-validation-design.md` (Phase 8)
**Implements:** umbrella §6.3 in full, and the cost mechanics of §6.4

Section references are to the **umbrella design** unless prefixed with "this spec".

## 1. What this slice is for

Every cost number this system currently produces is a total, and one of them is
mislabelled. `SimulatedTrade.gross_pnl` is gross of commission and swap but
already contains spread and slippage, because the fill model charges both into
the entry and exit prices — so the field's name oversells it, and
`research/backtest/result.py:45-52` says so at length.

That leaves three consequences this slice removes:

1. **No decomposition.** A reader can see what a trade earned and what it paid
   in commission and swap, but cannot see what it paid in spread and slippage,
   because those are folded into two prices and discarded.
2. **A live defect in the stress knob.** `costs.py:46-51` documents it plainly:
   stressing a *positive* carry increases profit, because `swap_cost`
   multiplies the signed rate by the multiplier. A strategy that earns carry
   therefore clears the section 12 gate more easily at 2x than at 1x.
3. **Spread cannot be stressed at all.** It is observed from `bar.spread` and
   never declared, so no multiplier reaches it, and §6.4 requires the observed
   spread cost to scale.

8B2a fixes the cost model and makes the decomposition evidence. It seals a
per-trade attribution beside the mark-to-market series, and makes
`CostSummary` a *checked aggregate* of that detail rather than a separately
asserted number. The three-scenario rerun is 8B2b.

### 1.1 What this slice does not do

- No 1.0x/1.5x/2.0x orchestration, no comparison report. That is 8B2b, which
  consumes the sidecar this slice seals.
- No compounding rerun (umbrella §6.5), no capacity diagnostics (§4.1).
- No field on `BacktestResult` or `SimulatedTrade`; no new dependency; no
  migration; no ledger event type; no new CLI option or command.

## 2. Decisions this slice makes

| # | Decision | Why |
|---|---|---|
| C-1 | The attribution is a sidecar parallel to `result.trades`, not a field on `SimulatedTrade` | §6.3's literal wording is "new result versions store, per trade". Fields on `SimulatedTrade` would move `BacktestResult.digest()` for every run, which invalidates four pinned digest constants, breaks 20+ test call sites, and makes the v1 legacy bundles unreadable — the wall 8B1 spent a sidecar to avoid. |
| C-2 | The attribution is sealed in the bundle, coupled to `costs.status == COMPLETE` | §6.3's point is that prospective evidence carries a complete attribution. `PARTIAL` with two `None`s is a legacy limitation, not a design choice, so no flag gates it: the components are already computed during the fill. |
| C-3 | Reconciliation is per trade, then totals, then the series transitively | A bar's equity change includes unrealized movement priced at the mid close, so a per-bar cost decomposition is not well defined against a `MARK_TO_MARKET` series. Asserting one would claim something the basis does not. Every link of the real chain is a separate assertion. |
| C-4 | The stress multiplier scales the observed spread **at fill time** | A stress the run does not pay is not a stress. Scaling in the attribution alone would leave all three scenarios with an identical result and digest, so the promotion gate would read a figure no run incurred — the substitution of a computed value for a measured one that `PARTIAL` exists to avoid. |
| C-5 | `CostModel` gains no field | `tests/acceptance/test_phase6.py:54` and `:195-201` permit exactly one defaulted field, and `:204-216` require the command to demand every cost the model demands. Spread is observed, not declared, so the multiplier belongs where the observation happens. |
| C-6 | The shared `run_id` is documented, not fixed | The fix is closed, not merely inconvenient — see §5. |

## 3. The three engine changes

All three are in the engine's cost path. None touches `BacktestResult`,
`SimulatedTrade` or `CostModel`'s field set.

### 3.1 Stressable spread

`entry_fill` (`fills.py:54-55`) currently computes
`half_spread = Decimal(bar.spread) * contract.point_size / 2`. It multiplies by
`model.stress_multiplier` first. `_closing_fill` (`engine.py:681-689`) reuses
`entry_fill` on the opposite side, so it inherits the scaling unchanged.

`resolve_exit` (`fills.py:89-104`) is **not** touched. A stop or target exit
crosses no spread at all, and that asymmetry is the model's real behaviour
rather than an oversight to smooth over: §6.3 has to attribute what is charged,
not what would be conventional.

At `m = 1` the change is `Decimal(x) * 1`, which is bit-identical to today, so
no result digest moves.

It is worth being precise about *why*, because the obvious statement is false:
several existing tests do run stressed — `test_costs.py:93` at `m = 2` and
`:262` at `m = 3`. Every one of them uses a **negative** swap rate, and the
charge branch of §3.2 is the branch today's code already implements, so they
survive unchanged. The one test that reaches the short side
(`test_costs.py:304`) also uses a negative rate, at `m = 1`. No existing test
stresses a *positive* carry, which is precisely the uncovered case §3.2 exists
to fix — and that is why the defect in `costs.py:46-51` survived this long with
a green suite.

### 3.2 The swap-credit rule

`swap_cost` (`costs.py:135-147`) selects a signed rate by side and then
multiplies it by the multiplier unconditionally. §6.4's rule is piecewise in
the sign of that selected rate:

- a negative rate is a **charge**: it becomes `m` times more negative, so
  `rate * m`;
- a positive rate is a **credit**: it is reduced by `(m - 1) * abs(rate)`, so
  that stress never increases carry — which is `rate * (2 - m)`.

Two properties make this the right shape rather than a special case bolted on:

- at `m = 1` **both branches reproduce the unstressed value exactly**, which is
  §6.4's "At m = 1, every component exactly matches baseline";
- the rule is total across the field's whole domain, because `m > 0` is the
  only bound: below 1 a credit grows (the legitimate "what if costs are lower
  than assumed" probe the `CostModel` docstring already invites), at 1 it is the
  baseline, at 2 it is zero, and above 2 it becomes a charge.

The `CostModel` docstring at `costs.py:46-51` currently documents stressing a
positive carry as an accepted consequence of one multiplier over the whole
model. That paragraph becomes false and is rewritten to state the rule and
where it lives, rather than left to contradict the code it describes.

### 3.3 Per-leg capture

The attribution has to exist **before the bar is gone**. `_trade`
(`engine.py:691-721`) receives a `_Position` and a resolved `Exit`, and from
those it can recover what each leg charged only if the fill functions told it.
So `entry_fill`, `_closing_fill` and `resolve_exit` each report the raw price
they used *before* any spread or slippage, alongside the fill, and the engine
assembles the components from the two legs at `_trade`.

`market_pnl` is the raw move, priced through the existing `_gross_pnl`
conversion with the two raw prices. It does **not** scale with the multiplier:
the market's move is a fact about the run, and a stress that stretched it would
be a different claim. `spread_cost` and `slippage_cost` are what the fills
actually charged, so they do scale. All three are **money**, not prices:
`spread` and `slippage` are observed as price deltas and are converted by
`lots * value_per_price_increment / price_increment`, exactly as `_gross_pnl`
converts a price move — the same conversion, the same single division running
last, so no component acquires a rounding boundary the total does not have.

The decomposition is exact by construction, at the price level before that
conversion. For a buy, the entry fill is `bar.open + half_spread + slip` and a
stop exit is `raw - slip`, so `gross = market - spread - slippage` with no
residual; for a sell the signs invert and it cancels the same way; for a time
exit, which crosses a second half-spread from its own bar, that extra term
appears on both sides of the identity. Because one conversion is applied to all
four quantities, the price-level identity survives it.

## 4. The attribution model

```python
class TradeCostAttribution(CanonicalModel):
    proposal_id: NonEmptyStr
    market_pnl: Decimal
    spread_cost: Decimal
    slippage_cost: Decimal
    post_fill_gross: Decimal
```

`post_fill_gross` is stored even though it is derivable, because §6.3 names it
and because a stored value that must equal its own derivation is a *checked*
duplicate rather than a silent one. That is the pattern 8B1 established when
`BacktestOutcome` bound a series to a result.

The tuple rides `BacktestOutcome` beside `equity`, and the sealed bundle beside
`mark_to_market`. It is **defaulted and excluded when absent**, for the same
reason `mark_to_market` is: `EvidenceStore.read` re-serializes what it decoded
and refuses any document whose bytes are not today's canonical encoding, so a
field that always wrote itself out would put `"cost_attribution":null` into
every bundle and break the v1 documents already sealed in an operator's store.

### 4.1 What the bundle enforces

In order, so a malformed document is refused for the first reason that applies:

1. attribution present **iff** `costs.status == COMPLETE` — the coupling shape
   8B1 uses for `mark_to_market` against its basis;
2. `len(attribution.trades) == len(result.trades)`, and the `proposal_id` at
   every index is the one `result.trades` carries there, so the parallel tuple
   cannot drift out of order;
3. per trade, `market_pnl - spread_cost - slippage_cost == post_fill_gross`;
4. per trade, `post_fill_gross == result.trades[i].gross_pnl` — the existing
   number, decomposed, checked rather than restated;
5. totals: the summed `spread_cost` and `slippage_cost` equal the two fields on
   `costs`, and `costs.commission` and `costs.swap` equal the sums over
   `result.trades`.

`net_pnl == post_fill_gross - commission + swap` needs no new assertion:
`SimulatedTrade` already refuses a net its own terms do not produce
(`engine.py:720`, `result.py`).

### 4.2 The series, transitively

The chain is: per-trade components → `post_fill_gross` → `result.trades[i].gross_pnl`
→ `result.net_pnl` → a flat run's final equity, and `BacktestOutcome` already
asserts that last link. So "totals reconcile to the result **and** the
mark-to-market series" is satisfied by three separate assertions rather than one
narrative, and without asserting anything a mid-price valuation does not claim.

### 4.3 Legacy

The importer builds no attribution, so legacy bundles keep `PARTIAL` with both
unknown components `None` and are refused by rule 1 if one ever appeared. §6.3's
"the legacy adapter must not invent a numeric residual or write either unknown
as zero" holds by construction rather than by vigilance: there is no code path
that could produce a number for a Phase 7 artifact, whose bar store was deleted
and which therefore can never be re-derived.

## 5. Three options the code closes

**Adding `cost_model` to the run-id identity is impossible, not merely
inconvenient.** `_run_id` (`engine.py`) derives from the request but omits
`cost_model` and `firm_equity`. That is already a defect — `--stress-multiplier 2`
today produces a different result under the same `run_id` — but the fix is
closed: `research/legacy_import.py:195-196` refuses any artifact whose `digest`
differs from `result.digest()`, and `run_id` is inside the model, so a changed
identity would make every Phase 7 artifact fail its own import. The scenario
identity therefore rides the attribution sidecar, and the shared `run_id` is
documented in the design and the README as an inherited Phase 6 defect with its
reason stated.

**Adding fields to `SimulatedTrade`** is closed by the four pinned digest
constants and by the v1 legacy bundles, which a required field would make
unreadable.

**Adding a `CostModel` field** is closed by the two Phase 6 acceptance tests
named in C-5.

## 6. Honest limits this slice carries

- The attribution explains where the cost went. It does not make the cost
  model *correct* — the spread is a mid-price half-spread the store observed,
  not a depth-aware fill, and MT5 does not provide the depth data that a better
  model would need.
- Scaling the spread at fill time changes the *prices*. It does not, on today's
  code, change the trade sequence: triggers test raw bar prices, stops come from
  the risk engine off the entry, and the time deadline is event-time based. The
  rerun is still the right method, because it is the only claim that stays true
  if that ever changes — but no one should expect three scenarios to produce
  different trade sequences, and 8B2b must not report that as a finding.
- An attribution is evidence about a *declared* cost model. The rates are the
  operator's, the ledger preserves them, and the chain does not vouch that they
  match any broker's.

## 7. Testing

**Known answer.** `tests/unit/research/backtest/test_engine.py:81` pins
`gross_pnl == Decimal("3.37")` from arithmetic a reader can check by hand. 8B2a
extends it to assert `market_pnl - spread_cost - slippage_cost` reconstructs
`3.37` exactly. A fill-model regression fails a hand-computed number.

**Spread asymmetry.** A time-stopped trade crosses spread on both legs; a
stopped trade only on entry. Both asserted, because this is the model's real
behaviour and the most likely thing for a later reader to "fix".

**Swap-credit rule.** Four cases on `swap_cost`: a charge scaled by `m`; a
credit scaled by `(2 - m)`; `m = 1` reproducing the unstressed value in **both**
signs, which is §6.4's baseline-equality property; and `m > 2` turning a credit
into a charge.

**No digest moves.** A `m = 1` run reproduces the known-answer `gross_pnl` and
the known `run_id`; the three existing stressed tests
(`test_costs.py:93`, `:262`, `:304`) still pass, because each uses a charge;
and a **positive** credit at `m = 1.5` is the first thing this slice has to get
right with no prior test to lean on. `_CANONICAL_BUNDLE_SHA256`
(`tests/property/test_trial_evidence.py:198`) stays put, because that bundle
omits both new fields — it is the same `exclude_if` regression witness 8B1
established.

**Coupling.** All four directions of attribution-present-iff-`COMPLETE`, and both
directions of the per-trade identity, each with the equal-length and
`proposal_id` checks.

**Legacy.** The v1 bundles still read, still verify, and the importer still
refuses a tampered digest.

**Property.** Over generated trades, the three components reconstruct
`gross_pnl` exactly — the same falsifiable shape 8B1's property suite was
corrected to, asserting a *mutated* series is refused rather than that a valid
one is accepted.

## 8. Definition of done

8B2a is complete when:

- a 1.0x run is bit-identical to today's, in every result digest and the known
  `run_id`;
- a 1.5x run's fills cross 1.5x the half-spread on the legs that cross spread
  at all, and a stop or target exit still crosses none;
- `m = 1` reproduces the unstressed swap in both signs, and a positive credit
  never grows under `m > 1`;
- every trade's `market_pnl - spread_cost - slippage_cost` equals its existing
  `gross_pnl`;
- the summed components equal `CostSummary`'s fields, which equal the sums over
  `result.trades`;
- a prospective bundle reports `costs.status == COMPLETE` with all four
  components present;
- a legacy bundle still reports `PARTIAL` with two `None`s and still verifies;
- `_CANONICAL_BUNDLE_SHA256` and the three Phase 7 result digests are unchanged;
- no new field on `BacktestResult`, `SimulatedTrade` or `CostModel`; no new
  dependency, migration, ledger event type, CLI option, or command.

## 9. What follows

**8B2b** — umbrella §6.4's orchestration: the 1.0x/1.5x/2.0x reruns, their
sealing, and the comparison the promotion gate will eventually read. It consumes
the sidecar this slice seals, and inherits the shared-`run_id` limit from §5.

Then **8B3** — §6.5 compounding and capacity diagnostics. Each is a separate
spec → plan → implementation cycle, per the umbrella's §1.
