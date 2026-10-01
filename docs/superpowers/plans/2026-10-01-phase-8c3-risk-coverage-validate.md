# Phase 8C3 Implementation Plan - drawdown, Monte Carlo, coverage, and `research trial validate`

Spec: `docs/superpowers/specs/2026-10-01-phase-8c-statistical-validation-design.md` (S-10..S-12, R-4, section 7).
Umbrella §7.7, §7.8 (the measurements behind gates 3, 5, 6, 7, 8). Rules as in the 8C1/8C2 plans. Consumes
8C1's `ReturnSeries`/`TradeSample`/`splits`/`sampling` and 8C2's `Measurement`, `psr`/`dsr`/`pbo`/`bootstrap`.
Measurements only: NO gate, NO verdict, NO threshold decision, NO new ledger event.

## Decision recorded now (amends 8C1's note): the CPCV p5 measurement is NOT blocked by `paths_differ`

8C1 told this slice to make the CPCV 5th-percentile measurement `undefined` when `paths_differ` is false. That
would make gate 5 unpassable for every intraday strategy (none straddles a UTC fold start), which is a gate that
can never be met and therefore measures nothing. When the paths are identical their p5 equals the sample's net
expectancy, which is a real, non-vacuous quantity (is out-of-sample net expectancy positive). So: the measurement
is ALWAYS computed, `paths_differ` is carried in its output as a plain diagnostic, and the README/spec say so
(replace the 8C1 sentences that said otherwise, with the "amended 2026-10-01" marker). 8D is told, in the README,
that gate 5 then reads the aggregate expectancy and must say so in its reason.

## Task 1 - signed constants (`research/validation/policy.py`)

Frozen constants fixed NOW, before any 8C3 output exists (S-11): `DSR_MINIMUM = 0.95`, `PBO_MAXIMUM = 0.50`,
`MAX_DRAWDOWN = 0.10` (strict: below), `MIN_OOS_TRADES = 30`, `MIN_REGIMES = 2`, `CPCV_P5_QUANTILE = 0.05`,
`MIN_WFA_FOLDS = 1`, `MC_POLICY_VERSION = "8c-mc-1"`. A test pins every literal (it fails if one moves). These are
constants 8D reads; 8C3's measurements do not compare against them except `MAX_DRAWDOWN` inside the Monte Carlo
(the halt level).

## Task 2 - drawdown (`drawdown.py`)

`max_drawdown_fraction(equity: np.ndarray) -> float`: largest peak-to-trough fall as a fraction of the running
peak; equity must be 1-D finite float64 and strictly positive, else StatisticalInputError. `equity_drawdown(
series: EquitySeries)`: prepend `firm_equity` as the initial point, then every observation's `equity` (BAR level,
open-position marks included - §7.7 "mark-to-market, not closed trades"). `path_drawdown(path_returns)`: compound
daily returns from 1.0 (the path's own start; the absolute P&L path is not reset, §7.3). Each returns a `Measurement`
(`max_drawdown_baseline`, `max_drawdown_cpcv_path_<i>`, `max_drawdown_compounding`); undefined when the equity
series is absent (e.g. a REALIZED_CLOSED_TRADES bundle has no mark_to_market) - never computed from trades.
Tests: hand-computed tiny curves; a fixture where equity dips 8% while a position is open and recovers before the
exit, so a trades-only drawdown is 0 and the MTM one is 0.08 (the §11.3 clause); non-positive equity refused; a
bundle without a series gives `undefined` with a reason.

## Task 3 - Monte Carlo halt and loss probabilities (`montecarlo.py`)

`drawdown_loss_probabilities(series: ReturnSeries, *, replicates, block_length, seed) -> McResult`: horizon = the
series length in days (R-4, stated in the result); stationary-bootstrap resample the daily returns to that horizon,
compound from 1.0, track per replicate the running peak and max drawdown streaming over time steps (memory
O(replicates), never O(replicates x n) - reuse 8C2's per-step index stream; if it is not factored for reuse, factor
it WITHOUT changing its outputs, proving the 8C2 reference-index test still passes untouched). `p_halt` = fraction
with max drawdown >= `MAX_DRAWDOWN`; `p_loss` = fraction whose FINAL equity is below 1.0 (state the definition).
Seed from `bootstrap_seed(spec_sha256, attempt_id, MC_POLICY_VERSION)` (policy version differs from the bootstrap's
so the two streams are independent). `McResult` CanonicalModel: seed, block_length, replicates, horizon_days, and
two Measurements. Undefined, never default, when n < 2 etc. (same as 8C2).
Tests: a series with all-zero returns gives p_halt 0 and p_loss 0; a constant -2% day gives p_halt 1 and p_loss 1
(exact, no randomness); a hand-sized case with a known analytic answer; determinism across PYTHONHASHSEED
subprocesses; the policy-version seed differs from the 8C2 bootstrap seed; replicate count honoured.
Mutation-prove each constant, comparison direction (>= vs >), peak tracking, horizon, seed policy.

## Task 4 - coverage, CPCV p5, scenario expectancy (`coverage.py`)

- `net_expectancy(sample) -> float | None` (mean net P&L per closed trade; None for zero trades).
- `cpcv_p5(paths) -> Measurement`: per path net expectancy; a path with zero kept trades makes the measurement
  undefined naming the path; otherwise the nearest-rank value at `ceil(CPCV_P5_QUANTILE * n_paths)` (1-based) of the
  ascending-sorted expectancies; carries `paths_differ` and the per-path values in its output model.
- `oos_coverage(paths, declared_labels) -> CoverageResult`: the OOS sample is the path with the FEWEST kept trades
  (conservative); `oos_trades` = its size; regimes defined only when every declared label is a `Session` name and the
  sample has trades; `regimes_represented` = number of declared labels with >= 1 trade in that sample; both as
  Measurements; undefined with the reason otherwise; the declared label set is echoed back.
- `scenario_expectancy(sample, multiplier) -> Measurement`: net expectancy over the FULL sealed sample of a stressed
  bundle; named `scenario_expectancy_<m>`; the reason/doc states it is the sealed sample's expectancy, not a locked
  out-of-sample one (no holdout exists - 8D decides what that means).
Tests: hand-built samples; nearest-rank at n_paths 5 (rank 1), 20 (rank 1) and 21 (rank 2); zero-trade path
undefined; fewest-kept-path choice; labels not sessions -> undefined; single-regime sample reports 1 represented;
mutation-prove rank formula, sort direction, min-path choice, label check.

## Task 5 - assembly and the command (`ops/validate.py`, `research/validation/evidence.py`, cli)

`StatisticalEvidence` CanonicalModel (trial_id, spec_sha256, basis flag, and every Measurement/result above: WFA fold
count or undefined reason, CPCV report, PSR, DSR result, PBO result, bootstrap result, MC result, drawdowns (baseline,
each path, compounding), CPCV p5, coverage, scenario expectancies at 1.5/2.0, the 8B3 capacity diagnostic, and the
evidence digests used). Assembly is a pure function over already-read inputs, mirroring how 8B2b/8B3 separate reads
from logic: baseline = the trial's single sealed constant 1.0x bundle (refuse otherwise, exit 19 like `splits`);
compounding bundle optional (compounding drawdown undefined when absent); 1.5x/2.0x bundles optional (undefined when
absent); PBO candidates = every protocol candidate with exactly one sealed constant 1.0x bundle, the rest listed as
excluded; counters = `ledger.counters()`, chain head = the last record's event hash; `horizon_days` = ceil(registered
strategy `horizon_seconds` / 86400) - find where the strategy registry exposes horizon_seconds; if it cannot be read
without constructing an exit policy, use the documented default arm and say so, or refuse - do not invent;
`attempt_id` for seeds = the baseline bundle's. Command `research trial validate --trial-id T`: READ only, prints
StatisticalEvidence; exit 0 whenever assembled (undefined measurements are data, not errors); refusals as `splits`.
Help text free of decision vocabulary (extend the gate); no "threshold" word anywhere in output keys or help.
Tests: assembly unit tests with builders; an integration test per refusal with row/file equality; the command over a
real sealed run (reuse 8C1/8C2 acceptance fixtures); every defined Measurement finite, every undefined has a reason,
every one carries an evidence digest; determinism: the command output is byte-identical on two runs.

## Task 6 - acceptance, README, spec amendments

`tests/acceptance/test_phase8c3.py` (real PostgreSQL), README "Phase 8C3" section + commands row + the amended p5
sentences (tests assert present), spec section 7 amended in place (the p5/paths_differ reversal, dated).
