# Phase 8A.1 Implementation Plan

Spec: `docs/superpowers/specs/2026-09-30-phase-8a1-specification-vouching-design.md`
Rules: as in the 8B3 plan (zero `assert` in src/, CanonicalModel, every refusal fails closed, every test
mutation-proven, every task ends green, no scope creep, where code contradicts the brief the code wins - report it).

## Task 1 - vouch at append (V-1..V-6)
`research/ledger_store.py` `_append_operation`: for `EXECUTION_STARTED` on a trial declared by >=1
`PREREGISTERED` event, read the declared candidates inside the append transaction (same containment document
as `_lineage_parameters`), validate each as `TrialSpec`, and require `event.spec_sha256 ==
canonical_sha256(candidate)` for one of them (a set across registrations). Refuse with the ledger's existing
single `TrialLedgerAppendError` path (private cause names the trial id only). Legacy-only trials exempt.
Retry of an already-appended start (same event id) must still succeed - check the append function's
idempotency path is reached before or unaffected by the new check. Replay/verify untouched.

## Task 2 - repair the suite, add the proofs
Every existing test that starts a registered trial with an arbitrary digest uses the trial's real digest
(one shared helper, found via grep of `spec_sha256`/`execution_started_event` in tests/). Add the spec's
section 4 tests incl. the drifted-history case (insert a drifted start BEFORE this change's semantics by
writing the row through a test path that bypasses the check, e.g. a raw SQL/append function insert, then
assert replay and verify still pass). CLI: `research trial start` surfaces the refusal with the ledger's
exit code. README: correct the 'What Phase 8A does not implement' passage (effective_specifications is now
vouched for EXECUTION_STARTED of preregistered trials; still not for legacy imports or other event types)
and the 8B2b 'spec_sha256 is still unvouched in the ledger' bullet.

## Task 3 - acceptance
`tests/acceptance/test_phase8a1.py` (real PostgreSQL), modelled on test_phase8b3.py.
