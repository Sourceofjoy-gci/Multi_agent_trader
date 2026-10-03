# Phase 8E Implementation Plan - the computed dataset digest

Spec: `docs/superpowers/specs/2026-10-03-phase-8e-dataset-digest-design.md` (E-1..E-10). Rules as in the 8D plans: zero
`assert` in src/, every pydantic model a CanonicalModel, every refusal fails closed, typed, opaque, BEFORE any write, every
test mutation-proven, every task ends green, no scope creep, where the code contradicts the brief the code wins - report it.
No pinned digest may move (the v1 bundle digest, the four Phase 7 constants, the engine fixture's run_id/digest literals).

## Task 1 - the digest and the shared window read
1. `research/dataset.py`: `dataset_sha256(bars)` per E-1/E-3 (typed refusals: empty, non-increasing times, mixed
   instrument/timeframe). Canonical bytes by the research rules; do NOT import ops.
2. In `research/backtest/engine.py` factor `Backtester._replay_bars` into a public module-level `replay_window_bars(reader,
   instrument_id, timeframe, start, end)` with identical behaviour; `Backtester` calls it. Prove the engine's existing
   tests and pinned digests pass untouched.
Tests: hand-built bars; each of the twelve fields changed alone changes the digest; order, drop, duplicate; boundary bars
in/out exactly as the replay reads; cross-process stability (subprocess, two PYTHONHASHSEEDs); known-answer literal for a
tiny fixed bar set computed by an independent standalone script (paste its source in the test docstring, no repo imports).

## Task 2 - engine, outcome, bundle
`BacktestOutcome.dataset_sha256: str | None = Field(default=None, exclude_if=...)` (absent from the serialization when
None); the engine sets it from the bars it replays; `ops/backtest.mark_to_market_bundle` writes it into
`provenance.dataset_sha256` (still None when the outcome has none). Proofs: every pinned digest unmoved; a bundle from a
real run now carries the digest; the digest equals `dataset_sha256(replay_window_bars(...))` computed independently.

## Task 3 - pre-flight, post-run agreement, faithfulness, decide
1. `ops/dataset.py`: `window_digest(...)`, `refuse_dataset_mismatch(declared, computed)` (ScenarioEvidenceError, both
   digests on the private cause). Pre-flight (E-5) in `scenarios`, `compounding`, `open-holdout` BEFORE the first append
   (declared = `protocol.data.dataset_sha256`; for the opening `protocol.holdout.dataset_sha256`); post-run agreement (E-6).
2. `refuse_unfaithful`: a bundle carrying a dataset digest must carry the declared one (E-7); `None` is not refused there.
3. `decide`: mismatch => PromotionRefusedError; matching digest clears the 'dataset-content hash is unavailable' reason;
   `None` keeps it (E-8). Update `EvidenceFacts` semantics and docs only as the spec says.
4. EXISTING TESTS: every protocol the suite builds declares a placeholder hash (e.g. "a"*64). They must now declare the REAL
   digest of their fixture window through ONE shared test helper (compute it from the seeded bars); do not weaken any
   assertion; add one test per command that a placeholder is refused with row/file equality.
Mutation-prove: each refusal, the post-run agreement, E-7, E-8 both directions.

## Task 4 - `research dataset digest`, README, acceptance
`research dataset digest` (READ only; same bar-store wiring as `backtest run`; add it to the right no-verdict help list and
keep the completeness test passing; refuse an empty window as exit 20). `tests/acceptance/test_phase8e.py` (real
PostgreSQL): the digest the command prints equals the digest a real run seals; a protocol declaring that digest runs; one
declaring another is refused before any write in all three commands; `decide` over such a run no longer carries the
'dataset-content hash is unavailable' reason; legacy stays unavailable. README: a 'Phase 8E' section, the commands row, and
correct EVERY passage that says the dataset hash is declared-not-computed or that every real trial carries the unavailable
reason (8D1/8D2/closing sections, the 'dataset hash mismatch is not implementable' statements): they become true only for
legacy imports and for runs sealed before 8E. Tests assert the new sentences are PRESENT.
