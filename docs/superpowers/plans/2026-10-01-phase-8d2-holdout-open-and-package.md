# Phase 8D2 Implementation Plan - the one-time holdout opening, the package contract, final acceptance

Spec: `docs/superpowers/specs/2026-10-01-phase-8d-promotion-workflow-design.md` (P-7, P-8, section 4, section 7).
Umbrella §8.1 (opening), §8.2, §8.3 (package), §10. Rules as in the 8D1 plan. Builds on 8D1's `research/promotion.py`
(`derive_holdout`, `evaluate_gates`, `decide`, `ValidationReport`), the report store, `decide`/`report`/`holdout`.

## What this slice must be honest about, up front
Gate 9 (capacity) is `UNAVAILABLE` for every candidate (8B3), and P-7 makes gate 9 a research gate, so
**no real candidate can reach `RESEARCH_PASSED`, therefore none can open a holdout, therefore none can reach
`PAPER_APPROVED`.** That is the framework failing closed as designed, not a defect. The consequence for testing: the
opening and the package rules cannot be exercised by a real decision today. They are proven by tests that
build a sealed report with all research gates `PASS` and append real `VALIDATED`/`GATE_DECIDED` events through the
real ledger (a named test helper; never reachable from a command), and the README and the final report say so.

## Task 1 - `research trial open-holdout`
1. `mark_to_market_bundle(..., holdout_state=...)`: the bundle's `provenance.holdout_state` and `provenance.dataset_sha256`
   become parameters (defaults unchanged: NOT_DEFINED / None, so no existing digest moves - assert the pinned digests).
2. The command: same option set as `scenarios` (identity/provenance options, no option for any value the protocol owns;
   the window is `protocol.holdout.start/end`, the costs are `costs.baseline` at each grid level). Refuses BEFORE any
   write unless: the derived holdout is `LOCKED` (8D1's `derive_holdout`); the latest `GATE_DECIDED` for the trial is
   `RESEARCH_PASSED` and its report is readable and its policy digest equals the recomputed one; no bundle with holdout
   state OPENED/CONSUMED exists for the trial; the run's replay inputs equal the research baseline's (reuse the shared
   REPLAY_FIELDS predicate); every request is built and validated first; provenance options are validated first. A second
   invocation is refused before any write. Reuse `_seal_levels`' machinery where it fits (do not copy it); the faithfulness
   check for these bundles compares the window against `protocol.holdout`, not `protocol.data` - make that a parameter of the
   shared check, with tests that a research bundle is still refused when its window is the holdout's and vice versa.
   Seals three bundles (1.0x, 1.5x, 2.0x), each with `holdout_state=OPENED` and the holdout's declared dataset hash recorded in
   provenance (state in the README that the hash is DECLARED, not computed: nothing in this repository can hash a bar store,
   so umbrella §10's 'dataset hash mismatch' check is not implementable and is NOT done).
   [Superseded by Phase 8E (`docs/superpowers/specs/2026-10-03-phase-8e-dataset-digest-design.md`): the digest is now
   computed, the opening refuses a window whose stored bars do not hash to the declared holdout hash before its first
   write, and each opened bundle carries the digest its run computed. The 8D2 statement stays true only for runs
   sealed before 8E and for legacy imports.]
3. After opening, `decide` (8D1) must read the opened 1.5x and 2.0x bundles (exactly one each, else UNAVAILABLE with the reason)
   and feed `holdout_expectancy_1_5/2_0` (the 8C3 `scenario_expectancy` measurement over the opened bundle's sample); gate 2 then
   evaluates; a later `GATE_DECIDED` moves the derived holdout to `CONSUMED`.
   **Any `decide` run after a sealed OPENED bundle appends a `GATE_DECIDED` and thereby CONSUMES the holdout** (8D1's
   `derive_holdout`: OPENED followed by any decision of the trial). So an un-informed `decide` (one that cannot supply
   gate 2's expectancies) would spend the holdout for nothing. Therefore: `decide` REFUSES (`PromotionRefusedError`,
   exit 21, nothing written: no report, no event) whenever a sealed OPENED bundle exists for the trial but the opened
   1.5x and 2.0x bundles are not BOTH present exactly once; this replaces the "else UNAVAILABLE" above for that case. And the
   opening workflow must make `decide` supply the holdout expectancies in the same flow: `open-holdout` must not
   leave a window in which an un-informed `decide` can consume the holdout (seal the three bundles, then the operator's next
   step is `decide`, which is the informed one; test that `decide` after only a partial opening is refused and writes
   nothing, and that the derived state is still OPENED afterwards). Specified here, NOT implemented in 8D1.
Tests (with the named synthetic-RESEARCH_PASSED helper): each refusal with row/file equality (not LOCKED, no RESEARCH_PASSED,
already opened, other replay inputs, holdout window outside bar coverage); the happy path seals three OPENED bundles and the
derived state is OPENED; a second call is refused; `decide` afterwards evaluates gate 2 PASS and FAIL both ways; the derived state
becomes CONSUMED after that decision; opened bundles change nothing in `scenario-report`/`splits`/`validate`/`compounding`
(the 8C3 filter) - re-prove end to end. Mutation-prove every refusal and the OPENED provenance.

## Task 2 - package contract and commands
1. `StrategyPackage` gains `authorization_ref: NonEmptyStr | None` (human paper authorization) and
   `capital_authorization_ref: NonEmptyStr | None`; validators: `PAPER` requires `authorization_ref`; `LIVE` requires
   `authorization_ref`, `capital_authorization_ref` AND the existing `signature_sha256`; `SANDBOX` carries none of the three
   authorizations (a sandbox package claiming an authorization is refused). Existing tests/semantics for the signature rule
   stay; tests for each combination.
2. `create_package(decision_report, ..., stage, ...)` pure in `research/promotion.py`: `PAPER` requires the report's decision
   `PAPER_APPROVED`, nine PASS gates, no blocking reasons, an authorization ref; `LIVE` requires an existing valid PAPER package
   (passed in), the signature ref and a capital authorization ref DIFFERENT from the paper one; any automatic route to `LIVE`
   (no paper package, no signature, equal refs) is a `PromotionRefusedError`. `SANDBOX` is what a REJECTED decision yields and is the
   default recorded stage.
3. Commands (read the chain, write a JSON file only, append NOTHING): `research package create --trial-id T --stage PAPER|LIVE
   --authorization-ref R [--capital-authorization-ref C --signature-sha256 S --paper-package FILE] --out FILE` (refuses to overwrite),
   `research package verify --file FILE` (re-derives against the chain: the report digest is named by a VALIDATED event and by the
   latest GATE_DECIDED, the decision is PAPER_APPROVED, `trial_ledger_reference` names a chain head hash that exists in the chain,
   stage and references are consistent, `source_sha256` equals the protocol's strategy hash). `StrategySpec` fields the protocol does not
   carry (book, horizon, asset classes) come from options validated against their enums, with `spec_id`/`hypothesis` from the
   candidate - find the enum types in core/values.py; do not invent a default.
   Neither command signs anything or checks that a person made the authorization (a reference, §8.3).
Tests: each stage/reference rule on both sides; `create` refuses a REJECTED or RESEARCH_PASSED decision, refuses LIVE without each
of its three requirements, refuses equal authorization refs, refuses overwrite; `verify` refuses a package whose report digest is
altered, whose decision is not PAPER_APPROVED, whose ledger reference is not in the chain; the full chain is untouched by both
commands (row/file equality). Mutation-prove each.

## Task 3 - final acceptance of Phase 8, README, operator documentation
`tests/acceptance/test_phase8_final.py` (real PostgreSQL): the whole story on a synthetic locked window: register with a LOCKED
holdout, run the grid, `validate`, a synthetic RESEARCH_PASSED, `open-holdout`, `decide` -> gate 2, a synthetic PAPER_APPROVED,
`package create` PAPER, `package verify`; and the REAL path: a legacy-imported Session-Momentum-shaped trial `decide`s REJECTED with all
six reasons and cannot produce a package; a real prospective candidate `decide`s REJECTED (capacity, holdout). README: a closing
'Phase 8 - what exists, what blocks promotion, what a human must supply' section stating the consequences above plainly (no candidate can
currently be promoted: capacity model, a real locked holdout, and human signatures are each missing and each lives outside this repo).
