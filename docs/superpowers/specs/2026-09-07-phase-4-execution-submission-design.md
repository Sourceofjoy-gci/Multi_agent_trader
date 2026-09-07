# Phase 4 — The Execution Plane: Submission and Reconciliation

**Status:** approved design
**Date:** 2026-09-07
**Predecessor:** Phase 3, risk and position sizing (`docs/superpowers/specs/2026-09-06-phase-3-risk-sizing-design.md`)

Section references are to the **master specification**
(`mt5-multi-agent-trading-house-spec.md`) unless prefixed with "this spec".

## 1. What this phase is for

Every phase so far has been reversible. This one is not: it is the first code
in this project that changes state at a broker.

Phase 3 produces a decision — a lot size and a protective stop. Phase 4 turns
that decision into a position, exactly once, and refuses to do anything else
until it knows what happened to every decision that came before.

**One guarantee and one gate.**

- **The guarantee:** an approved decision becomes at most one position, ever.
  No lost response, timeout, or process death can produce a second. This is
  invariant I-6, which already exists; this phase is what enforces it.
- **The gate:** no new order is sent while any earlier intent is unresolved.
  That is new, and it is I-20.

§3.6 calls idempotency "the hardest MT5 problem" for a specific reason: MT5 has
no client order ID, so a timed-out `order_send` is genuinely ambiguous. The
request may have been lost on the way out, or executed with the answer lost on
the way back. Retrying resolves the first case and doubles the position in the
second, and nothing in the response distinguishes them.

## 2. Scope

The master spec's roadmap bundles the intent ledger, order manager,
reconciliation, position guard and trailing engine into one phase. **This spec
covers the first three only.** Getting a position open exactly once is a
durability and idempotency problem; guarding and trailing it is a control-loop
problem. They share little machinery, the second is meaningless without the
first, and the first deserves to be proven on a demo account before a loop
starts modifying live stops.

The position guard, the tighten-only stop API, trailing policy and exit paths
are the next phase.

## 3. Decisions

| | Decision | Why |
|---|---|---|
| **D-1** | **CLI commands, not a daemon.** The "hard startup gate" of §3.6 becomes a precondition on **every** order-placing invocation. | Submitting an order is a discrete act. Running the gate per invocation is strictly stronger than once per process, and simpler to prove. The daemon belongs to the phase that needs a loop: trailing must react between keystrokes, submission need not. |
| **D-2** | **The intent ledger is an append-only event table.** A state change is a new row; current state is the latest row per intent. | Every other table in this database is append-only by grant rather than by discipline. A mutable state column would need an UPDATE grant no other table has, and would destroy the one record an incident review needs: not that an intent is UNKNOWN, but when it became so and what it passed through. |
| **D-3** | **`submit()` never reconciles.** It writes SUBMITTING, sends, and records exactly one of CONFIRMED, REJECTED or UNKNOWN. A separate reconciler owns every non-terminal intent, and the startup gate runs that same reconciler. | Recovering a lost response and recovering after a crash are the same problem. Solving it in two places means two implementations of the hardest logic in the phase — and the copy that runs at startup would be the one never exercised in normal operation. |
| **D-4** | **The reconciler's verdict is a pure function** of `(intent, deals, positions, now)`. | Every hard case — a deal arriving 40 seconds late, colliding magics, a partial fill — becomes a table-driven test with no broker and no clock. |
| **D-5** | **Fakes carry correctness; one opt-in live test proves the real path.** | The property that matters most cannot be produced against a real broker: you cannot make MT5 return `None` *after* executing. Only a fake can stage the case the whole phase exists to survive. |
| **D-6** | **No automatic retry.** A rejection records its `RecoveryAction` and returns. | A requote's correct recovery is a *fresh* intent, which is a new submission rather than a resend — safe, but it means one invocation could place two orders. Every retry is another chance to lose a response, and no strategy loop exists yet to weigh that. |
| **D-7** | **Two matches is not a match.** An ambiguous reconciliation returns STILL_UNKNOWN and escalates; it never picks one. | Adopting the wrong deal attaches our ledger to someone else's position. Staying unresolved is strictly better than being confidently wrong about which position is ours. |
| **D-8** | **A partial fill is CONFIRMED at the filled quantity**, with the shortfall recorded. | Chasing the remainder and re-running risk on it needs a position model, which is the next phase. Recording the fill honestly is this phase's job. |
| **D-9** | **R-8 closes with `max_spread_fraction_of_stop`,** a per-book fraction in the signed constitution. | It never references the median, so the zero-median blind spot cannot exist for it. It is venue- and instrument-neutral — a 30-point spread on gold and a 3-point spread on EURUSD are judged by one economic rule — and it says the thing worth saying: a trade whose cost is a large fraction of its risk is uneconomic however normal that spread is. |

## 4. Boundaries

```
execution/
  ledger.py       the append-only intent event store
  manager.py      OrderManager.submit() — write, send, classify, write
  reconciler.py   the verdict function, and the sweep the gate runs
```

`execution/` imports `core/` and `database/`. It does **not** import
`brokers/`: it declares the narrow venue port it needs, and the MT5 adapter
satisfies it structurally — the same shape as Phase 2's `BarReader` and Phase
3's `MarginPort`. An acceptance test pins the arrow.

### What already exists and is not rebuilt

- `IntentState` — SUBMITTING, CONFIRMED, UNKNOWN, RECONCILING, FAILED, REJECTED
- `OrderIntent`, `PositionState`, `ExecutionOutcome` with its consistency validators
- `RejectReason`, `RejectClass`, `RecoveryAction`, `recovery_for()`
- `derive_magic(intent_id, magic_range)` and the signed per-book magic ranges
- the retcode taxonomy in `brokers/mt5/retcodes.py`

**Magic is a locator, not an identity.** Phase 1 chose deterministic derivation
over the master spec's sequential scheme so that an intent's magic survives a
crash with no persisted mapping to lose, and its docstring is explicit that a
10,000-wide range collides by birthday at roughly 120 concurrent intents.
Reconciliation inherits that constraint: matching is by magic **and** symbol
**and** volume **and** time window, with the ledger authoritative.

### What changes outside `execution/`

| Change | Where |
|---|---|
| `submit()` and `close()` implemented | `brokers/mt5/adapter.py` — both currently raise `NotImplementedError` |
| `reconcile()` matches venue positions against the ledger | same — today every position lands in `unmatched_venue_refs` |
| `max_spread_fraction_of_stop` per book | `config/risk_constitution.yaml`, re-signed |
| the spread-fraction gate | `risk/engine.py` |
| `execution.intent_events` | new migration `0004` |
| order commands | `cli.py` |

`amend_protection()` stays stubbed. That is the position guard's, next phase.

## 5. The ledger and the protocol

### 5.1 The table

`execution.intent_events`, in its own schema, following migration `0001`'s
pattern: `REVOKE ALL` from `PUBLIC`, the runtime role granted `INSERT` and
`SELECT` only, and triggers rejecting `UPDATE`, `DELETE` and `TRUNCATE`.

Columns: a `bigserial seq`, `intent_id`, `state`, an event time in UTC, a
recording time, and a JSONB payload carrying the intent snapshot or the
outcome. Current state is `DISTINCT ON (intent_id) … ORDER BY intent_id, seq
DESC`.

### 5.2 The protocol

```
1. intent_id = uuid4();  magic = derive_magic(intent_id, book.magic_range)
2. INSERT (intent_id, SUBMITTING, full intent snapshot)  ->  COMMIT
3. outcome = venue.submit(intent)
4. INSERT exactly one of:
     CONFIRMED  -- accepted: venue_ref, fill price, filled quantity
     REJECTED   -- its RejectReason and the RecoveryAction recovery_for() gives
     UNKNOWN    -- no usable answer
   then return. Never resend. Never reconcile here.
```

**Step 2's commit returning is the durability point, and it is the whole
design.** Every crash lands on one side of it:

| Died | Ledger says | Broker holds | Reconciler concludes |
|---|---|---|---|
| before the commit | nothing | nothing | nothing to recover |
| between commit and send | SUBMITTING | nothing | FAILED, once the window closes |
| during the send | SUBMITTING | possibly a position | CONFIRMED, on finding the deal |

**SUBMITTING and UNKNOWN are reconciled identically, and both exist on
purpose.** SUBMITTING means we never lived to record an answer; UNKNOWN means
we survived and recorded that we did not get one. Operationally the same;
after an incident, that distinction is what says whether the process died or
the broker went quiet.

## 6. Reconciliation

```python
def verdict(*, intent, deals, positions, now) -> Verdict
    # CONFIRMED(venue_ref) | FAILED | STILL_UNKNOWN
```

**Two distinct durations, and conflating them is a defect.**
`MATCH_LOOKBACK` is 60 seconds: how far *before* `t_submit` a deal may have
been stamped and still be ours, covering broker clock skew.
`RESOLUTION_TIMEOUT` is 30 seconds: how long *after* `t_submit` the reconciler
keeps saying STILL_UNKNOWN before it is entitled to say FAILED. The first is
about which deals to consider; the second is about when to give up. Both are
module constants, not arguments, so no caller can shorten the timeout.

A deal matches only on **all** of: the derived magic, the server symbol, the
volume, and a deal time inside `[t_submit - MATCH_LOOKBACK, now]`.

| Situation | Verdict |
|---|---|
| exactly one matching deal or position | CONFIRMED, adopting its ticket |
| more than one candidate | STILL_UNKNOWN and escalate — never guess (D-7) |
| none, `RESOLUTION_TIMEOUT` elapsed, terminal healthy | FAILED |
| none, terminal unhealthy | STILL_UNKNOWN and escalate |

The last row matters: "I cannot see the broker" is not "the order did not
happen", and a phase that conflated them would mark live positions FAILED and
forget them.

**What "escalate" concretely means here.** There is no global safe-mode state
machine in this codebase yet — §13.2 describes one, and nothing implements it.
What exists is `Gateway.mark_stale()`, which marks gateway state untrusted
until a successful reconcile clears it, and `RecoveryAction.ENTER_SAFE_MODE`
as a classification. So escalation is: leave the intent non-terminal, call
`mark_stale()`, and let §6.1's gate refuse every subsequent order. That is
sufficient — an unresolved intent already blocks all trading — and inventing a
safe-mode subsystem here would be building §13.2 without its triggers.

### 6.1 The gate (I-20)

Every order-placing command begins by finding intents whose latest state is
SUBMITTING, UNKNOWN or RECONCILING, running the reconciler on each, and
**refusing to submit anything if any remains unresolved** — exiting non-zero
and naming the intent. The sweep writes RECONCILING before polling, so a sweep
that dies mid-poll is visible as such instead of looking untouched.

## 7. New invariant

**I-20** — *No order is sent while any earlier intent is unresolved.*
Enforced by the gate in §6.1 of this spec, on every invocation.

Exactly-once remains **I-6**, which predates this phase. Phase 4 is its
enforcement, not its introduction.

## 8. Testing

**Two tests carry this phase.**

**`test_lost_response_no_double_send`.** The fake venue executes the order and
then returns `None`. Exactly one position must exist, the intent must resolve
CONFIRMED through reconciliation, and `submit` must have been called exactly
once. Only a fake can stage this, which is why the fake is the primary
instrument and not a convenience.

**`test_restart_reconciliation`.** The fake dies between the ledger commit and
the send. A fresh manager must refuse every new order until the gate resolves
the leftover.

Beyond those:

- the ambiguous double match, asserting STILL_UNKNOWN rather than a choice
- a table-driven test that every retcode the adapter can emit maps to a
  recorded `RejectReason`, with a guard proving the map is exhaustive
- a partial fill recorded at its filled quantity
- **durability proven structurally:** an integration test that connects as the
  runtime role and asserts the database refuses an `UPDATE` on
  `intent_events`. Discipline is not evidence; the grant is.
- an acceptance test pinning `execution/`'s imports

**One opt-in live test**, behind the existing `mt5` marker and the demo guard:
open a single minimum-lot position, confirm it through the ledger, close it,
and force-close anything left open in teardown. Skipped by default, as the
live suite already is.

## 9. Deliberately excluded

| Item | Where it lands |
|---|---|
| Position guard, `tighten_stop`, trailing policy, exits | The next phase |
| `amend_protection()` | The next phase |
| Automatic retry on transient rejections | When a strategy loop exists to weigh it (D-6) |
| Chasing a partial fill's remainder | Next phase — it needs a position model (D-8) |
| Every risk check needing `BookState` | Still blocked on an open-position model |
| A daemon, supervision, heartbeats | When trailing needs a loop (D-1) |

## 10. Handoff to the next phase

The position guard receives an intent ledger in which every intent has reached
a terminal state, and a `reconcile()` that returns matched `PositionState`
rather than a list of unmatched refs. What it must supply itself:
`amend_protection()`, the tighten-only stop API, trailing policy, and the
control loop that drives them.
