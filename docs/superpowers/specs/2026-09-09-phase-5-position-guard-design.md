# Phase 5 — The Position Guard

**Status:** approved design
**Date:** 2026-09-09
**Predecessor:** Phase 4, the execution plane (`docs/superpowers/specs/2026-09-07-phase-4-execution-submission-design.md`)

Section references are to the **master specification**
(`mt5-multi-agent-trading-house-spec.md`) unless prefixed with "this spec".

## 1. What this phase is for

Phase 4 can open a position exactly once. Nothing yet watches it afterwards.

The guard makes one promise: **every position this system opened is, at every
moment, carrying the stop the ledger says it carries** — and if it is not, the
guard notices within a second and restores it.

That is the whole phase. Not trailing, not exits, not profit-taking: the
narrow, unglamorous property that a live position is never quietly
unprotected.

## 2. Scope

The master spec's §9 bundles the guard with breakeven, ATR trailing, structure
trails, time stops and regime exits. **This spec covers the protection half
only.**

§9.2 is explicit about why:

> trailing stops do not universally improve expectancy... on scalping horizons
> tight trails frequently *degrade* expectancy by converting winners into
> scratches. Every strategy must A/B test trail-vs-fixed-target in the
> backtest harness... **Do not apply a global trailing policy.**

No strategy exists yet. Building trailing now means choosing multipliers on
behalf of a caller that does not exist and cannot yet produce the evidence the
spec demands — the same reasoning that kept `BookState` out of Phase 3 and the
daemon out of Phase 4.

What ships is what protects the positions Phase 4 can now open, and it is
needed whatever any future strategy decides.

## 3. Decisions

| | Decision | Why |
|---|---|---|
| **D-1** | **A supervised daemon**, one cycle per second. | §13.3's premise is a ≤1s cycle. A stop checked only when someone types a command is not a guard. This is the phase that genuinely needs the loop, which is why Phase 4 deferred it here rather than building it speculatively. |
| **D-2** | **Position state is append-only, written only on material change.** | Every table in this database is append-only by grant. Writing every cycle would be 86,400 rows per position per day and would bury the events that matter; writing on change keeps the trail readable — an incident review wants to see when the stop moved, not that it was checked 86,000 times. |
| **D-3** | **The monotonic guarantee is enforced twice, and each layer gets a test that reaches it.** `decide()` cannot emit a widening; `amend_protection` refuses one. | They stop different things: the first stops the policy asking, the second stops anything succeeding. Written naively the outer check masks the inner, and only one is ever exercised — which is exactly how Phase 4's ledger came to claim enforcement by grant *and* trigger while proving only the grant. |
| **D-4** | **`decide()` is pure** over `(observed, recorded, contract, default_stop_distance, now)` and returns one of a closed set of actions. | A vanished position, a stop the broker moved underneath us, a retraced price, an orphan with and without a stop — all become table-driven tests with no broker and no clock. The shape that worked for Phase 4's `verdict()`. |
| **D-5** | **An orphan is adopted, never invented around and never liquidated.** Keep its broker stop if it has one; otherwise compute one from an adoption distance supplied by the composition root — the book's signed `k_sigma` against current ATR, floored by the broker minimum — then escalate per §5.2. **Where no distance is supplied for that instrument, the orphan escalates rather than being adopted at a fabricated one, and Phase 5 ships with none wired:** nothing feeds an ATR to the daemon, and a distance computed once at startup and reused for days would be exactly the invented number this decision forbids. | A stop computed by the system's own signed rules is not a fabricated number — it is the distance those rules would have chosen. Closing instead would have a machine make a money decision about a position it does not understand, and if the cause is a restored backup it liquidates good positions the ledger merely forgot. |
| **D-6** | **The guard is NOT gated by the unresolved-intent check.** | `require_clean_ledger` refuses to *open* while something is unresolved. A guard that stopped protecting existing positions for an unrelated stuck intent would abandon real money at the worst possible moment. The guard never opens anything. |
| **D-7** | **Two failed restore attempts escalate; they do not auto-close.** | §13.3 says "consider market-closing". A machine liquidating because it could not write a stop is worse than the exposure it removes — and a broker that will not accept an SLTP will most likely not accept a close either. |
| **D-8** | **MAE and MFE are sampled, and the spec says so.** | The guard reads the tick once per cycle, so a spike between cycles is invisible to it. Adequate for deciding whether to act now; research-grade excursion figures must be recomputed from bar data rather than trusted from this stream. |

## 4. Boundaries

```
execution/
  guard.py       decide() — pure; observed + recorded + contract -> GuardAction
  positions.py   the append-only position event store
  loop.py        the daemon cycle: read, decide, act, record
```

`execution/` imports `core/` and `database/` and **nothing else from this
project**. That constrains one thing sharply: the orphan stop needs
`k_sigma × ATR`, and ATR lives in `features/`. So `decide()` does not compute
it — the daemon's composition root does, and passes it in as a `Decimal`,
exactly as Phase 3's risk engine takes ATR as an argument rather than reaching
for it.

### What already exists and is not rebuilt

- `PositionState` with its five-state lifecycle, `initial_risk_distance`,
  `r_multiple_open`, `mae_r`, `mfe_r`, `venue_ref`
- the intent ledger, the reconciler, and `reconcile()` returning matched
  positions
- `Gateway.mark_stale()`

**R-multiples are `FiniteFloat`, not `Decimal`.** That is a deliberate Phase 0
choice: they are analytics, not money. The stop itself, and every price, stays
`Decimal`.

### What changes outside `execution/`

| Change | Where |
|---|---|
| `amend_protection()` implemented, refusing a widening stop | `brokers/mt5/adapter.py` — the last stubbed method |
| the SLTP request mapping | `boundary.py`, **not** `terminal.py` |
| a deal's entry direction (open versus close) | `core/venue.py`, `boundary.py` — see §7 |
| `execution.position_events` | new migration `0006` |
| `guard run`, `guard status` | `cli.py` |

**`terminal.py` is at 79 of an enforced 80-statement cap.** The SLTP call must
be call-and-delegate with its mapping in `boundary.py`. Do not raise the cap.

## 5. The cycle

Each cycle: read every open position from the broker, read what the store last
recorded, call `decide()` per position, execute what it returns, and append a
row **only if something changed**.

The action set is closed, which is what makes the loop testable:

| Situation | Action |
|---|---|
| stop present and matches the record | nothing — no write |
| stop present, record disagrees | record the broker's value; the broker is the truth |
| stop missing (`sl == 0`) | restore immediately to the recorded stop |
| restore failed twice | escalate; stop trying |
| orphan carrying a stop | adopt at that stop, escalate (§5.2) |
| orphan with no stop, adoption distance supplied | adopt at `k_sigma × ATR` floored by the broker minimum, escalate (§5.2) |
| orphan with no stop, no distance supplied (Phase 5's shipped state) | escalate; adopt nothing (D-5) |
| position gone from the broker | it closed — record CLOSED against its closing deal |
| a better stop is available | tighten to it |
| candidate worse than current | **nothing — this is normal, not an error** |

That last row is why the widening guard is never approached in ordinary
operation: when price retraces, a candidate is legitimately worse, `decide()`
returns no action, and nothing reaches the adapter at all. The guard exists for
the case where something is actually wrong.

### 5.1 R arithmetic

`initial_risk_distance` is fixed at entry and never recomputed — that is what
makes MAE and MFE comparable across trades, and §8.2 requires it.
`r_multiple_open` is the signed distance from entry over that distance; MAE is
the most adverse value observed, MFE the most favourable.

### 5.2 Escalation, concretely

Two mechanisms this spec names do not exist, and saying so is the point.

**There is no safe-mode subsystem.** §13.2 describes one; nothing implements
it. What exists is `Gateway.mark_stale()`, which marks gateway state untrusted
until a successful reconcile clears it.

**There is no alerting.** No pager, no notification channel, nothing that
reaches a human who is not looking. What exists is the hash-chained audit
ledger (`audit.repository.append`) and the gateway's optional `_on_event` hook.

So "escalate" means exactly this, and nothing more: call `mark_stale()`, append
a canonical event to the audit ledger, append the position event, and **stop
attempting modifications** — per §9.2's "broker or data failure → rely on
broker-native SL; do not attempt modifications".

**§9.2's reasoning does not transfer to the escalations that stop the guard
working, and the first draft of this section wrongly assumed it did.** Every
`ActionKind.ESCALATE` `decide()` can emit — "orphan has no stop and no
distance", "no stop anywhere to restore", "restore already failed twice" —
requires `observed.stop_loss is None`. (`ADOPT_ORPHAN` also escalates, and
there §9.2 does hold: that path is reached precisely when the broker has a
stop, which is the one being adopted. It is not terminal and the guard keeps
working the position.) There is
no broker-native SL to rely on: that is *why* we escalated. So after escalating,
the position may be carrying **no stop at all**, and a human is the only
remaining protection.

D-7's refusal to auto-close still stands, but on its own terms — a machine
liquidating because it could not write a stop is worse than the exposure it
removes, and a broker that will not accept an SLTP will most likely not accept a
close either. It does not stand on a broker-side stop that is not there.

**An operator learns about it by reading the audit ledger or running
`guard status`.** Wiring a real notification channel is out of scope here, and
this spec must not be read as promising one — a design that says "alert" while
the code only writes a row is the defect that produced the previous phase's
Critical.

## 6. The daemon

It owns start, one cycle per second, and shutdown. On shutdown it records that
it stopped, so a gap in the event stream is explainable rather than mysterious.

**It does not restart itself.** Supervision belongs to the operating system; a
process that resurrects itself after an unexplained death is how a broken guard
runs unnoticed for a week.

## 7. What this phase must close from Phase 4

**A deal carries no entry direction.** `DealRecord` has no equivalent of MT5's
`DEAL_ENTRY_IN` / `DEAL_ENTRY_OUT`, so a closing deal and the original opening
deal are indistinguishable. Phase 4 could carry that; this phase cannot, because
recording a close *is* matching a closing deal — and today a closed position
reconciles as two matches and wedges permanently under D-7 of the previous
phase, with no override command and an append-only table. (That is the previous
phase's two-matches rule, not this spec's D-7.)

This is the one inherited obligation that blocks the guard. The other two —
ticket adoption and `reconcile()`'s production caller — were closed by Phase 4's
final fix wave.

## 8. New invariant

**I-21** — *Every open position is verified against its recorded protection at
least once per cycle, and a missing stop is restored, or escalated after two
failed restore attempts.*

(The earlier wording said "escalated within two cycles", which is off by one
against D-7's own rule: the guard attempts a restore on cycles 1 and 2 and
escalates on cycle 3. D-7 is right and the invariant's wording was wrong.)

I-8's stop-widening prohibition is not new. Like I-6 in Phase 4, this phase is
its enforcement.

## 9. Testing

**Two tests carry this phase.**

**The monotonic property.** §15's `test_stop_monotonic`. Over generated price
paths and candidate stops, no sequence of cycles may move a stop away from
profit: the recorded stop is non-decreasing for a BUY and non-increasing for a
SELL, across the entire sequence. A property test, because the failure mode is a
*sequence* of individually plausible steps.

**Restoration.** A fake that zeroes the broker's stop mid-flight. The guard must
restore it on the next cycle, and escalate rather than loop if the restore keeps
failing.

Beyond those, and required by D-3: **each enforcement layer gets a test that
reaches it** — one proving `decide()` cannot emit a widening, and one handing
`amend_protection` a widening stop directly and proving it refuses. A test that
only exercises the outer layer proves nothing about the inner one.

Also: an orphan with and without a stop; a position vanished from the broker
recorded CLOSED against its closing deal; the daemon shutting down cleanly and
recording that it did; and an acceptance test pinning `execution/`'s imports.

## 10. Deliberately excluded

| Item | Where it lands |
|---|---|
| Breakeven, ATR/Chandelier trailing, structure and channel trails | With the first strategy, which must A/B test it (§9.2) |
| Time stops, regime exits, scale-outs, take-profit management | Same |
| Closing a position on the guard's own initiative | Nowhere — the guard protects, it does not trade |
| A safe-mode state machine | §13.2, when its triggers exist |
| Self-restarting supervision | The operating system's job (§6) |

## 11. Handoff to the strategy layer

A strategy receives positions that are continuously verified as protected, with
`initial_risk_distance` fixed at entry and MAE/MFE tracked in R, and a
`tighten_stop` path it can drive with its own policy.

What it must supply itself: the trailing policy, its A/B evidence against a
fixed target, and the exit conditions — none of which this phase presumes to
choose.
