# Phase 8D1 Implementation Plan - policy digest, holdout derivation, nine gates, report, decision

Spec: `docs/superpowers/specs/2026-10-01-phase-8d-promotion-workflow-design.md` (P-1..P-12, section 6).
Umbrella §8.1-§8.4, §10. Rules as in the 8C plans: zero `assert` in src/, every pydantic model a CanonicalModel,
every refusal fails closed and typed, every test mutation-proven, every task ends green, no scope creep, where the
code contradicts the brief the code wins - report it. Thresholds come ONLY from `research/validation/policy.py` and the
protocol's `ValidationSpec`; nothing is an argument or an option. `research/promotion.py` is a pure module: no I/O,
no ledger, no store, no clock.

## Task 1 - models, policy digest, holdout derivation (`research/promotion.py`)
1. `GateStatus(str, Enum)`: PASS, FAIL, UNAVAILABLE (keep `# noqa: UP042`). `GateResult` (P-2), validated:
   PASS needs a defined finite `measured_value` and a non-empty `evidence_sha256`; UNAVAILABLE carries
   `measured_value` only if one exists; `reason` non-empty always.
2. `Decision(str, Enum)`: REJECTED, RESEARCH_PASSED, PAPER_APPROVED (the three values `GateDecidedPayload` accepts -
   assert equality of the value sets in a test, not in src).
3. `policy_sha256(spec: ValidationSpec) -> str`: `canonical_sha256` of a small CanonicalModel `PolicyDigestInput`
   holding every signed constant from policy.py and the spec's own fields. A test pins the digest for a known spec
   as a literal AND asserts that moving any one constant or any spec field moves it.
4. `HoldoutStatus` (CanonicalModel: state, reason, opened_bundle_count) and
   `derive_holdout(trial_id, events: Sequence[LedgerEvent], sealed_holdout_states: Mapping[str, HoldoutState])`
   implementing P-8 exactly (legacy => CONTAMINATED; protocol NOT_DEFINED/LOCKED; protocol OPENED/CONSUMED/
   CONTAMINATED => CONTAMINATED; one OPENED bundle => OPENED; OPENED then a later GATE_DECIDED => CONSUMED; two
   opened bundles => CONTAMINATED). Event ORDER is the sequence in `events` (replay order). `sealed_holdout_states` maps
   an evidence digest to that bundle's `provenance.holdout_state` (the caller reads it).
Tests: one per transition and per contamination route, written from the table in P-8; an OPENED holdout never goes
back; CONSUMED needs the later decision; order matters (a decision BEFORE the opening does not consume it).
Mutation-prove each branch.

## Task 2 - the nine gates and the decision (`research/promotion.py`)
1. `GateInputs` (CanonicalModel): `trial_id`, `policy_sha256`, `evidence: StatisticalEvidence | None` (8C3 model),
   `holdout: HoldoutStatus`, `holdout_expectancy_1_5: Measurement | None`, `holdout_expectancy_2_0: Measurement | None`
   (always None in 8D1; 8D2 fills them), `facts: EvidenceFacts`. `EvidenceFacts`: `registration_state`
   (RegistrationState), `dataset_sha256_present: bool`, `return_series_basis`, `cost_status`, `baseline_net_expectancy:
   float | None`, `baseline_evidence_sha256`.
2. `evaluate_gates(inputs) -> tuple[GateResult, ...]` (exactly nine, in order, P-5). Each gate reads its measurement
   from `inputs.evidence`; `evidence is None` => every research gate UNAVAILABLE with reason 'no statistical evidence'.
   Boundary semantics, each tested on BOTH sides and exactly at the boundary: DSR >= 0.95 passes at 0.95; PBO <=
   0.50 passes at 0.50; drawdown strictly < 0.10 (0.10 FAILS); expectancies and bootstrap p5 strictly > 0; trades >= 30;
   regimes >= 2; folds >= 1. Gate 7 needs baseline, every CPCV path, compounding AND both Monte Carlo measurements
   defined: any missing => UNAVAILABLE; any drawdown >= 0.10 => FAIL (FAIL wins over UNAVAILABLE only when a
   measured value is on the wrong side - say which in the reason). Gate 2: holdout state not OPENED/CONSUMED =>
   UNAVAILABLE with the state in the reason; both holdout expectancies > 0 => PASS else FAIL. Gate 5's reason states
   when the CPCV paths were identical (P-6). Gate 9 reads the capacity diagnostic: UNAVAILABLE status => UNAVAILABLE.
   A policy digest in the inputs that differs from the recomputed one is a `PromotionRefusedError` (see task 3's
   error), raised before any gate is evaluated.
3. `blocking_reasons(facts, holdout) -> tuple[str, ...]` (P-11): fixed vocabulary of exactly six strings (define them as
   module constants): negative expectancy at baseline costs (baseline_net_expectancy < 0); no locked holdout (state is
   NOT_DEFINED or CONTAMINATED); legacy evidence was not preregistered; dataset-content hash unavailable; mark-to-market
   returns unavailable (basis != MARK_TO_MARKET); spread and slippage cannot be separately attributed (cost_status !=
   COMPLETE). Order fixed. Each independently triggerable.
4. `decide(results, holdout, reasons) -> Decision` (P-7): REJECTED if any reason; else PAPER_APPROVED iff all nine PASS;
   else RESEARCH_PASSED iff all eight research gates PASS and holdout.state is LOCKED; else REJECTED. Property test
   (Hypothesis over the 3^9 status vectors x holdout states x reason subsets, small): the decision is a pure function of
   its inputs; RESEARCH_PASSED never occurs with a non-LOCKED holdout; PAPER_APPROVED never with any non-PASS or any
   reason. Mutation-prove each rule.

## Task 3 - the report, its store, the events, the error
1. `PromotionRefusedError(TradingHouseError)` in core/errors.py, opaque public message, stable exit code in
   `cli.EXIT_CODES` (a test enforces every typed error has one).
2. `ValidationReport` (P-9) with `report_schema_version: Literal[1]`; its digest is `canonical_sha256(report)`; NO field
   holds its own digest (the digest is derived). `EvidenceStore` gains `write_report(report)` and `read_report(digest)`
   using the SAME root, layout, atomic link-publish and domain-separated digest as bundles; `read_report` refuses a file
   that is not a canonical ValidationReport (including a bundle's digest), and `read` refuses a report's digest (tests
   both ways, plus tamper/missing -> EvidenceIntegrityError).
3. Event builders in `ops/ledger.py`: `validated_event(trial_id, attempt_id, spec_sha256, report_sha256, occurred_at)`
   and `gate_decided_event(..., decision)`; ids are content-derived (uuid5 over trial, report digest, event type) so a
   rerun is an idempotent no-op; scope ATTEMPT with the baseline attempt id.

## Task 4 - assembly, commands, verify (`ops/decide.py`, cli)
1. `assemble_gate_inputs(...)`: reads (chain replay, protocol, 8C3 assembly for a prospective trial; the sealed legacy
   bundle for a legacy trial) and returns `GateInputs`; a legacy-imported trial has `evidence=None` and facts read from
   its sealed bundle. `sealed_holdout_states` built from the trial's EVIDENCE_SEALED digests.
2. `research trial decide --trial-id T --occurred-at O`: builds the report, seals it, appends VALIDATED then
   GATE_DECIDED (P-10); prints the report digest, the decision, all nine gate results and the blocking reasons; rerun
   with identical evidence is a no-op (assert row/file counts unchanged). It does NOT create a package, change a
   stage, or take a stage option.
3. `research trial report --trial-id T`: read the latest report for the trial from the chain + store and print it.
4. `research trial holdout --trial-id T`: print the derived HoldoutStatus.
5. `research trial verify` re-reads every report digest named by a VALIDATED or GATE_DECIDED event through `read_report`
   (a missing or altered report exits as it already does for evidence, 17).
6. Help text of the new commands carries no decision vocabulary EXCEPT where the command's job is the decision - the
   no-verdict gate currently bans the stems; `decide`/`report` legitimately speak in decisions. Add them to a SEPARATE
   allowlist in the gate (named, with the reason), and keep `splits`/`validate`/`scenario-report`/etc. covered by the
   existing total ban. Do not weaken the existing gate for the other commands.
Tests: unit tests with builders; integration tests per refusal with row/file equality; the legacy path end to end
(import a legacy artifact, `decide`, assert REJECTED with all six reasons and nine non-PASS gates); the prospective
path over a real sealed run (decision REJECTED today because capacity is UNAVAILABLE, holdout NOT_DEFINED);
determinism (same inputs => same report digest, byte for byte, across two processes).

## Task 5 - acceptance, README, ledger-ready operator flow
`tests/acceptance/test_phase8d1.py` (real PostgreSQL), README 'Phase 8D1' section + commands rows (tests assert the
sentences are PRESENT): the nine gates and their thresholds sourced from policy, the three-valued decision, the six
reasons, that a pass moves no stage, the limits of spec section 7.
