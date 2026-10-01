# Phase 8C — Statistical Validation Design

**Status:** approved design (autonomous /goal run), pending plans
**Date:** 2026-10-01
**Predecessors:** Phase 8B3 (compounding, capacity state), Phase 8A.1 (vouched specification digests)
**Umbrella:** `docs/superpowers/specs/2026-09-25-phase-8-validation-design.md` §7.1–§7.8

Section references are to the **umbrella design** unless prefixed "this spec".

## 1. What this phase is for

8A sealed evidence, 8B made it mark-to-market and cost-attributed. 8C measures it. Every statistic the
umbrella asks for — walk-forward, purged CPCV, PSR/DSR, PBO, stationary bootstrap, drawdown/halt/ruin and
regime coverage — is computed here from **sealed bundles only**, never from an assumed schema, and never
judged. Judging is 8D's (§8.2): 8C emits **measurements**, each of which is either a finite value or a typed
`undefined` with a reason, each carrying the evidence digests it was derived from. A measurement that cannot
be computed is never replaced by a default, and non-finite output is never emitted.

### 1.1 What 8C does not do

- No gate, no `PASS`/`FAIL`, no promotion decision, no report document, no package (8D).
- No new ledger event, migration or command that writes; the commands added are reads over sealed evidence.
- No SciPy, pandas or any dependency except **NumPy** (§7.1). The normal CDF is `math.erf`; its inverse is
  `statistics.NormalDist().inv_cdf`; both are stdlib.
- No threshold defined after results exist. Every cutoff below is fixed **in this document, before any 8C
  output exists**, and pinned by a test that fails if it moves; the ones §7.8 leaves to the frozen validation
  policy are read from the protocol's `ValidationSpec`, never from an argument.

## 2. Delivery split (each sub-slice: own plan, implementation, review loop, commit range)

| Slice | Content | Umbrella |
|---|---|---|
| **8C1** | NumPy dependency; the validated input (`ReturnSeries`, `TradeSample`) built from a sealed bundle; WFA folds; CPCV folds/splits/paths; purge and embargo | §7.1–§7.3 |
| **8C2** | PSR, DSR, PBO, stationary bootstrap | §7.4–§7.6 |
| **8C3** | Drawdown, bootstrap Monte Carlo halt/ruin probabilities, regime and trade coverage, and the `research trial validate` read command that assembles every measurement into one typed `StatisticalEvidence` | §7.7 |

The split follows dependency, not size: 8C2 and 8C3 consume 8C1's splits and series and nothing else.

## 3. Design clauses resolved, not silently

The umbrella is the authority, and some of its clauses cannot be implemented as literally worded without
producing a statistic that means something other than what it names. Each is resolved here with its direction
stated: R-1 is a direction correction (the umbrella's sentences contradict one another, and the reading chosen is the
one under which they agree), R-2, R-3, R-5 and R-7 are stricter than the literal reading, and R-4 and R-6 are neutral
or conservative. Each is surfaced to the human in the slice report.

| # | Clause | Problem | Resolution |
|---|---|---|---|
| R-1 | §7.5: "rank the OOS… best first", "rank of 1 produces −∞ and rank N produces +∞", "count `logit <= 0` as overfit" | These three cannot all hold. With best-first ranks, the in-sample-best candidate that ranks *first* out-of-sample gets the smallest λ, the most negative logit, and is counted as **overfit** — the inverse of PBO. The infinities also need λ to reach 0 and 1, which `rank/(N+1)` never does | The OOS rank λ is taken **ascending (1 = worst OOS)** and mapped `λ = (rank − 1)/(N − 1)`, so rank 1 → −∞ (the IS-best was the OOS-worst: overfit) and rank N → +∞. This is the only reading under which the infinity sentence, the `logit <= 0` rule and Bailey–López de Prado's definition agree. "IS best first" is unchanged |
| R-2 | §7.4: `SR = mean/std·√365` inside `z = √(n−1)(SR−SR0)/√variance_term` | Annualising `SR` while `n` counts *days* scales `z` by √365 ≈ 19×, so PSR/DSR would read ~1 for almost any series. That **fails open** on the most important gate | `z` uses the **per-day** Sharpe `sr_d = mean/std(ddof=1)`, which is what the PSR formula's `n` requires. The annualised `SR = sr_d·√365` is still reported, as a diagnostic field named for what it is. The formula, the 0.95 threshold and E[max Z] are unchanged |
| R-3 | §7.3/§11.3: CPCV paths, "purge and embargo remove every overlap" | Candidates are fixed-parameter (no refit), so a test fold's returns are the same in every split and all paths would be the identical full series — the 5th-percentile gate would be vacuous | In each split a closed trade is **excluded from that split's test sample when it crosses the boundary of its own exit fold into a non-test predecessor fold** (fold F, start s: F-1 exists and is not a test fold, purge >= 1 day, entry day <= s-1); a test trade is never dropped for another test fold's boundary. On the train side a trade is excluded when its span reaches the purge zone before, or the embargo zone after, a test fold. *Amended 2026-10-01 after the 8C1 implementation found the original clause incoherent* (it applied every test fold's boundary to every test trade, so a trade wholly inside an earlier test fold was dropped by a later, isolated test fold). Paths then differ by which neighbours were held out, which is what purging is for. Train-side purging is applied the same way for PBO's in-sample sample |
| R-4 | §7.7: "within a declared horizon"; §5.1 lists a horizon under validation | `ValidationSpec` has no horizon field, and adding one would change every protocol digest | The Monte Carlo horizon is the **series length** (the protocol's declared data window, in days), stated in the measurement. No new protocol field |
| R-5 | §7.4: "The same variance correction is applied to the benchmark" | The deflated Sharpe needs a benchmark Sharpe, and the usual one scales `E[max Z]` by the cross-sectional standard deviation of Sharpe estimates across trials; one candidate has no such cross-section, and the sentence does not name another scale | *Amended 2026-10-01, strengthened after the 8C2 independent review.* The benchmark is `SR0 = E[max Z] * max(SE, cross-section)`: `SE = sqrt(variance_term / (n - 1))` is the estimator's own standard error, and the cross-section is the sample standard deviation (`ddof=1`) of the per-day Sharpes of the PBO candidates that have a defined one, used only when at least two are supplied (8C3 passes them). `z = sqrt(n - 1) * (sr_d - SR0) / sqrt(variance_term)`. With `SE` alone this is `sqrt(n - 1) * sr_d / sqrt(variance_term) - E[max Z]`; either way `N = 1` (`E[max Z] = 0`) is exactly PSR. Taking the larger of the two is stricter than either alone, which matters because `N` is chain-global and the true cross-sectional spread can exceed `SE`. The result records both numbers and which was used |
| R-7 | §7.4: `alpha = 1/N` as the weight in `E[max Z]` | The umbrella's weights are not the published Bailey-Lopez de Prado constant, which is the Euler-Mascheroni `gamma = 0.5772156649015329`: `E[max Z] ~= (1 - gamma) * Phi^-1(1 - 1/N) + gamma * Phi^-1(1 - 1/(N e))`. This is a defect in the umbrella text. For `N >= 2` the published value is larger (`N = 2`: 0.520 against 0.450; `N = 10`: 1.575 against 1.332; `N = 100`: 2.531 against 2.330) | *Amended 2026-10-01 after the 8C2 independent review.* The published weights are used (stricter: DSR is lower); the `N = 1` rule (`E[max Z] = 0`) and the large-`N`-safe `-Phi^-1(p)` evaluation stand. A test pins that the published value is not below the umbrella's. **To be surfaced to the human:** the umbrella design text should be corrected |
| R-6 | §7.6: "the 5th and 95th percentiles" | The umbrella names no interpolation. NumPy's default is linear, which can report a value that no replicate produced and, for the 5th percentile that gates a lower bound, one that is *more favourable* than any replicate at or below that rank | *Amended 2026-10-01.* The 5th percentile is `np.percentile(replicate_means, 5, method="lower")` and the 95th is `method="higher"`: always an actual replicate mean, and the neighbour that widens the interval, never narrows it |

A fifth point is a constraint rather than a resolution: §7.7's regime coverage needs each trade's label. The
only classifier in this repository is `features.sessions.session_of`, which the strategies already use.
Regime coverage therefore computes only when every label the protocol declares is a session name, and is
`undefined` otherwise. `RegimeSpec.provenance_sha256` is recorded and **cannot be vouched** (nothing in the
chain can verify a label set was pre-declared beyond the protocol that carries it) — stated, not papered over.

## 4. Decisions

| # | Decision | Why |
|---|---|---|
| S-1 | The input is a `ReturnSeries`: UTC dates (contiguous, strictly increasing), `float64` daily returns, the bundle's `return_series_basis`, and the evidence digest. Built from an `EvidenceBundle`; Decimal→float happens once, at this boundary, with a finite check | One conversion point; Decimal stays everywhere money is held |
| S-2 | `TradeSample`: per closed trade, entry date, exit date, exit instant, net P&L (float64), entry-session label, from `bundle.result.trades` | Expectancy gates are per-trade (§7.8.5) |
| S-3 | Every measurement carries `basis` and is usable on `REALIZED_CLOSED_TRADES` for research, but `promotion_grade` is true only for `MARK_TO_MARKET` (§5.5, §8.4) | Legacy stays reportable and non-promotable |
| S-4 | Purge/embargo are read from `ValidationSpec` hours, converted to whole days by ceiling (16 h → 1 day). Day granularity is the series' own resolution | No sub-day observation exists to purge |
| S-5 | WFA: expanding training ≥ 24 months, 6-month validation, 6-month test, 6-month step, all from `ValidationSpec` months; calendar-month arithmetic on the first UTC day; folds that do not fit the series are **not created**; zero folds is `undefined`, never one short fold | No fabricated fold |
| S-6 | CPCV: `ValidationSpec.cpcv_folds` contiguous folds (sizes differ by at most one day, earlier folds take the remainder); all `C(N, 2)` test-pair splits in lexicographic order; `φ = C(N−1, 1)` paths, path `p` taking for fold `j` the `p`-th split containing `j` as a test fold | Standard CPCV bookkeeping |
| S-7 | The primary metric `net_expectancy` (mean net P&L per closed trade) is the only metric implemented. Any other `primary_metric` string is `undefined`, not a silent fallback | §7.5 "frozen primary metric" |
| S-8 | PBO's candidate set is **every result-producing candidate of the protocol**, read as sealed constant-notional baseline (1.0x) bundles; a candidate with no sealed baseline is listed as excluded, not dropped silently; one candidate, or any candidate with zero trades in any IS or OOS segment, is `undefined` | §7.5 |
| S-9 | DSR's `N` is `max(selection_lotteries, effective_specifications)` from `TrialCounters` over the chain, passed in and recorded with the digest of the chain head it was read at; 8A.1 is what makes the second term trustworthy | §5.6 |
| S-10 | The bootstrap is Politis–Romano over `numpy.random.Generator(PCG64)`; seed = first 8 bytes of `sha256(spec_sha256 ‖ attempt_id ‖ policy_version)` big-endian; `policy_version = "8c-sb-1"`; the report states seed, block length, replicates, mean, p5, p95 | §7.6 |
| S-11 | Signed constants, fixed here: DSR ≥ 0.95, PBO ≤ 0.50, max drawdown < 10%, ≥ 30 OOS trades, ≥ 2 regime labels represented, CPCV p5 rank `ceil(0.05·φ)`, bootstrap c = 6.7, 10,000 replicates (the last two read from the protocol) | §7.8; "cannot be weakened after results exist" |
| S-12 | Every measurement is a `CanonicalModel` with `value: float \| None`, `undefined_reason: str \| None` (exactly one set), and the evidence digests it used. `NaN`/`inf` never appear in a value (PBO's logit infinities are an internal count, not an emitted float) | Fail closed |

## 5. Module layout (§9)

`research/validation/` (created empty in 8B3): `series.py`, `splits.py` (8C1); `moments.py`, `psr.py`,
`pbo.py`, `bootstrap.py` (8C2); `drawdown.py`, `coverage.py`, `evidence.py` (8C3). No module imports
brokers, execution, or live-order code (the architecture allowlist already covers `research`).

## 6. Testing strategy

Mutation-proven, per §11.3: fold boundaries on exact dates; purge/embargo leave no overlapping trade; PSR and
DSR against frozen reference values computed **independently** (a standalone script using `NormalDist`, pinned
as literals, plus a hand-derived case); DSR's denominator is the conservative maximum; PBO ranking, ties
(average rank), logits, the one-candidate refusal, **and the R-1 direction tested both ways** (an IS-best that is
OOS-best must count as *not* overfit); bootstrap seed, block length, percentiles and cross-process determinism;
non-finite and undersized input refused; drawdown uses open-position marks, not exits (a fixture where the
equity dips mid-trade and recovers by the exit); regime and trade-count coverage use the same sealed rows.

## 7. Known limits

- CPCV for a fixed-parameter candidate is a partition-and-exclude exercise, not a refit loop (R-3). Each path keeps boundary-crossing trades at exactly one fold and drops them at every other fold, so the paths differ only in which single fold's crossers survive: the spread measures how many trades straddle fold starts, not out-of-sample robustness, and the CPCV p5 gate must not be described as measuring generalisation.
- 8C1's splits output carries `paths_differ` (false when the purge is under a day or no trade straddles a fold start, since the paths are then identical). 8C3's CPCV p5 measurement must be `undefined` when `paths_differ` is false.
- The 8C1 output names the basis fact `basis_is_mark_to_market`; `promotion_grade` (S-3) remains the property name on `ReturnSeries`.
- DSR's `N` is chain-global, not per strategy family: conservative, and fail-closed.
- DSR's horizon sentence (umbrella 7.4: "the non-overlapping horizon is the ceiling of the declared holding horizon") is honoured **only for `horizon_days == 1`**. With a longer holding horizon the daily returns overlap, and no non-overlap handling is implemented, so DSR is `undefined` with that reason for any other horizon rather than computed as if the days were independent. A caller that does not know the horizon must pass 1 and say so; 8D decides what an undefined DSR blocks.
- Regime labels are recognised, not vouched (§3).
- Capacity stays `UNAVAILABLE` (8B3); it is an input to 8D's ninth gate, not a statistic.
