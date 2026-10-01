# Phase 8C1 Implementation Plan - NumPy, the validated input, WFA and CPCV splits

Spec: `docs/superpowers/specs/2026-10-01-phase-8c-statistical-validation-design.md` (esp. S-1..S-7, S-12, R-3, R-4)
Umbrella: §7.1-§7.3. Rules as in the 8B3 plan: zero `assert` in src/, outputs are `CanonicalModel`s,
numpy-carrying working structures are frozen slotted dataclasses (the `BacktestRequest` precedent; pydantic
cannot hold an ndarray), every refusal fails closed and typed, every test mutation-proven, every task ends
green, no scope creep, where the code contradicts the brief the code wins - report it.

## Task 1 - dependency, error type, the validated input (`research/validation/series.py`)
1. `pyproject.toml`: add `numpy` as a direct dependency (it is already in uv.lock transitively; pin a range
   like the others, e.g. `numpy>=2,<3`), `uv lock`, `uv lock --check`. numpy must be the ONLY new dependency.
2. A new typed `StatisticalInputError(TradingHouseError)` in core/errors.py with an opaque public message and a
   stable exit code mapped in `cli.EXIT_CODES` (a test enforces every typed error has one).
3. `ReturnSeries` (frozen slots dataclass): `days: tuple[date, ...]`, `values: np.ndarray` float64 1-D,
   `basis: ReturnSeriesBasis`, `evidence_sha256: str`; classmethod `from_bundle(bundle, evidence_sha256)`.
   Refuses (StatisticalInputError, specifics on the private cause): empty; non-contiguous or non-increasing
   days; any value that is not finite after Decimal->float; fewer than `MIN_SERIES_DAYS = 30` days; basis
   MISSING. `promotion_grade` property = basis is MARK_TO_MARKET.
4. `TradeSample` (frozen slots dataclass): parallel tuples/arrays `entry_day`, `exit_day`, `entry_at`, `exit_at`,
   `net_pnl` (float64), `session` (str, `session_of(entry_at).value`), built from `bundle.result.trades`;
   same finite check; zero trades is allowed (an empty sample), not an error.
Tests: each refusal with its own case; Decimal->float exactness on a few values; a real bundle from the unit
builders round-trips; promotion_grade both ways. Mutation-prove each refusal.

## Task 2 - walk-forward folds and CPCV bookkeeping (`research/validation/splits.py`)
1. `add_months(day, n)` (calendar months, day clamped to month end; pure, tested at 31 Jan, 29 Feb leap/non-leap).
2. `walk_forward_folds(days, *, train_months, validation_months, test_months) -> tuple[WalkForwardFold, ...]`;
   `WalkForwardFold` is a CanonicalModel (index, train_start, train_end, validation_start, validation_end,
   test_start, test_end; all inclusive dates). Expanding train from `days[0]`; the first train window is
   `train_months`; fold k's train end is `train_months + k*WFA_STEP_MONTHS` (constant 6, spec §7.2) after the
   start; validation then test follow with no gap; a fold exists only if its test end is <= `days[-1]`; zero
   folds returns an empty tuple (the CALLER treats that as undefined - this function never fabricates a short fold).
   Purge: the last `purge_days` of train and of validation are dropped (train_end/validation_end move back); fold
   fields report the effective, post-purge windows. `purge_days = ceil(purge_hours / 24)` helper `hours_to_days`.
3. CPCV: `cpcv_folds(days, n_folds)` -> contiguous (start, end) day ranges, sizes differ by <= 1 day, earlier folds
   take the remainder; refuses n_folds < 3 or n_folds > len(days). `cpcv_splits(n_folds)` -> all C(n,2) pairs in
   lexicographic order; `cpcv_paths(n_folds)` -> the C(n-1,1) paths, path p taking for fold j the p-th split (in
   lexicographic order) that has j as a test fold; return `(split_index, fold_index)` assignments per path.
   `CpcvSplit`/`CpcvPath` CanonicalModels. For n=6: 15 splits, 5 paths, every fold exactly once per path.
Tests (exact known dates): a 5-year series gives the folds you compute by hand (write the expected list in the
test from the arithmetic, not from the code); month-end clamping; zero folds for a 20-month series; 15/5 counts;
each path covers all 6 folds exactly once; fold sizes sum to the series length; purge shrinks windows.
Mutation-prove: off-by-one on a boundary, wrong remainder placement, step != 6, path assignment shifted.

## Task 3 - purge/embargo sampling (R-3), the splits command, acceptance, README
1. `split_samples(trades: TradeSample, days, folds, test_indices, *, purge_days, embargo_days)
   -> (train_idx, test_idx)` (index arrays into the sample). A trade's fold is the fold containing its exit_day.
   Test candidates: exit fold in test_indices; train candidates: exit fold not in test_indices.
   A TEST trade is excluded only at the boundary of its OWN exit fold F (start s): exclude iff fold F-1 exists
   and is a non-test fold, `purge_days >= 1` and `entry_day <= s - 1`; never for another test fold's boundary.
   (Amended 2026-10-01 after the 8C1 implementation found the original clause - "for any test fold ... exclude
   if entry_day <= s - 1" - incoherent: it dropped a test trade wholly inside an earlier test fold because of a
   later isolated test fold's boundary.)
   A TRAIN trade is excluded when, for any test fold [s, e], `entry_day <= e + embargo_days and exit_day >=
   s - purge_days` (this also removes trades lying entirely inside the zone).
   Document in the docstring that this is R-3 and why (fixed-parameter candidates make every path otherwise
   identical).
2. `path_return_series(series, folds, path)`: the held-out fold segments concatenated chronologically; assert-free
   check that no artificial zero day is inserted (length == sum of fold lengths, values are the series' own).
3. `path_trade_sample`: per path, for each fold j the test_idx of the assigned split restricted to exit fold j.
4. Command `research trial splits --trial-id T` (READ only, no help vocabulary of a decision, same gate as the
   other report commands): reads the trial's sealed constant-notional 1.0x bundle via the existing helpers
   (`sealed_bundles` etc. in ops/scenarios.py; exactly one required else the usual refusal), the registered
   protocol's ValidationSpec, builds the ReturnSeries/TradeSample and prints: series span and basis and
   promotion_grade, the WFA folds (or `"wfa": {"folds": [], "undefined_reason": "..."}` when none fit), the CPCV
   folds, the 15 splits, the 5 paths with, per path, the number of test trades kept and excluded. No statistic, no
   verdict.
5. `tests/acceptance/test_phase8c1.py` (real PostgreSQL) driving `splits` over a real sealed bundle (reuse the
   8B3 acceptance fixtures); zero-fold output for a short series; refusal on an unregistered trial; README section
   'Phase 8C1' + table row (tests assert the new sentences are PRESENT).
Mutation-prove: each exclusion clause (test-boundary, train purge side, train embargo side) neutered must fail ITS
test; a fixture where a trade straddles each zone is required for each.
