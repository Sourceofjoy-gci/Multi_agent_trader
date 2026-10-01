# Phase 8C2 Implementation Plan - PSR, DSR, PBO, stationary bootstrap

Spec: `docs/superpowers/specs/2026-10-01-phase-8c-statistical-validation-design.md` (R-1, R-2, S-7..S-12)
Umbrella: §7.4-§7.6. Rules as in the 8C1 plan. Consumes 8C1's `ReturnSeries`, `TradeSample`, `splits` only.
No command in this slice: 8C3's `validate` assembles everything. These are pure functions; the proof is tests
plus an independent reference computation.

## Task 1 - the measurement type and the moments (`measurement.py`, `moments.py`)
1. `Measurement(CanonicalModel)`: `name`, `value: float | None`, `undefined_reason: str | None` (exactly one set;
   a set value must be finite - NaN/inf are refused at construction), `evidence_sha256: tuple[str, ...]`,
   `promotion_grade: bool`. Helper constructors `defined(...)` / `undefined(...)`.
2. `moments(values) -> Moments` (n, mean, `std` with ddof=1, per-day Sharpe `sr_d = mean/std`, `skew = m3/m2**1.5`,
   `kurt = m4/m2**2` from RAW CENTRAL moments with m2 = mean((x-mean)^2), i.e. population moments, exactly as
   umbrella §7.4 words it). Undefined (a typed `None` result, not an exception) when n < 2 or std == 0.
Tests: known-answer moments on a tiny hand-computed series (write the arithmetic out), constant series is
undefined, n=1 undefined, a NaN never reaches a Measurement.

## Task 2 - PSR and DSR (`psr.py`)
`variance_term = 1 - skew*sr_d + ((kurt-1)/4)*sr_d**2`; undefined when <= 0 (umbrella §7.4).
`psr(series_values, *, benchmark_sr_d=0.0)`: `z = sqrt(n-1)*(sr_d - benchmark)/sqrt(variance_term)`, `PSR = Phi(z)`
with `Phi(x) = 0.5*(1+math.erf(x/sqrt(2)))`. Per R-2 `sr_d` is the PER-DAY Sharpe; the annualised
`sr_d*sqrt(365)` is returned as a separate diagnostic `Measurement` named `sharpe_annualised`.
`expected_max_z(n_trials)`: N <= 0 undefined; N == 1 -> 0; else `alpha=1/N`,
`(1-alpha)*NormalDist().inv_cdf(1-1/N) + alpha*NormalDist().inv_cdf(1-1/(N*e))`.
`dsr(series_values, *, trials: TrialCounters, horizon_days: int)`: `N = max(selection_lotteries,
effective_specifications)` (S-9; record both inputs in the result's reason/evidence); undefined when N == 0;
undefined when `horizon_days != 1` with a reason saying multi-day non-overlap handling is not implemented
(fail closed; the umbrella's horizon sentence, §7.4, is the reason it exists); the benchmark is the expected
maximum scaled by the estimator's own standard error, so `z = sqrt(n-1)*sr_d/sqrt(variance_term) - E[max Z]`
(spec amendment R-5: the umbrella says only 'the same variance correction is applied to the benchmark'; this is the
reading that needs no cross-sectional Sharpe variance, which does not exist for one candidate, and reduces to PSR
at N = 1). Add R-5 to the 8C design doc section 3 table, marked amended 2026-10-01.
Tests: PSR and DSR against reference literals computed by a STANDALONE throwaway script that uses only
`math` and `statistics.NormalDist` (paste its source into the test module docstring so the derivation is
inspectable; do not import any repo code in it); PSR is monotone in mean; N=1 DSR == PSR at benchmark 0; DSR strictly
decreases as N grows; the denominator is the max of the two counters (each term wins in its own case); variance_term
<= 0 undefined (construct skew/kurt that force it); horizon_days=2 undefined; an annualised-scale mistake is caught:
a test asserting PSR of a modest-edge 500-day series is NOT ~1 (it would be under the literal annualised formula) -
this is the R-2 guard. Mutation-prove each branch, including swapping sr_d for sr_d*sqrt(365).

## Task 3 - PBO (`pbo.py`)
`pbo(candidates: Mapping[str, TradeSample], excluded: Sequence[str], folds, splits, *, purge_days, embargo_days,
primary_metric: str) -> PboResult` (CanonicalModel: pbo Measurement, per-split records [split index, IS-best
candidate ids, their OOS lambda, logit-sign as a bool, NOT an infinite float], excluded ids, candidate count).
primary_metric other than `net_expectancy` -> undefined (S-7). Per split: use `split_samples` from 8C1 per
candidate; IS metric = mean net P&L per train trade, OOS = per test trade; any candidate with an empty IS or OOS
sample, or fewer than 2 candidates, -> whole PBO undefined naming the cause. IS-best set = all candidates tied for
the maximum IS metric; OOS ranks are ASCENDING with average rank for ties (1 = worst OOS), R-1;
`lambda = (rank-1)/(N-1)`; the split's lambda is the mean over the IS-best set; overfit iff `lambda <= 0.5`
(equivalently logit <= 0; compute the sign without evaluating log(0)). `PBO = overfit_splits / n_splits`.
Tests: hand-built 3-candidate fixtures where the IS-best is the OOS-best in every split (PBO 0), the OOS-worst in
every split (PBO 1), and mixed; ties (average rank, tied IS-best set); N=1 undefined; empty OOS undefined; wrong
primary_metric undefined; an excluded candidate is listed and not ranked; R-1 BOTH WAYS in two named tests; the
split count equals C(folds,2). Mutation-prove: ascending->descending rank (must flip PBO 0<->1), lambda off-by-one,
`<` vs `<=`, tie handling.

## Task 4 - stationary bootstrap (`bootstrap.py`)
`bootstrap_seed(spec_sha256, attempt_id, policy_version) -> int`: first 8 bytes of
`sha256(f"{spec}|{attempt}|{policy}".encode())` big-endian; constant `POLICY_VERSION = "8c-sb-1"`.
`block_length(n, c) = min(n, max(2, round(c * (n/3) ** (1/3))))` for n >= 2 (n < 2 undefined). Python's `round`
(banker's) is what §7.6 literally says; test a half-way case and document it.
`stationary_bootstrap_means(values, *, replicates, block_length, seed)` Politis-Romano over
`numpy.random.Generator(PCG64(seed))`: restart probability `1/L`, wrap-around indexing, vectorised across
replicates (loop over the n time steps). `BootstrapResult` CanonicalModel: seed, block_length, replicates, mean,
p5, p95 (`np.percentile(..., method="lower")` for p5 and `"higher"` for p95, conservative; record that choice in
the 8C design as R-6), plus a Measurement `bootstrap_lower_bound` = p5. Undefined if n < 2 or replicates < 1.
Tests: block length known answers; seed is stable across `PYTHONHASHSEED` (subprocess with two values, as the
existing determinism test does) and identical for identical inputs, different when any of the three inputs differs;
a constant series gives mean == that constant and p5 == p95; the mean estimate of a long series is close to the
sample mean (loose tolerance, fixed seed); block length 2 on a trending series preserves more autocorrelation than
a huge block length is NOT asserted (flaky) - assert instead the exact first resample indices for a tiny fixed-seed
case computed by hand-run reference code pasted in the test. Mutation-prove: restart probability, wrap-around,
seed input, percentile method.

## Gates (every task and at the end)
Full `uv run pytest -q` with coverage (>95%, new modules fully covered), ruff format/check, mypy, `uv lock
--check`, `git diff --check`, clean tree. README: a 'Phase 8C2' section (what is measured, R-1/R-2/R-5/R-6 stated as
resolutions with their reasons, no gate/verdict) - tests assert it is present.
