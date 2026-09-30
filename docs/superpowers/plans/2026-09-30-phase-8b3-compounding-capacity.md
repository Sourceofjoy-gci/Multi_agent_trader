# Phase 8B3 Implementation Plan

Spec: `docs/superpowers/specs/2026-09-30-phase-8b3-compounding-capacity-design.md`

**Rules for every task** (house rules, from AGENTS/progress.md):
- `src/` contains zero `assert`; narrowing is `typing.cast`. Every model is a `CanonicalModel`.
- Every new test is mutation-proven: break the protected behaviour, watch the named test fail, restore,
  and put the failing output in your report.
- Every task ends green: `uv run pytest -q` (tests needing PostgreSQL use the container fixtures),
  `uv run ruff format --check .`, `uv run ruff check .`, `uv run mypy`, `git diff --check`.
- Do not widen scope. If a spec clause is wrong or unimplementable, stop and say so; do not improvise.
- The controller's brief has been wrong before. Where code contradicts the brief, the code wins; report it.
- Commit at the end of each task with a conventional message; do not push.

## Task 1 — sizing mode, engine compounding, scenario identity in run_id (defect 2)

Files: new `research/backtest/sizing.py` (`SizingMode` enum), `research/backtest/engine.py`,
`research/backtest/mark.py` (`BacktestOutcome.sizing`), tests under `tests/unit/research/backtest/`.

1. `SizingMode(str, Enum)` with `CONSTANT_NOTIONAL = "constant_notional"`, `COMPOUNDING = "compounding"`.
2. `BacktestRequest.sizing` defaults to constant-notional; engine passes
   `request.firm_equity` or `request.firm_equity + realized` to `evaluate_for_execution` (spec C-2).
   Non-positive sizing equity appends `("equity_exhausted",)` to rejections and `continue`s (C-3).
3. `_run_id` adds `"stress_multiplier": format(m.normalize(), "f")` only when `m != 1`, and
   `"sizing": "compounding"` only for compounding (C-4). A constant 1.0x request's id is unchanged.
4. `BacktestOutcome.sizing` (default constant). Keep the serialized form of a constant outcome
   byte-identical if any existing test pins it; use `exclude_if` like the bundle if needed.
5. Tests: see spec §6 bullets 1-5. Reuse the existing engine test fixtures; do not build a new harness.
   Record, before any change, the `run_id` and `digest()` of an existing known fixture and show they are
   byte-identical after.

## Task 2 — bundle `sizing`, report ignores compounding, stressed baseline named (defect 4)

Files: `research/evidence.py`, `ops/backtest.py` (`mark_to_market_bundle`), `ops/scenarios.py`, tests.

1. `EvidenceBundle.sizing` with `exclude_if` (spec 4.2) + the MARK_TO_MARKET validator.
2. `mark_to_market_bundle` sets `sizing=outcome.sizing`.
3. `scenario_report` filters to `CONSTANT_NOTIONAL` before grouping (C-5); `_scenario_report_for` in
   `cli.py` needs no change if the filter lives in `scenario_report`.
4. `ScenarioReport.declared_baseline_multiplier` (C-11). Update the pinned field-set tests.
5. Tests: spec §6 bullets 6, 7 and 12. The byte-identity of a constant bundle and the pinned v1 digest
   in `tests/property/test_trial_evidence.py` must still pass untouched.

## Task 3 — compounding report and capacity diagnostic

Files: new `ops/compounding.py`, new `research/validation/__init__.py`, `research/validation/capacity.py`,
architecture allowlist if `tests/acceptance/test_architecture.py` requires an entry for the new package
(it must not be allowed to import brokers/execution), tests.

1. `compounding_report(*, trial_id, protocol, constant, compounding)` where each is
   `(evidence_sha256, EvidenceBundle)`; refusals per C-8 raise `ScenarioEvidenceError` with the specifics
   on the private cause; `same_trade_sequence` and `final_equity_difference` per C-7 and spec 4.3.
2. `CapacityDiagnostic`/`CapacityStatus`/`capacity_diagnostic(protocol)` per C-9.
3. Tests: spec §6 bullets 8 and 9, each refusal clause with its own case.

## Task 4 — commands and the pre-flight (defect 3)

Files: `cli.py`, `ops/scenarios.py` (pre-flight helpers), tests under `tests/unit/test_cli.py` and
`tests/integration/research/`.

1. `research trial compounding`, `compounding-report`, `capacity`, per spec 4.4; reuse
   `_backtest_request`, `_instrument_contract`, `simulate`, `seal_bundle`; one attempt.
2. C-10 pre-flight in `scenarios` and `compounding` **before the first append**: file-digest equals
   registered-protocol digest; no level already sealed under another attempt id; `compounding` also
   requires a sealed constant-notional 1.0x bundle. Refusals are `ScenarioEvidenceError` (exit 19).
3. Extend the no-verdict help gate to the three new commands (find it from 8B2b's acceptance test).
4. Tests: spec §6 bullets 10, 11; row-count equality before/after the refused command.

## Task 5 — acceptance, README, verification

1. `tests/acceptance/test_phase8b3.py` modeled on `test_phase8b2b.py` (real PostgreSQL where that file
   uses it).
2. README: a *Phase 8B3* section; correct the three defect passages (run_id limit, file-edit limit,
   baseline-multiplier gap) so none claims what is no longer true.
3. Full gates, then the real operator flow against live PostgreSQL (controller does this).
