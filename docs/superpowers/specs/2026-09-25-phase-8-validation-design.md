# Phase 8 — Validation Evidence and Promotion Framework

**Status:** approved umbrella design; detailed design approved for 8A
**Date:** 2026-09-25
**Predecessor:** Phase 7, Session Momentum (`docs/superpowers/specs/2026-09-22-phase-7-session-momentum-design.md`)

Section references are to the **master specification**
(`mt5-multi-agent-trading-house-spec.md`) unless prefixed with "this spec".

## 1. What this phase is for

Phase 7 produced evidence rather than a promising story. Three predeclared
Session Momentum variants lost money after costs, and the full development span
had already been inspected. Phase 8 must make that result impossible to hide,
replace the trial-ledger protocol with durable evidence, implement the validation
battery, and prevent an unvalidated candidate from reaching a paper or live
account.

The framework is allowed to reject the strategy that caused it to be built. A
validation system that requires a positive result is a tuning machine. The
Session Momentum outcome is therefore a success of this phase when it is
reproducible, correctly deflated, and fail-closed.

This document is both:

1. the umbrella design for the complete validation framework; and
2. the detailed design for 8A, Canonical Evidence and Trial Ledger.

8B–8D receive their own brainstorming, spec, and plan cycles after their
predecessor is implemented and verified. This avoids a single plan whose parts
could not be built or reviewed independently.

## 2. Repository state this design must respect

### 2.1 What exists

`research/trial_ledger.py` contains `Trial`, `TrialStatus`,
`deflation_trial_count`, and a two-method `TrialLedger` protocol. `Trial` carries
an ID, spec ID, agent run ID, status, Sharpe, and registration sequence. It has
no result digest, return series, cost provenance, concrete persistence, or
append-only guarantee.

`research/packages.py` already defines `StrategyPackage` with
`trial_ledger_reference`, `validation_report_sha256`, `signature_sha256`, and
`PromotionStage`. The package validator requires a signature for `LIVE`, but
there is no promotion engine behind it.

Phase 7's `BacktestResult` contains reconciled trades and a deterministic digest.
It deliberately has no equity curve. Its `gross_pnl` is after spread and
slippage but before commission and swap; therefore its name cannot support full
cost attribution.

### 2.2 The Phase 7 evidence

The completed Session Momentum trial set is:

| Candidate | Trades | Net P&L | Original result digest |
|---|---:|---:|---|
| `none` | 1,035 | -1174.29200000015850 | `a10b0fa43fc4ddb0185b3a376caab7c9369ec49e3e347d13171d31ab989f022b` |
| `fixed_target` 1.0R | 1,035 | -15578.74700000010180 | `fe5efadef7f5ea2448957e0bec507f2133755df676ade140b05a120cfb5fbc6b` |
| `chandelier` 3.0 ATR | 1,035 | -25120.06200000013010 | `69867de09ef3993e5aabca78b225b21bb1ef82a4aa4fbcd63a9fd52dfe2e0c54` |

All three cover `2022-09-16T01:30:00Z` through `2026-09-25T03:15:00Z`, use
1,035 closed trades each, and remain below zero expectancy after costs. They
were not preregistered in a concrete ledger. Their source bar database was
temporary and has been deleted, so an exact dataset-content hash can no longer be
created. The full trade ledgers remain in the preserved result JSON files and
are sufficient to derive realized daily returns, but not mark-to-market returns
or separate spread and slippage costs.

No unseen holdout exists. The complete development span was inspected during
strategy design and candidate selection. A holdout carved from it now would be
retrospective, not out-of-sample.

### 2.3 Naming

The repository roadmap calls this Phase 8, while the master specification labels
validation as Phase 5 because the two documents use different schedules. This
phase follows the repository sequence and does not renumber prior phases.

## 3. Decisions

| # | Decision | Why |
|---|---|---|
| D-1 | Deliver the full framework as four staged subprojects | Ledger, evidence generation, statistics, and promotion have separate failure modes and can be accepted independently |
| D-2 | Use immutable evidence bundles plus an append-only PostgreSQL ledger | Evidence must be independently reconstructable; the ledger needs transactions, uniqueness, and concurrent append safety |
| D-3 | Treat the canonical bundle as evidence and PostgreSQL as ledger/index | A mutable database aggregate must not be the only copy of a result, and a filesystem alone cannot enforce transactional registration ordering |
| D-4 | Add NumPy when 8C first needs it | 8A and 8B do not need it; CPCV, covariance, bootstrap, and Monte Carlo need a direct float64 numerical implementation |
| D-5 | Fail closed when no locked holdout exists | A retrospective split cannot become unseen by relabelling it |
| D-6 | Track audit attempts, selection lotteries, and effective specifications separately | Attempts, repeated executions, and statistical candidates answer different questions; deterministic reproductions increase audit history but are not new selection lotteries |
| D-7 | Use balanced fail-closed promotion thresholds | Thresholds must be fixed before results; §12 supplies the economic rule, while this spec fixes the statistical cutoffs |
| D-8 | Preserve constant-notional expectancy and add compounding separately | Constant notional measures edge; compounding measures capital dynamics. Replacing the former with the latter would make date ranges incomparable |
| D-9 | Emit mark-to-market equity and complete cost attribution before statistical validation | Realized trade returns omit open-position losses and cannot support honest drawdown or cost gates |
| D-10 | Import Phase 7 as explicitly legacy evidence | Failed attempts remain visible, but backdated preregistration or an invented dataset hash would be false provenance |
| D-11 | Keep promotion and capital signatures outside the research engine | Statistical evaluation can be automated; capital authorization remains a human control |
| D-12 | Record Session Momentum as rejected and leave it in `SANDBOX` | Negative expectancy, contaminated holdout status, and incomplete legacy evidence each independently prevent promotion |

## 4. Architecture and delivery order

### 4.1 Subprojects

1. **8A — Canonical Evidence and Trial Ledger**
   - prospective registration and legacy import;
   - immutable evidence bundles and digests;
   - append-only PostgreSQL events and hash chain;
   - trial, attempt, and counter accounting;
   - verification CLI and Phase 7 import.
2. **8B — Equity and Cost Evidence**
   - mark-to-market equity;
   - canonical daily UTC returns;
   - versioned trade cost attribution;
   - 1.0x, 1.5x, and 2.0x adverse-cost reruns;
   - constant-notional and compounding evidence;
   - capacity diagnostics and an explicit unavailable state when no honest
     volume-to-lots model exists.
3. **8C — Statistical Validation**
   - walk-forward analysis;
   - purged and embargoed validation;
   - CPCV;
   - PSR and DSR;
   - PBO;
   - stationary block bootstrap;
   - drawdown, halt, ruin, and regime diagnostics.
4. **8D — Promotion and Operator Workflow**
   - fail-closed gate evaluation;
   - locked-holdout lifecycle;
   - immutable validation reports;
   - strategy package creation;
   - human paper/live authorization workflow.

The order is evidence first, statistics second. Statistical code must not be
built against an assumed equity schema or an incomplete result.

### 4.2 Data flow

```text
frozen TrialProtocol containing one or more TrialSpecs
    -> database-recorded PREREGISTERED event
    -> execution attempts
    -> immutable baseline, cost, equity, and risk evidence bundles
    -> EVIDENCE_SEALED events
    -> statistical validation report
    -> pure promotion-gate decision
    -> append-only decision event and StrategyPackage
```

No result can be accepted unless its trial was registered first. Legacy import
is the sole explicit exception and is visibly marked at every layer.

### 4.3 Authority and failure

- The content-addressed bundle is authoritative for reconstructing a result.
- PostgreSQL is authoritative for registration order, event uniqueness, event
  chain, and references to evidence digests.
- Neither is trusted when the other disagrees.
- Missing or altered referenced evidence, broken event chains, non-finite
  statistics, stale digests, and unverifiable promotion reports fail closed.
- An interrupted bundle write may leave an orphan content-addressed file. The
  file is quarantined and is not evidence until a later successful registration
  references its digest. Orphan presence alone neither proves nor disproves a
  trial.

### 4.4 Outside this umbrella

- collecting future market data;
- opening a new holdout;
- paper or live order execution;
- automatic scheduling;
- remote object storage;
- cryptographic signing;
- a second broker adapter or asset class;
- capacity modelling that requires depth data MT5 does not provide.

## 5. 8A — Canonical Evidence and Trial Ledger

### 5.1 Trial protocol and candidate specifications

A `TrialProtocol` is the complete frozen declaration made before any candidate
in it executes. A `TrialSpec` is one candidate within that protocol. One
`PREREGISTERED` event seals the protocol and its complete, non-empty candidate
tuple atomically, so an A/B family cannot be registered one winner at a time.

Both are strict, frozen Pydantic models:

| Group | Protocol content | Per-trial content |
|---|---|---|
| Identity | protocol ID/version and `agent_run_id` | `trial_id`, `spec_id`, and candidate ordinal |
| Strategy | strategy ID/version/hash | one typed candidate configuration and lineage |
| Data | instrument, timeframe, requested range, point-in-time policy, and dataset-content SHA-256 | references the protocol dataset exactly |
| Execution | seed, warm-up, fill, sizing, and execution-policy definitions | references the protocol execution policy exactly |
| Costs | baseline and 1.5x/2.0x scenario declarations | declared cost-scenario applicability |
| Validation | WFA/CPCV policy, primary metric, horizon, bootstrap policy, and trial-count rule | declared validation-policy reference |
| Regimes | typed regime-label provenance | references the protocol labels exactly |
| Holdout | range and dataset hash, or an explicit not-defined state | references the protocol holdout exactly |
| Provenance | author, created time, and parent evidence references | candidate rationale and parent trial references |

A prospective registration fails if any required value is missing or if the
candidate tuple is empty. Database `recorded_at`, not a caller-provided
timestamp, establishes registration order. An execution event whose start
precedes registration is refused.

The strategy definition, data specification, execution policy, validation
policy, regime definition, and holdout definition are typed models, not
free-form dictionaries. Their canonical serializations contribute to
`trial_protocol_sha256`; each candidate's canonical serialization contributes to
`trial_spec_sha256`. Re-registering the same protocol and candidate tuple is
idempotent. Repeating an existing candidate creates an attempt under that trial,
not a new trial.

### 5.2 Trial, attempt, and event

A **trial** is one frozen candidate specification. An **attempt** is one
execution of that trial. Re-running a trial creates another attempt without
changing its specification.

The event vocabulary is:

- `PREREGISTERED`;
- `LEGACY_IMPORTED`;
- `EXECUTION_STARTED`;
- `RESULT_RECORDED`;
- `FAILED`;
- `EVIDENCE_SEALED`;
- `VALIDATED`;
- `GATE_DECIDED`.

`PREREGISTERED` is scoped to the protocol and creates all declared trial IDs in
one transaction. `LEGACY_IMPORTED` is the explicit non-prospective event for a
historical result; it creates a visible trial lineage and attempt counter entry
without pretending that registration preceded execution. Execution, result, and
seal events are scoped to one trial and, where applicable, one attempt. `FAILED`
and `RESULT_RECORDED` are terminal alternatives for an attempt. Later
validation and gate events refer to sealed attempts and do not rewrite them.
Current state and the full history are derived by replaying events.

### 5.3 PostgreSQL ledger

The durable ledger uses a dedicated DSN and database, separate from disposable
market-data backfill databases. The core table is `trial_ledger_events` with:

- monotonic database sequence used for ordering, not continuity proof;
- UUID event ID;
- event scope kind and scope ID, plus optional trial and attempt IDs;
- event type and strict discriminated-union JSON payload;
- payload SHA-256;
- client occurrence time and database record time;
- previous event SHA-256 and current event SHA-256;
- explicit legacy-import flag and reason.

A singleton `trial_ledger_heads` row is updated in the same append transaction to
serialize concurrent writers and provide the expected chain head. It is a
projection, not the source of history. The first event's previous hash is 64
zero hexadecimal characters. The migration owner and runtime ledger role are
separate: the runtime role receives only the required event insert/select and
head update privileges, never table ownership or head deletion. Role provisioning
belongs to deployment configuration rather than an environment-specific
migration.

Events cannot be updated or deleted. A unique event ID makes retries idempotent
only when the request scope, event type, occurrence time, and canonical payload
are byte-identical; server-assigned sequence and record time are excluded from
that request comparison. The same event ID with different request content is a
conflict. PostgreSQL sequence gaps caused by rolled-back attempts are permitted;
integrity comes from explicit event links and hashes, not a gap-free allocator.

Each event hash covers its canonical payload, payload digest, identity fields,
database sequence, and previous hash. Verification walks the chain from the
zero-hash genesis and checks every referenced evidence digest. A periodic
externally signed head checkpoint is introduced in 8D; 8A provides the
deterministic chain and export needed by it.

### 5.4 Canonical evidence bundle

A bundle is one UTF-8 JSON document with `bundle_schema_version`,
`result_schema_version`, and a top-level shape containing:

- trial specification and digest;
- attempt identity;
- registration and execution provenance;
- source `BacktestResult` or explicitly versioned legacy result;
- daily-return series and its basis;
- cost-attribution status (`COMPLETE`, `PARTIAL`, or `UNAVAILABLE`) and
  component summary;
- result and rejection summaries;
- dataset and market-data provenance.

Canonical serialization is Pydantic JSON-mode output encoded as UTF-8 with
object keys sorted, array order preserved, `(',', ':')` separators, Unicode left
unescaped, and `allow_nan=False`. Decimal values use `format(value, "f")`,
preserving the field's declared scale and never using exponent notation; timestamps
use a UTC `Z` suffix. Its SHA-256 is the
`evidence_sha256`. Validation reports reference sealed evidence and are never
embedded in a result bundle. The existing backtest result digest is retained
separately as `source_result_sha256` so a Phase 7 artifact keeps its original
identity.

Evidence is written to a configured durable root under its content digest. The
writer uses a temporary sibling, flushes it, atomically renames it, and never
overwrites different bytes at the same digest. The ledger stores a root-relative
path and digest, never an unrestricted caller path.

### 5.5 Return-series status

Every evidence bundle states one basis:

- `REALIZED_CLOSED_TRADES` — derivable from historical trade exits;
- `MARK_TO_MARKET` — includes open-position valuation and is required for
  promotion-grade statistical validation;
- `MISSING` — no return series can be reconstructed.

Realized and mark-to-market series are never interchangeable. Phase 7 imports
use `REALIZED_CLOSED_TRADES` and remain non-promotable.

### 5.6 The three counters

Counters are derived from immutable events:

1. **Audit attempts:** every distinct started execution, including abandoned,
   failed, repeated, and deterministic-reproduction attempts.
2. **Selection lotteries:** every distinct preregistered trial that began an
   execution, including failed or abandoned candidates, whether or not it
   produced or won a result. An exact deterministic reproduction increments the
   audit count but does not create another selection lottery.
3. **Effective specifications:** distinct trial specification digests that began
   an execution, deduplicated across trial IDs by canonical digest and lineage.

Until dependence between related specifications is modeled, DSR uses the
conservative maximum of selection lotteries and effective specifications. The
full audit count remains a governance diagnostic and does not turn a deterministic
reproduction into a new statistical lottery.

The three Phase 7 candidates produce three audit attempts, three selection
lotteries, and three effective specifications.

### 5.7 Phase 7 legacy import

The importer accepts each preserved Phase 7 JSON artifact and records:

- original result digest;
- strategy, contract, constitution, run range, and cost provenance present in
  the artifact;
- `LEGACY_UNPREREGISTERED` status;
- late-import reason;
- dataset-content hash as unavailable, never fabricated;
- holdout status `CONTAMINATED` under the §8.1 lifecycle;
- realized daily returns derived from the closed-trade ledger;
- unavailable mark-to-market and separate spread/slippage attribution.

The import is idempotent by source result digest. A second import does not add
a trial, attempt, or selection candidate. It never creates a synthetic
preregistration event.

### 5.8 8A command surface

The existing `research` command group gains:

- `research trial register` — validate and append a prospective registration;
- `research trial record` — attach and seal one attempt's evidence;
- `research trial import-legacy` — import the explicitly permitted historical
  form;
- `research trial show` — replay one trial's events and evidence references;
- `research trial count` — report the three counters;
- `research trial verify` — verify the event chain, digests, files, and
  cross-references.

Every command fails with a typed domain error when its ledger DSN or evidence
root is unavailable. No default credentials, in-memory production ledger, or
implicit temporary directory is allowed.

### 5.9 8A acceptance

8A is complete when:

- the migration creates the append-only ledger;
- a prospective protocol seals all declared candidates atomically;
- a prospective trial cannot record a result without prior registration;
- concurrent appends preserve one valid chain;
- update/delete and event-ID conflicts fail;
- canonical JSON and all hashes are deterministic across processes;
- missing or altered referenced evidence fails verification;
- interrupted writes leave no partially visible evidence;
- legacy import is idempotent and reports the three Phase 7 candidates;
- repeated registration of the same frozen trial cannot silently become a new
  candidate;
- every required lint, type, architecture, and test check passes.

## 6. 8B — Equity and Cost Evidence

### 6.1 Mark-to-market equity

The engine emits an immutable mark-to-market observation at every processed bar
close and a final observation after all positions are flat. Each observation
contains:

- UTC timestamp;
- equity;
- cumulative realized P&L;
- unrealized P&L;
- open-position count.

For every point:

```text
equity = firm_equity + cumulative_realized_pnl + unrealized_pnl
```

The final flat observation must reconcile to `firm_equity + net_pnl`. Missing,
duplicate, or non-increasing UTC timestamps fail. The series is digested and
sealed through 8A.

### 6.2 Canonical daily returns

Daily evidence uses every UTC calendar date from the first requested date through
the last requested date, inclusive:

- end-of-day equity is the final available mark within that UTC day;
- a day with no available mark carries the prior end-of-day equity forward and
  therefore returns zero;
- the first day's denominator is fixed initial firm equity;
- later denominators are the preceding UTC day's end equity and must be strictly
  positive;
- an open position's unrealized P&L remains in the return.

This produces a rectangular daily series without inserting artificial rows
between CPCV test segments and without pretending that weekends are missing.

### 6.3 Versioned trade accounting

New result versions store, per trade:

- market P&L before spread and slippage;
- spread cost;
- slippage cost;
- commission;
- signed swap;
- net P&L.

They satisfy:

```text
post_fill_gross = market_pnl - spread_cost - slippage_cost
net_pnl = post_fill_gross - commission + swap
```

Totals must reconcile to the result and the mark-to-market series. The misleading
legacy `gross_pnl` is mapped to `post_fill_gross` only by the versioned legacy
adapter. Legacy spread and slippage remain explicitly unknown; the legacy
adapter must not invent a numeric residual or write either unknown as zero.

### 6.4 Cost scenarios

Every preregistered candidate is rerun at 1.0x, 1.5x, and 2.0x costs. Scenarios
rerun the engine because changed fills can alter exits and trade sequence.

For adverse multiplier `m`:

- commission charges and slippage scale by `m`;
- observed spread cost scales by `m`;
- a negative swap charge becomes `m` times more negative;
- a positive swap credit is reduced by `(m - 1) * abs(swap)`, so stress never
  increases carry.

At `m = 1`, every component exactly matches baseline. Signed-baseline
multiplication remains available only for separately labelled sensitivity
analysis, never as a promotion stress result.

### 6.5 Compounding

Constant-notional execution remains the canonical edge and comparability path.
A separate compounding rerun supplies current equity to the real risk engine for
position sizing while preserving all constitutional caps. It records its own
mark-to-market series and cannot replace constant-notional expectancy evidence.

## 7. 8C — Statistical Validation

### 7.1 Numerical contract

Statistical arrays use direct NumPy `float64`. Money, prices, rates, hashes, and
ledger values remain Decimal or strings. Inputs are finite, one-dimensional where
specified, and aligned by explicit UTC date. Empty, constant-with-zero-variance,
or non-finite samples fail closed rather than receiving convenient defaults.

### 7.2 Walk-forward analysis

The default signed policy is:

- expanding chronological training;
- at least two years of training before the first evaluation;
- six-month validation;
- six-month forward test;
- six-month fold step;
- no shuffling;
- purge and embargo derived from the longest declared feature, label, and
  holding horizon.

The first prospective Session Momentum policy must register 16 hours; the
legacy Phase 7 import has no signed split policy. Validation selects only among
configurations already declared in the trial specification. Forward test results
cannot feed a later parameter choice.

### 7.3 CPCV

Six contiguous chronological purged folds yield:

```text
C(6, 2) = 15
```

CPCV paths concatenate held-out segments in chronological order. Within a split,
daily returns are measured relative to that segment's starting equity; the
absolute P&L path is not reset. This preserves segment-level returns and the
continuous capital path without inventing zero-return gaps between test blocks.
The CPCV fifth-percentile gate uses the nearest-rank value at
`ceil(0.05 * path_count)`, which is conservative when the path count is small.

### 7.4 PSR and DSR

For daily net return observations with sample Sharpe `SR`, sample size `n`, raw
central-moment skewness `gamma3 = m3 / m2^(3/2)`, and raw central-moment
kurtosis `gamma4 = m4 / m2^2`, PSR at benchmark `SR0 = 0` is:

```text
variance_term = 1 - gamma3*SR + ((gamma4 - 1)/4)*SR^2
z = sqrt(n - 1) * (SR - SR0) / sqrt(variance_term)
PSR = Phi(z)
```

`SR` is `mean(r) / sample_std(r, ddof=1) * sqrt(365)` because the canonical
series contains UTC calendar days, including weekends. A non-positive variance
term or `N <= 0` is undefined and blocks promotion. The non-overlapping horizon
is the ceiling of the declared holding horizon in UTC calendar days. DSR
compares the observed statistic with the expected maximum of
`N` independent standard-normal trials:

```text
E[max(Z)] = (1 - alpha) * Phi^-1(1 - 1/N)
         + alpha * Phi^-1(1 - 1/(N*e))
alpha = 1/N
```

where `e` is Euler's number. Use this expression for `N > 1` and set
`E[max(Z)] = 0` for `N = 1`. The same variance correction is applied to the
benchmark. Promotion requires `DSR >= 0.95`. `N` is the conservative trial
count from §5.6, not Python's `hash()` or a count reconstructed from a
selected result.

### 7.5 PBO

PBO uses every distinct result-producing preregistered candidate and the same 15
CPCV splits. Failed candidates remain in the DSR trial count and audit report
but cannot be ranked without a result; the PBO report lists their IDs as
excluded. Candidates are ranked by the frozen primary metric, not by a metric
chosen after seeing the split. For each split:

1. rank candidates by in-sample performance, best first;
2. rank the same candidates by out-of-sample performance;
3. convert the out-of-sample rank `lambda` to
   `logit = log(lambda / (1 - lambda))`;
4. count `logit <= 0` as an overfit case.

Ties use average rank. One candidate, no overlapping observations, or empty OOS
segments make PBO undefined and block promotion. A rank of `1` produces
`-infinity` and a rank of `N` produces `+infinity`; both are valid outcomes.
Promotion requires `PBO <= 0.50`.

### 7.6 Stationary bootstrap

The default policy uses 10,000 stationary-bootstrap replicates of daily returns.
The expected block length is:

```text
L = round(c * (n / 3)^(1/3))
```

with `c = 6.7`, capped at `n` and never below two when `n >= 2`. The
deterministic seed is derived from the trial specification digest, attempt
ID, and policy version, never Python's randomized `hash()`. Reports include
seed, block length, replicate count, mean estimate, and the 5th and 95th
percentiles. Promotion requires the one-sided 95% lower bound, the 5th
percentile, for mean return to be above zero.

### 7.7 Drawdown, halt, ruin, and regimes

- Maximum drawdown uses mark-to-market equity, not closed trades.
- The deterministic gate requires maximum drawdown below 10% on baseline
  constant-notional, every CPCV path, and the compounding baseline.
- Monte Carlo uses the same deterministic stationary-bootstrap policy to report
  probability of a 10% drawdown halt and probability of equity loss within a
  declared horizon. Simulation failure is blocking; the probabilities are
  mandatory diagnostics under the signed 10% rule.
- Promotion requires at least 30 OOS trades distributed across declared regime
  labels. A missing, post-hoc, or single-regime-only label set blocks promotion.
- Capacity is a mandatory typed diagnostic. Tick volume alone may produce a
  relative participation proxy, but it cannot be presented as capital capacity;
  without a declared volume-to-lots model, the capacity gate is `UNAVAILABLE`
  and blocks promotion.

### 7.8 Statistical promotion gates

The signed gate set is:

1. research WFA is complete;
2. locked-OOS expectancy is positive at 1.5x and 2.0x costs;
3. DSR is at least 0.95;
4. PBO is at most 0.50;
5. CPCV fifth-percentile net expectancy, defined as the mean net P&L per trade
   in each path, is above zero;
6. stationary-bootstrap 95% mean-return lower bound is above zero;
7. maximum mark-to-market drawdown is below 10%;
8. OOS trade and regime coverage requirements pass;
9. a declared capital-capacity model passes its predeclared participation and
   cost-budget limits.

Missing, stale, undefined, or non-finite evidence fails its gate. Thresholds are
part of the frozen validation policy and cannot be weakened after results exist.

## 8. 8D — Promotion and Operator Workflow

### 8.1 Holdout lifecycle

Holdout state is one of:

- `NOT_DEFINED`;
- `LOCKED`;
- `OPENED`;
- `CONSUMED`;
- `CONTAMINATED`.

A holdout follows the transitions `NOT_DEFINED -> LOCKED -> OPENED -> CONSUMED`.
Any inspection before lock transitions directly to `CONTAMINATED`, which is
terminal. A holdout is locked with date range, dataset hash, and policy before
research gates run. Only a strategy that passes every research gate may open it
once. Opening records time and evidence digest; consumption records the final
validation report. Post-opening parameter changes create a new holdout
requirement. No later relabelling repairs contamination.

### 8.2 Gate evaluation

Promotion evaluation is a pure function from a sealed validation package to a
complete typed decision. Every gate emits `PASS`, `FAIL`, or `UNAVAILABLE` with
its measured value, threshold, evidence digest, and reason. Any non-pass blocks
promotion.

The existing `PromotionStage` remains:

- `SANDBOX` — research and rejected candidates;
- `PAPER` — all gates and a human paper authorization;
- `LIVE` — separate capital authorization.

No statistical pass directly changes stage.

### 8.3 Validation report and package

The immutable report contains:

- trial and attempt references;
- policy and source digests;
- dataset and holdout state;
- WFA/CPCV definitions and results;
- DSR/PBO/bootstrap outputs;
- cost scenarios and attribution status;
- drawdown/halt/ruin diagnostics;
- trade and regime counts;
- every gate outcome;
- report schema version and report SHA-256.

`StrategyPackage` references the ledger and report digests. 8D extends the
package contract so `PAPER` requires a human authorization reference. `LIVE`
additionally requires the existing signature field. This phase verifies
authorization and signature references; it does not implement signing.

### 8.4 Session Momentum decision

The recorded decision is `REJECTED`, and its stage remains `SANDBOX`. Required
reasons include:

- negative expectancy at baseline costs;
- no locked unseen holdout;
- legacy evidence was not preregistered;
- dataset-content hash is unavailable;
- mark-to-market returns are unavailable;
- spread and slippage cannot be separately attributed.

Any one reason blocks promotion. Statistical diagnostics may be reported for
research, but they cannot convert this evidence into prospective proof.

## 9. Module boundaries

The natural ownership is:

- `research/trial_ledger.py` — trial, attempt, event, and ledger contracts;
- `research/evidence.py` — canonical evidence schema, digest, and content store;
- `research/ledger_store.py` — concrete PostgreSQL append and replay;
- `research/validation/` — equity, cost, split, statistical, and risk modules;
- `research/promotion.py` — holdout, gate, report, and package orchestration.

The concrete ledger evolves the existing two-method protocol into the append/replay
contract; it does not add a parallel ledger API. No storage interface is added
until a second implementation exists. Research validation may depend on
canonical core, market-data, strategy, and risk contracts; it must not import
broker execution or live-order modules.

## 10. Error handling and trust boundaries

The following are hard errors, not warnings:

- missing ledger DSN or evidence root;
- malformed strict Pydantic input;
- non-UTC or ambiguous timestamp;
- result before preregistration;
- duplicate event ID with different bytes;
- update or delete of an event;
- broken event or evidence hash chain;
- missing, altered, or digest-mismatched referenced evidence;
- dataset hash mismatch for a prospective trial;
- contaminated holdout presented as locked;
- non-finite or undersized statistical input;
- changed validation thresholds after registration;
- missing cost attribution where a promotion gate requires it;
- missing regime provenance where coverage is required;
- missing, stale, or unverifiable validation report;
- attempted automatic transition to `LIVE`.

Errors identify the trial, attempt, event, or gate when known and never include
credentials, connection strings, or secret material.

## 11. Testing strategy

### 11.1 8A tests

- canonical serialization is stable across key insertion order and processes;
- Decimal scales, UTC forms, arrays, and hashes have known answers;
- event genesis and every chained append verify;
- update, delete, missing linked event, chain break, and event conflict fail;
  allocator gaps from rolled-back attempts do not fail verification;
- concurrent append produces one valid chain;
- repeated identical events are idempotent; changed duplicates fail;
- result-before-registration fails;
- missing and altered evidence fail verification;
- atomic writes expose complete files or no files;
- legacy imports are idempotent and preserve original result digests;
- counters distinguish audit attempts, selection lotteries, and specifications;
- CLI commands cover success and every typed failure without network access.

### 11.2 8B tests

- equity identities hold at every point;
- final flat equity reconciles with net P&L;
- open-position losses appear in MTM and daily returns;
- weekends and no-signal days are present exactly once;
- first-day and later-day return denominators are explicit;
- every cost identity reconciles exactly;
- missing legacy attribution stays unknown;
- 1.5x and 2.0x reruns never improve positive swap carry;
- constant-notional and compounding paths remain distinct;
- scenario reruns use the real risk and fill paths.

### 11.3 8C tests

- WFA and CPCV fold boundaries use exact known timestamps;
- purge and embargo remove every overlap;
- segment reconstruction introduces no artificial return;
- PSR and DSR match frozen numerical reference values;
- DSR uses the conservative trial denominator;
- PBO ranking, ties, logits, and one-candidate refusal are tested;
- bootstrap seed, block length, percentiles, and determinism are tested;
- non-finite and undersized inputs fail;
- each gate independently fires;
- drawdown uses open-position MTM, not trade exits;
- regime and trade-count gates use the same sealed OOS rows.

### 11.4 8D tests

- every gate combination produces one deterministic decision;
- missing evidence cannot become a pass;
- a contaminated holdout cannot be opened;
- a holdout opens at most once;
- validation-policy changes require a new trial;
- report hashes bind ledger, policy, data, and results;
- `PAPER` and `LIVE` authorization rules fire;
- rejected Session Momentum evidence remains `SANDBOX`.

Every new test is mutation-checked: break the protected behavior, observe the
intended test fail, and restore the implementation. The complete repository
suite, Ruff, strict mypy, lock check, architecture acceptance, and clean diff
must pass before a subproject is considered complete.

## 12. Phase 7 migration

1. Confirm the three preserved JSON artifacts and their SHA-256 values.
2. Create the dedicated research-ledger database and evidence root.
3. Import each artifact once as `LEGACY_UNPREREGISTERED`.
4. Verify original digests, realized-return derivation, event chain, and file
   references.
5. Confirm counters report three audit attempts, three selection lotteries, and
   three effective specifications.
6. Record the candidate rejection and incomplete provenance in the operator
   documentation.
7. Do not rerun Session Momentum merely to manufacture prospective metadata.
8. Do not copy the temporary source result into source control unless it becomes
   necessary content-addressed evidence under the configured durable root.

If an artifact is missing or its digest differs, migration stops and reports the
exact mismatch. No placeholder or reconstructed digest is accepted.

## 13. Deliberately excluded

| Item | Treatment |
|---|---|
| Future holdout collection | Separate operational phase after 8D |
| Paper/live execution and capital signing | Separate authorized deployment work |
| Remote durable object store | Add when deployment topology requires it |
| Full option-bar spread/slippage reconstruction for Phase 7 | Impossible from the preserved result; legacy attribution remains unknown |
| Capacity requiring order-book depth | Deferred until a depth data source exists |
| Live market-data regime classification | Separate warm-loop feature work; promotion requires declared provenance |
| Automatic hyperparameter search | Not added; every candidate is an explicit trial |
| Replacing negative results with a new strategy | Separate hypothesis and preregistration cycle |

## 14. Handoff from 8A

8B receives:

- a durable, append-only trial and attempt history;
- canonical, digest-verifiable evidence bundles;
- explicit legacy lineage and the three Phase 7 imports;
- a verified return-series basis field;
- strict failure behavior for missing or altered evidence;
- a clean separation between constant-notional edge evidence and future
  compounding risk evidence.

8B must extend the engine's result contract without weakening Phase 6's
constant-notional semantics, then seal its first mark-to-market and
fully attributed evidence through the 8A ledger.
