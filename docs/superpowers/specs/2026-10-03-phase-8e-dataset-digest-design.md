# Phase 8E — Computed Dataset Digest Design

**Status:** approved design (user approved the slice on 2026-10-03), pending plan
**Date:** 2026-10-03
**Predecessor:** Phase 8D2
**Umbrella:** `docs/superpowers/specs/2026-09-25-phase-8-validation-design.md` §5.1 (dataset-content SHA-256), §8.1 (holdout dataset hash), §10 ("dataset hash mismatch for a prospective trial" is a hard error)

Section references are to the **umbrella design** unless prefixed "this spec".

## 1. What this slice is for

Every protocol declares a `dataset_sha256` and every holdout declares one, and nothing in this repository has ever
**computed** one. Every real `backtest run` seals `provenance.dataset_sha256 = None`, so every real prospective trial
carries the blocking reason "dataset-content hash is unavailable", and umbrella §10's "dataset hash mismatch" error
could not be implemented (8D2 states it as not done). The declared hash was a promise nobody could check.

8E computes it. The digest is a deterministic, domain-separated SHA-256 over **exactly the bars the replay reads** for
a window, and it is checked: a run whose computed digest differs from the protocol's declared one is refused before
any write; a holdout opening whose window digest differs from the declared holdout hash is refused before any write;
and a sealed bundle now carries the digest it was actually run on.

### 1.1 Not in 8E

- No claim about where the bars came from: the digest covers the **stored** bars. A bar store re-ingested with
  corrected bars has a different digest, and that is the point (fail closed), not a defect.
- No change to `BacktestResult`, to any pinned digest, or to legacy imports (a legacy artifact's dataset stays
  `unavailable`, §5.7).
- No new ledger event, no migration, no new dependency.
- No collection of future data for a holdout (operational, outside this repository).

## 2. Decisions

| # | Decision | Why |
|---|---|---|
| E-1 | `research/dataset.py`, pure: `dataset_sha256(bars)` = `sha256(DOMAIN + canonical bytes of the ordered per-bar records)`, `DOMAIN = b"trading-house:dataset:v1"`, per bar: instrument, timeframe, event time, availability time, open, high, low, close, tick volume, spread, real volume, quality, with Decimals as `format(value, "f")` and UTC `Z` timestamps, keys sorted, compact separators (the research canonical rules) | One rule for canonical bytes; the digest of a window must not depend on the process |
| E-2 | The input is exactly what `Backtester._replay_bars` reads: `include_defective=True`, range `[start, end + one bar)`, `as_of = end + one bar`. That read becomes one module-level function in `research/backtest/engine.py` used by both the engine and the digest | The digest names the data the replay used; two reads that could differ would make the digest a claim about something else |
| E-3 | Refuses (typed `StatisticalInputError`-class, exit 20; new error not needed) an empty window, duplicate or non-increasing event times, bars of more than one instrument or timeframe | A digest of nothing, or of an ambiguous series, is not a content hash |
| E-4 | The engine computes the digest from the bars it replays and the outcome carries it (`BacktestOutcome.dataset_sha256`, absent from the serialized form when `None`, so every existing serialization is byte-identical); `mark_to_market_bundle` writes it into `provenance.dataset_sha256` | A sealed bundle states the data it ran on, computed, not typed |
| E-5 | **Pre-flight, before the first append**, in `scenarios`, `compounding` and `open-holdout`: the digest of the window is computed from the store and must equal the protocol's declared `data.dataset_sha256` (for the opening: `holdout.dataset_sha256`). A mismatch is a `ScenarioEvidenceError` (exit 19) naming both digests | §10; the one-way-door lesson from 8B2b: refuse before the writes |
| E-6 | After the run, the engine's computed digest must equal the pre-flight's (the store did not change in between); a difference is a refusal | Closes the read-then-run race the pre-flight alone leaves |
| E-7 | `refuse_unfaithful` (the shared faithfulness check) additionally requires a bundle that **carries** a dataset digest to carry the protocol's declared one. A bundle with `None` is not refused there (older bundles exist) — it is handled by the blocking reason | Backward-compatible; a present-but-wrong digest is evidence from other data |
| E-8 | `decide`: a baseline bundle carrying a digest that differs from the protocol's declared one is a `PromotionRefusedError` (exit 21, nothing written); a baseline carrying the declared digest **clears** the "dataset-content hash is unavailable" reason; `None` keeps it | The reason now means what it says |
| E-9 | `research dataset digest --instrument I --timeframe T --start S --end E` (READ only): prints the digest, the bar count and the first/last bar times, so an operator can declare a protocol's hash from the store rather than typing one | The workflow that makes the declared hash non-fictional |
| E-10 | Protocols already in a chain with placeholder declared hashes stay valid history; a new run against them is refused (correct), and nothing rewrites them | Append-only |

## 3. Interfaces

`dataset_sha256(bars: Sequence[Bar]) -> str`; `replay_window_bars(reader, instrument_id, timeframe, start, end)`
(public in `research/backtest/engine.py`, used by `Backtester`); `ops/dataset.py`: `window_digest(bars_reader, ...)`
(reads through the same function) and `refuse_dataset_mismatch(declared, computed)`. `FactsOf` in 8D's `EvidenceFacts`
keeps `dataset_sha256_present`; its meaning becomes "the baseline carries the protocol's declared digest".

## 4. Errors

Mismatch refusals reuse `ScenarioEvidenceError` (exit 19, opaque message, both digests on the private cause) at the
run/open pre-flight and `PromotionRefusedError` (exit 21) at `decide`. A malformed window reuses
`StatisticalInputError` (exit 20).

## 5. Tests and proof

Mutation-proven: the digest is stable across processes and key order; changing any single field of any single bar
(each of the twelve) changes it; reordering, dropping or duplicating a bar changes or refuses; the defective bar is
included; the window boundaries are inclusive/exclusive exactly as the replay reads them (a bar at `end` is in, a bar
at `end + 1 bar` is out); a declared placeholder hash is refused before any write (row and file equality) in all three
commands; the engine and the pre-flight agree; `decide` clears the reason only on a matching digest and refuses a
mismatching one; `dataset digest` equals the digest a real run seals; legacy imports still say unavailable; every
pinned digest in the suite is unmoved.

## 6. Known limits

- The digest proves the replay's input bytes, not their source or that the bars are real market data.
- A bar store that is later corrected invalidates every earlier run's digest by design.
- Existing trials whose protocols declared placeholder hashes cannot be re-run; they remain readable.

## 7. As built (2026-10-03)

Where the implementation differs from the decisions above, the implementation is the authority:

- **E-1 (module).** The digest is `dataset_sha256` in `research/backtest/dataset.py`, not
  `research/dataset.py`: the engine must call it and `BACKTEST_ALLOWED` admits
  `trading_house.research.backtest` but not `trading_house.research`. For the same reason the
  canonical-bytes rules are restated there (twelve fields, sorted keys, compact separators,
  `Decimal` as `format(value, "f")`, UTC `Z` stamps) and not imported from `research/canonical.py`.
- **E-4 (outcome field).** `BacktestOutcome.dataset_sha256` is `Field(default=None, exclude=True)`,
  not `exclude_if`: it is carried in memory and never serialised. The engine always sets it, so a
  conditional exclusion would have moved every pinned outcome byte.
- **E-4 (bundle).** `mark_to_market_bundle`'s `dataset_sha256` parameter was removed; the bundle
  takes `outcome.dataset_sha256` only (computed, never typed). `backtest run --mark-to-market`
  therefore seals the computed digest too.
- **E-7 and E-8.** E-7 pre-empts E-8 inside `decide`: the statistical read's faithfulness check would
  refuse a baseline carrying another digest as exit 19 first. `refuse_other_dataset` therefore runs
  before the statistical read: the baseline is refused with exit 21, every other bundle (compounding,
  stressed, other candidates) with exit 19.
- **E-9 (coverage).** `window_digest` refuses a window the store does not hold with `CoverageError`
  (exit 10), which also moves `scenarios` and `compounding` on such a window from an orphan start row
  plus a simulator refusal to a refusal before any write.
- **E-3 in the engine.** `Backtester.run` over an in-coverage window that holds no bar now raises
  `StatisticalInputError` (exit 20) from the digest, where it used to fail construction of the outcome
  with a validation error.
- **Third orphan source.** The post-run agreement (E-6) refuses after the start row is appended:
  one row more, no file more, and the attempt id is spent.
