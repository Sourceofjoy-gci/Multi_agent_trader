# Phase 8B3 — Compounding Rerun, Capacity Diagnostic, and the 8B2 Defects Design

**Status:** approved design (autonomous /goal run), pending plan
**Amended during review (2026-09-30):** see the dated note at the end of this document.
**Date:** 2026-09-30
**Predecessor:** Phase 8B2b, the declared cost grid
**Umbrella:** `docs/superpowers/specs/2026-09-25-phase-8-validation-design.md` (Phase 8)
**Implements:** umbrella §6.5 (compounding), §7.7's capacity clause as a typed `UNAVAILABLE` state, and closes
three of the four recorded defects (the fourth, ledger-level specification vouching, is its own slice, 8A.1).

Section references are to the **umbrella design** unless prefixed with "this spec".

## 1. What this slice is for

§6.5 says constant-notional execution stays the canonical edge and comparability path, and that a
**separate** compounding rerun supplies current equity to the real risk engine for position sizing,
"preserving all constitutional caps", records its own mark-to-market series and "cannot replace
constant-notional expectancy evidence". §7.7 then gates on the compounding baseline's drawdown, so 8C
needs that evidence to exist, sealed, before it is built against it. §7.7/§7.8 also make capacity a
mandatory typed diagnostic that is `UNAVAILABLE` (and blocking) without a declared volume-to-lots model;
8D's ninth gate consumes that type, so it has to exist as a type first.

### 1.1 What this slice does not do

- **No verdict and no threshold.** Reports, like 8B2b's. Drawdown limits, survival, "is compounding
  better" are 8C/8D's, with thresholds fixed in advance.
- No statistics, no numpy, no new ledger event type, no migration, no new dependency.
- No change to constant-notional semantics at the **1.0x** level: every existing 1.0x run id, result
  digest and bundle digest is byte-identical (the pinned digests, the known-answer v1 bundle digest).
  Stressed levels' `run_id`s change by design (C-4), and so do their result and bundle digests.
- No ledger-level change. `spec_sha256` vouching at append is slice 8A.1.

## 2. The four defects, and which slice owns each

The task named "the four known defects recorded in README.md and progress.md". Those documents state four
limits each with a stated remedy, and this run reads them as:

| # | Defect (where recorded) | Owner |
|---|---|---|
| 1 | `effective_specifications` counts client-supplied digests; the ledger cannot vouch for one (README, *What Phase 8A does not implement*) | **8A.1**, before 8C, because DSR divides by `max(lotteries, specifications)` |
| 2 | A 1.5x run and a 1.0x run share a `run_id` (README *Known limit*; progress "closed as a defect") | **8B3** |
| 3 | An edited `--protocol` file is refused only at exit 19, after three documents and nine events are written (README 8B2b, progress "Known limits") | **8B3** |
| 4 | A protocol declaring `baseline.stress_multiplier != 1` is neither refused nor mentioned (progress "A diagnostic gap") | **8B3** |

## 3. Decisions

| # | Decision | Why |
|---|---|---|
| C-1 | `SizingMode` ∈ {`CONSTANT_NOTIONAL`, `COMPOUNDING`} lives on `BacktestRequest` (default constant) and on `BacktestOutcome` and the sealed bundle, **not** on `BacktestResult` | A field on the result moves every digest. The bundle precedent (`exclude_if`) keeps absent = constant-notional byte-identical |
| C-2 | Compounding sizes from `firm_equity + cumulative_realized_pnl` at the decision bar, passed to the same `RiskEngine.evaluate_for_execution` call | Sizing has one home (D-3). Decisions are taken only when flat, so unrealized is zero there; nothing is recomputed in the engine |
| C-3 | A non-positive sizing equity **rejects the proposal** with reason `equity_exhausted` (recorded, D-8) instead of calling the risk engine, which raises on `firm_equity <= 0` | A ruined account is a result, not a crash, and is visible in the rejections of an in-memory outcome (amended: it cannot reach a sealed bundle, see the review note) |
| C-4 | Scenario identity enters `run_id` only when it is non-default: `stress_multiplier != 1` adds a normalised `stress_multiplier`; `COMPOUNDING` adds `sizing` | Closes defect 2 without moving any Phase 7 / constant 1.0x run id, so the legacy importer's `digest == result.digest()` rule still holds |
| C-5 | `scenario_report` reads only `CONSTANT_NOTIONAL` bundles; a compounding bundle in the same trial is **ignored by the grid, not refused** | It is a different experiment. Counting it as a second 1.0x would make the candidate permanently unreportable — the false-refusal trap the 8B2b README names |
| C-6 | Compounding runs once, at the baseline cost level (1.0x), as its own attempt | §6.5 says "a separate compounding rerun" and §7.7 "the compounding baseline". A compounding grid is not asked for |
| C-7 | The compounding report states `same_trade_sequence` as a **reported fact**, not a check | The 8B2b README assigns this to 8B3: with re-based equity the same costs can change a size and so a trade count. Refusing on it would forbid the very difference compounding exists to show |
| C-8 | The compounding report **refuses** on identity and fidelity: same trial/spec/strategy, window equals the protocol's, cost model equals `costs.baseline` at level 1, same `firm_equity` start, same `bars_seen` (amended 2026-09-30: the nine `REPLAY_FIELDS` -- `firm_equity`, `exit_policy`, `atr_period`, `spread_window`, `defective_bar_tolerance`, `contract_sha256`, `constitution_sha256`, `instrument_id`, `timeframe` -- must match, plus `bars_seen`) | Those are what make the two runs one candidate on one replay; unlike the trade sequence they cannot legitimately differ |
| C-9 | `CapacityDiagnostic` is a typed `UNAVAILABLE` with a reason. No tick-volume proxy is produced | §7.7 permits a proxy but forbids presenting it as capacity; a number nobody can use honestly is noise in a gate input |
| C-10 | `research trial scenarios` and `compounding` **pre-flight** before any write: the `--protocol` file's canonical digest must equal the registered protocol's; no level may already be sealed under another attempt id (amended: plus the checks in the review note) | Closes defect 3. The one-way door is closed before the door, not after nine writes |
| C-11 | `ScenarioReport` gains `declared_baseline_multiplier`. A stressed baseline stays reportable (8B2b's review settled that a registration cannot be amended) but is **named** | Closes defect 4 by mentioning, not refusing |

## 4. Interfaces

### 4.1 Engine

`BacktestRequest.sizing: SizingMode = SizingMode.CONSTANT_NOTIONAL`. In the replay loop, where the risk
engine is called, `firm_equity=` is `request.firm_equity` (constant) or
`request.firm_equity + realized` (compounding). `_run_id` adds the two conditional keys of C-4.
`BacktestOutcome.sizing` mirrors the request. Under compounding the observations'
`firm_equity`/`equity` identity is unchanged: `EquitySeries.firm_equity` is still the **initial** capital.

### 4.2 Bundle

`EvidenceBundle.sizing: SizingMode = Field(default=CONSTANT_NOTIONAL, exclude_if=is_constant_notional)`.
A `COMPOUNDING` bundle must be on the `MARK_TO_MARKET` basis (a validator), because evidence of capital
dynamics without an equity series is not evidence. `mark_to_market_bundle` carries `outcome.sizing`.

### 4.3 Reports (`ops/compounding.py`, `research/validation/capacity.py`)

`CompoundingReport`: `trial_id`, `spec_sha256`, `constant_notional` and `compounding` (each:
`attempt_id`, `evidence_sha256`, `source_result_sha256`, `trades`, `net_pnl`, `final_equity`),
`same_trade_sequence`, `final_equity_difference` (compounding − constant, a delta and never a ratio).

`CapacityDiagnostic`: `status: CapacityStatus` (only `UNAVAILABLE` today), `reason`.

### 4.4 Commands

- `research trial compounding` — runs the compounding rerun at baseline costs, seals it, reports.
  Takes the same option set as `scenarios` minus nothing the report compares against the protocol
  (the rule is unchanged: no option for any value the report checks), plus `--attempt-id`.
- `research trial compounding-report --trial-id` — standalone read.
- `research trial capacity --trial-id` — the typed diagnostic for a registered candidate.
- `scenarios` and `compounding` gain the C-10 pre-flight.

Help text of all three new commands carries no decision vocabulary (the 8B2b gate is extended to them).

## 5. Errors

A new typed `CompoundingEvidenceError` is **not** added: `ScenarioEvidenceError` (exit 19) already means
"sealed scenario evidence is not what it claims", and the compounding report refuses for the same reason.
Its `public_message` stays opaque; the specifics ride the private cause.

## 6. Tests and proof

Every behaviour below is mutation-checked (break it, watch the named test fail, restore):

- constant-notional run ids, result digests and the pinned bundle digest are unchanged;
- a 1.5x run and a 1.0x run of one request have different `run_id`s; a 1.0x run's id is the old value;
- compounding lots at a decision equal constant-notional lots computed at the same equity;
- a winning trade grows the next lot size, a losing one shrinks it; first-position size is identical;
- non-positive sizing equity rejects with `equity_exhausted` and never reaches the risk engine;
- `sizing` is absent from a constant-notional bundle's bytes and present for compounding; a compounding
  bundle without a series is refused;
- a compounding bundle in a trial does not change its scenario report, and a trial with one still reports;
- the compounding report refuses each identity/fidelity clause and reports `same_trade_sequence` both ways;
- the capacity diagnostic is always `UNAVAILABLE` and carries a reason;
- a protocol file one byte off the registered one is refused before any row is written (row counts equal);
- a level already sealed under another attempt id is refused before any write;
- a stressed-baseline protocol reports `declared_baseline_multiplier` of 1.5.

## 7. Known limits this slice leaves

- Two runs at 1.0x with different non-multiplier cost terms still share a `run_id`; the baseline-fidelity
  check catches that at report time, and folding the whole cost model into the id would fail every Phase 7
  import.
- A compounding run that takes the account to zero or below cannot be sealed: `derive_daily_returns`
  refuses non-positive equity. That is fail-closed and stated, not silently handled. Its
  `EXECUTION_STARTED` row stays in the chain and counts as an audit attempt.
- Compounding at higher cost levels is not run; §6.5 does not ask for it.

## 8. Amended during review (2026-09-30)

An independent review of the implemented slice found the following, each verified against the code and
fixed in the fix wave. Where it changes a statement above, the statement is amended in place and marked.

- **Wrong replay inputs could seal a stuck rerun.** The report compared only `firm_equity` and
  `bars_seen` between the two runs, after the writes. Now one predicate (`REPLAY_FIELDS` in
  `ops/compounding.py`: `firm_equity`, `exit_policy`, `atr_period`, `spread_window`,
  `defective_bar_tolerance`, `contract_sha256`, `constitution_sha256`, `instrument_id`, `timeframe`)
  is used by the report (refusal, each field) and by the commands' pre-flight, which compares the run's
  own replay inputs with the sealed constant-notional baseline's before the first append. `scenarios`
  compares against any constant level already sealed and skips the comparison when none is.
- **"Same attempt id is an identical retry and stays a no-op" was false.** The stressed levels' `run_id`
  changed in 8B3 and provenance options are part of the bundle bytes, so a retry derived a different
  bundle under the same attempt id and sealed a second document. A level already sealed under the run's
  own attempt id is now **skipped entirely** (no start event, no simulation, no seal) and its existing
  digest reported. Re-running `scenarios` on a chain sealed before 8B3 therefore skips the sealed levels.
- **Attempt-id reuse.** `compounding --attempt-id grid-1` started an attempt whose start event was
  identical to the baseline's, so the audit count did not rise. `compounding` now refuses any attempt id
  the trial has already started; `scenarios` refuses one sealed at another level but lets a started and
  never-sealed id finish under itself (the retry path of a failed run).
- **Requests and provenance values are built and validated before the first append** (second fix wave:
  non-empty `agent_run_id`, `trial_id` and attempt ids, UTC-able timestamps, and the code's strategy
  id/version equal to the protocol's, exit 19), so a mistyped option leaves no orphan start row. Still able
  to orphan one: a data-dependent `BacktestRefused` from `simulate` and a `derive_daily_returns` refusal,
  both of which need the bars. An orphaned `compounding` attempt id is spent (retry under a new
  `--attempt-id`; `audit_attempts` rises by one more); `compounding` refuses an id the trial has started and
  not sealed as this run's own rerun, and an id already sealed as this run's rerun is a no-op skip. The
  read-then-write pre-flight is not atomic; a single operator is assumed.
- **`equity_exhausted`** cannot reach a sealed bundle, and a ruined run's start row counts as an attempt.
- **`CompoundingRun.ends_flat`** added; `same_trade_sequence` compares closed trades' `proposal_id`s only.
- **One helper** (`baseline_at`) states "the declared baseline at level m"; the two helpers
  `compounding.py` imported from `scenarios.py` are now public (`required_candidate`, `refuse_reportable`).
