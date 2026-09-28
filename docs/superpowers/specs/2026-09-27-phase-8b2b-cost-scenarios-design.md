# Phase 8B2b — Cost Scenario Orchestration and the Declared-Grid Report Design

**Status:** approved design, pending plan
**Date:** 2026-09-27
**Predecessor:** Phase 8B2a, Cost Correctness and Per-Trade Attribution
**Umbrella:** `docs/superpowers/specs/2026-09-25-phase-8-validation-design.md` (Phase 8)
**Implements:** umbrella §6.4's orchestration, over §6.3's sealed attribution

Section references are to the **umbrella design** unless prefixed with "this spec".

## 1. What this slice is for

8B2a made the cost model express §6.4 honestly and sealed a per-trade decomposition of what a run
was charged. Nothing yet *runs* the grid §6.4 requires, and nothing reads the `stress_multipliers`
a protocol preregisters.

A `TrialProtocol` declares its own cost grid: `CostSpec` carries a `baseline` and a
`stress_multipliers` tuple the validator holds to exactly `{1.5, 2}`. So the required scenario set
is `{1.0, 1.5, 2.0}`, **fixed before any result exists**. This slice runs that set, seals the three
bundles, and derives a report that refuses anything the preregistration did not ask for.

It also exposes, on real data, the thing §6.4 exists to show. Two runs of the same candidate over
identical inputs, differing only in `--stress-multiplier`, produced on the operator's own machine:

| | 1.0x | 1.5x |
|---|---|---|
| trades | 12 | 12 |
| `market_pnl` | 1091.87999999997 | 1091.87999999997 |
| `spread_cost` | 404.4 | 606.6 |
| `slippage_cost` | 16.176 | 24.264 |
| `commission` | 283.08 | 424.62 |
| `swap` | −37.744 | −56.616 |
| **`net_pnl`** | **+350.48** | **−20.22** |

A candidate that looked comfortably profitable at baseline is approximately break-even at 1.5x, on
the same twelve trades. The trade sequence did not move — same `proposal_id`s in the same order,
same lots, same 876 bars — while every price did.

### 1.1 What this slice does not do

- **No verdict.** It reports; 8D's promotion gate judges. D-7 puts the economic rule in §12 and the
  statistical cutoffs in 8C/8D, and a threshold chosen in this slice would be chosen after seeing how
  the numbers came out.
- **No new sealed artifact.** The three bundles are the evidence, and they are already sealed and
  verified. See §3.1.
- No statistical significance, no DSR, no drawdown gate (8C). No compounding or capacity (8B3).
- No new ledger event type, migration, dependency, or database privilege.
- No field on `BacktestResult`, `SimulatedTrade`, `CostModel` or `EvidenceBundle`.

## 2. Decisions this slice makes

| # | Decision | Why |
|---|---|---|
| S-1 | The comparison is a **read over three already-sealed bundles**, not a fourth document | The evidence exists and is verified. A sealed comparison would fix a number derivable from documents already under content-addressed storage, and would need a new type and a new way to reference it. |
| S-2 | The report **fails closed on evidence and reports on performance** | The preregistration gate, the fidelity checks and the identity check are all answerable now and all fail closed. Whether the degradation is *acceptable* is 8D's question, and answering it here would fix a threshold after seeing the results. |
| S-3 | Both an **orchestrator and a standalone read** | The orchestrator removes three hand-written attempt ids and a level that can be forgotten; the read lets a candidate whose runs were produced another way still be checked. Neither substitutes for the other. And per §7 the orchestrator is the only thing in the system that guarantees the three levels share one declared specification — a hand-run grid demonstrably does not. |
| S-4 | `backtest run` and the orchestrator call **one** simulation function | Two implementations of a simulation drift. The Typer signatures must differ — that is this CLI's idiom — but the computation must not. |
| S-5 | The degradation is reported as the **difference in `net_pnl`**, and the four cost components separately — never as a "total cost" | `swap` is signed. A summed total means something only given a sign convention, and the convention would be chosen by whoever wrote the sum. |
| S-6 | Three `start`s on one trial are **three audit attempts and one selection lottery**, with the effective-specification count depending on the declared digests | `trial_counters` sets `selection_lotteries` from `trial_id` and `audit_attempts` from `attempt_id`. One candidate run at three cost levels is one selection decision. Inflating the lottery count would inflate the DSR denominator by three for nothing but diligence. The specification count is a set of client-asserted digests, so it is whatever the caller declared — see §7. |
| S-7 | The identity check pins the **trade sequence**, not the prices | The prices must differ across multipliers — that is the whole stress. Pinning them would make every grid unpassable. |

## 3. Commands

### 3.1 No new sealed artifact

The three bundles are the evidence. 8A's `research trial record` already seals any bundle, and
8B1/8B2a already gave the bundle a `mark_to_market` series and a `cost_attribution`. Nothing is
added to the document; a comparison document would be a derived number stored beside the inputs it
is derived from, and the store's whole value is that a document is its own evidence.

### 3.2 `research trial scenarios` — the orchestrator

```
research trial scenarios --protocol P --trial-id T --attempt-prefix A
  --started-at S --agent-run-id R --occurred-at O --registered-at G
  <the backtest run options, minus --stress-multiplier>
```

It derives its grid from `P` — `{1.0} ∪ protocol.costs.stress_multipliers` — and for each level
`m` in ascending order:

1. `attempt_id = f"{A}-{m}"`, and `start`s an attempt at that id with `--started-at S`;
2. simulates with `stress_multiplier = m`;
3. seals the resulting bundle under that attempt.

The three are then reported. `--attempt-prefix` plus the multiplier makes the attempt ids
deterministic, so re-running the same prefix is idempotent, and the three ids are visibly one
candidate's.

`--started-at`, `--occurred-at` and `--registered-at` are stated **once** for all three: the three
runs are one operator action, and a per-level clock would make the grid harder to read for no gain.

### 3.3 `research trial scenario-report --trial-id T` — the standalone read

Reads every sealed bundle for `T`, groups them by the multiplier each one's
`result.cost_model.stress_multiplier` names, runs the four checks of §5, and reports. A candidate
whose runs were produced by hand, or by a future caller, is checked identically.

### 3.4 One simulation, two signatures

`src/trading_house/ops/backtest.py` gains a `simulate(...)` carrying the body of `backtest run`'s
`operation()`, with the stress multiplier as a parameter. `backtest run` passes its own option; the
orchestrator passes each grid level. `src/trading_house/ops/scenarios.py` gains the grid derivation,
the four checks, and the report.

Sealing is factored the same way: `ops/ledger.py` gains `seal_bundle(bundle, *, trial_id, attempt_id,
ledger)`, and `research trial record` uses it after loading a bundle from disk while the orchestrator
uses it on the bundle it just built. One seal path, two callers.

## 4. The grid

`CostSpec.stress_multipliers` is validated to be exactly `{1.5, 2}` by
`research/trial_ledger.py:132-136`, so the derived grid is always `{1.0, 1.5, 2.0}`. The derivation
reads the protocol rather than hard-coding the tuple, because the protocol is what was preregistered
and a report that ignored it would be checking its author's memory rather than the record.

The report also states the grid it *expected*, before it states what it found, so a reader sees the
preregistration and the outcome in one place.

## 5. The four fail-closed evidence checks

In order, so an incomplete candidate is refused for the first reason that applies.

**1 — Completeness.** Every declared multiplier is present exactly once. A baseline-only candidate is
refused: the point of a preregistered grid is that running less of it is a different claim.

**2 — Baseline fidelity.** The `1.0` scenario's `cost_model` equals the protocol's
`CostSpec.baseline` exactly — every field, not just the stressed ones. A quietly altered baseline is
refused.

**3 — Scenario fidelity.** Each stressed scenario's `cost_model` equals the baseline with **only**
`stress_multiplier` replaced. Commission, slippage, both swap rates, the triple-swap weekday and the
whole of every other field must be identical, because a "1.5x" run whose commission also changed is
not the declared scenario and would flatter or libel it arbitrarily.

**4 — Identity.** All three are one candidate: the same `trial_id`, the same `spec_sha256`, the same
`strategy_id` and `strategy_version`, the same `start` and `end`, the same `bars_seen`, and the same
ordered `proposal_id`s. This is the check that catches a substituted run.

Check 4's last clause is where 8B2a's documented limit earns its place. The **prices** must differ
across multipliers — that is the stress, and 8B2a measured 404.4 → 606.6 of spread on the same twelve
trades. The **sequence** must not. A report that pinned either price or sequence as "must be equal"
would be wrong in one direction or the other, so it pins the sequence and says why.

Each refusal states what it found and what it expected, because a refusal an operator cannot act on
is a refusal they will route around.

## 6. What the report says

Per scenario, from that bundle's own sealed fields and never recomputed from anything else:

`market_pnl`, `spread_cost`, `slippage_cost`, `commission`, `swap`, `net_pnl` — the first four as the
sums the bundle's validator already checked against its detail, and `net_pnl` as the result reports
it. Then, per stressed level, the **degradation**: the difference in `net_pnl` from the baseline, and
the difference in each of the four components, so a reader can see which term moved.

It does **not** print a total cost. `swap` is signed — a negative is a charge, a positive is a credit
— and on a carry-earning strategy the four components do not all move the same way under stress. The
stated relation is `net = market − spread − slippage − commission + swap`, and the components stay
separate.

The report also states plainly what it does not establish: no significance, no deflation, no drawdown,
no promotion eligibility.

## 7. The counters, and what the real chain shows

Three `start`s on one trial carry three distinct `attempt_id`s. `trial_counters`
(`research/trial_ledger.py:365-385`) sets `audit_attempts` from a set of `attempt_id`s and
`selection_lotteries` from a set of `trial_id`s, so a hand-run grid on one candidate is
**3 audit attempts and 1 selection lottery** — which is what §5.6 requires and what the machinery
already produces. `selection_lotteries` is what DSR divides by, and running one candidate at three
cost levels is one selection decision; a counter that counted three would inflate the denominator by
three for nothing more elaborate than diligence.

**`effective_specifications` needs its condition stated, and the operator's own chain is why.**
It is a set of `spec_sha256` values, and §9.1 of the 8B2a design records that this digest is
client-asserted and the ledger does not vouch it. That gap is not theoretical. On the machine that
verified 8B1 and 8B2a, `vt-1` accumulated five `execution_started` events, and one of them declared
a different specification from the other four:

| attempt | `spec_sha256` |
|---|---|
| `att-1`, `att-b1`, `att-c1`, `att-c2` | `dddd…` |
| `att-b2` | `bbbb…` |

The chain recorded what was typed, nothing compared it to the preregistered candidate, and the
counters reported **5 audit attempts, 4 selection lotteries, 5 effective specifications** for the
ledger as a whole — two specifications for a single trial, because one hand-run attempt drifted.

So: the three-scenario grid is 3/1/1 **when the three attempts declare the same specification**, and
the ledger will faithfully report 3/2 if one of them does not. This is the concrete argument for §3.2's
orchestrator being more than a convenience: deriving all three levels from one protocol and one
candidate is the only thing in the system that *guarantees* they share a declared specification. A
hand-run grid, as this slice's own verification shows, does not.

## 8. Honest limits

- **The three scenarios share a `run_id`.** 8B2a established that the fix is closed: `_run_id` omits
  the cost model, and `research/legacy_import.py:195-196` refuses any artifact whose digest is not its
  own `result.digest()`, so changing the identity would make every Phase 7 artifact fail its own
  import. The evidence is still unambiguous — `result.cost_model.stress_multiplier` and
  `result.digest()` both differ — and the report names the multiplier from the sealed field rather
  than inferring it from a digest.
- **Orchestrating three runs does not establish that the trade sequence is cost-invariant.** It is on
  today's engine, and 8B2a measured why: triggers test raw bar prices, stops come from the risk
  engine off the entry, and the time deadline is event-time based. 8B3's compounding path makes sizing
  cost-sensitive. Check 4 will catch a sequence that moves; it must not be read as claiming one
  cannot.
- **A report is not a verdict.** `net_pnl` going from +350.48 to −20.22 is a fact about two runs. Whether
  that refutes a candidate is 8D's question, with thresholds fixed in advance.
- **The attribution identifies a total, not a split between spread and slippage.** 8B2a pinned that
  limit; this report inherits it, and a per-level comparison of the two components separately is
  correspondingly weaker than a comparison of either against the total.

## 9. Testing

**The grid is preregistration-driven.** A protocol with a mutated `stress_multipliers` is refused by
`CostSpec` before the report sees it, and a report given a candidate whose bundles are missing a
declared level refuses on completeness. Both are the same test seen from two sides.

**Each of the four checks has a failing case**, and each is proved by removing the check rather than by
inspecting it. Check 3 in particular: a "1.5x" scenario whose commission also changed must be refused,
because that is the substitution that would flatter a candidate arbitrarily.

**The counters.** A test that three `start`s on one trial yield 3/1/1, next to the existing Phase 7
assertion of 3/3/3 for three candidates. If the two ever converge, one of them is wrong.

**The degradation is the difference in `net_pnl`**, computed from two sealed bundles and never
recomputed from either's inputs. A test that the reported 1.5x degradation equals the two bundles'
`net_pnl` difference, on the real fixture.

**No signed total.** A test that the report has no "total cost" key, and that `swap` is reported
signed, so a carry-earning candidate cannot be summarised into a number that hides the credit.

**Identity holds across the real grid.** A test that the three orchestrator-produced bundles pass all
four checks, and that a bundle from a *different* trial injected into the set is refused by check 4.

**The orchestrator and `backtest run` agree.** The orchestrator's `1.0` scenario and a hand-run
`backtest run --stress-multiplier 1` over identical inputs must produce the same
`result.digest()`. That is the S-4 guarantee tested, and it is the test that would fail if the two
commands ever grew separate simulation bodies.

## 10. Definition of done

8B2b is complete when:

- the grid is derived from the protocol, never hard-coded;
- a candidate missing any declared level is refused;
- a baseline that is not the declared `CostSpec.baseline` is refused;
- a stressed scenario differing in anything but the multiplier is refused;
- a set from more than one trial, or with a different trade sequence, is refused;
- the orchestrator's `1.0` scenario and a hand-run `backtest run --stress-multiplier 1` produce the
  same result digest;
- three `start`s on one trial report 3 audit attempts, 1 selection lottery, 1 effective
  specification;
- the report states the four components and `net_pnl` per level, and the degradation as a difference
  in `net_pnl`, with no signed total anywhere;
- no verdict, threshold, or promotion claim appears in the report or its documentation;
- no new sealed artifact, event type, migration, dependency, or model field.

## 11. What follows

**8B3** — umbrella §6.5's compounding rerun through the real risk engine, and the capacity
diagnostics with an explicit unavailable state. The compounding path makes sizing cost-sensitive, so
it is also the first thing that could move a trade sequence across multipliers, which check 4 is
already watching for.

Then **8C** (statistical validation) and **8D** (promotion and operator workflow), each a separate
spec → plan → implementation cycle per the umbrella's §1.
