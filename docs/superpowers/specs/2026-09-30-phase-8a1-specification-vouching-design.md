# Phase 8A.1 — Ledger-Level Specification Vouching Design

**Status:** approved design (autonomous /goal run), pending plan
**Date:** 2026-09-30
**Predecessor:** Phase 8B3
**Umbrella:** `docs/superpowers/specs/2026-09-25-phase-8-validation-design.md` §5.3, §5.6
**Closes:** defect 1 of the four recorded defects (see the 8B3 design §2)

## 1. Why this slice exists, and why before 8C

README, *What Phase 8A does not implement*: `effective_specifications` "is a count of supplied digests,
not a verified match". §5.6 defines DSR's trial count as the conservative maximum of selection
lotteries and effective specifications. 8C divides by that number. A denominator an operator can
inflate or deflate by typing a digest is not a denominator, so the ledger has to *vouch* before anything
divides by it.

The information to vouch with already exists in the chain: a `PREREGISTERED` event seals the protocol's
whole candidate tuple, and `canonical_sha256(TrialSpec)` is deterministic. 8B2b already uses exactly that
expression at read time. This slice moves the same comparison to **write** time, inside the append
transaction, where it cannot be skipped by a caller that goes around the CLI.

### 1.1 Not in this slice

- No change to `LEGACY_IMPORTED`. A legacy trial has no preregistration; its digest is whatever the
  importer derived, and there is nothing declared to compare it with. Stated, not papered over.
- No change to events other than `EXECUTION_STARTED`. Only starts feed `trial_counters`; a sealed or
  failed event carrying a different digest does not move a counter, and widening the rule would widen the
  blast radius of this change for no denominator gain.
- No migration, no new event type, no change to replay or verify: events already in a chain are never
  re-judged. A past drift (the local chain's `att-b2`) stays exactly as counted; it is history.

## 2. Decisions

| # | Decision | Why |
|---|---|---|
| V-1 | `append` refuses an `EXECUTION_STARTED` whose `spec_sha256` is not `canonical_sha256` of a candidate that some `PREREGISTERED` event declares under that `trial_id` | The rule lives where the row is written; the CLI gets it for free |
| V-2 | "Declared under that trial id" is **the set** of digests across every registration naming the trial | Phase 8A permits registering one trial twice (8B2b found this). A set is the honest generalisation; first-match is the defect the 8B2b review already named |
| V-3 | The refusal is `TrialLedgerAppendError` with an opaque public message; the private cause is the store's fixed `_LedgerStoreFailure` and names nothing, not even the trial id | The ledger's contract is one redacted error; the digest values are not secret but the store does not narrate its inputs |
| V-4 | A trial declared *only* by a legacy import is exempt | See 1.1 |
| V-5 | The check runs on the append's own connection and transaction | It precedes the append function's advisory lock, so a registration landing concurrently may be missed; that can only refuse a start, never admit one |
| V-6 | A start already in the chain with byte-identical canonical bytes skips the check, so its retry succeeds; a same-id event with different bytes does not skip and reaches the append function's conflict path | The vouch runs before the append function's idempotency, so without the skip a pre-8A.1 drifted start, or one whose trial was registered after a legacy import, would be refused on retry |

## 3. Implementation notes

The containment SQL `_LINEAGE_SQL` answers "declared at all". V-1 needs the declared candidates, so a
second read selects the `payload.protocol.candidates` arrays of the `preregistered` events that name the
trial (same containment document `_lineage_parameters` builds, one place), and the store validates each
matching element as a `TrialSpec` and hashes it. Validation failure of a stored document is an append
failure, not a pass.

The check sits beside `_is_registered` in `_append_operation`. A start for a trial that is declared only
by legacy import skips it (V-4).

## 4. Tests and proof

Mutation-checked:

- a start whose digest matches the declared candidate is appended (integration, real PostgreSQL);
- a start whose digest matches no candidate of a registered trial is refused and appends no row;
- two registrations of one trial declaring two different specs: both digests are accepted, a third is
  refused (V-2);
- a legacy-only trial's start is still appended (V-4);
- a retry of an appended start succeeds (V-6);
- the local-chain drift scenario: replay/verify of an existing chain holding a drifted start still
  verifies (history is not re-judged);
- the CLI `research trial start` surfaces the refusal with the ledger's existing exit code;
- every existing test that appended a start with an arbitrary digest against a registered trial now uses
  the trial's real digest (a helper, not a magic constant per test).

## 5. Known limits this slice leaves

- A start for a registered trial can still be a **different** but declared candidate's digest if the two
  are registered under the same trial id; V-2 accepts that by design, and the report-side identity check
  (8B2b) is what ties a bundle to one of them.
- `EVIDENCE_SEALED`, `RESULT_RECORDED`, `FAILED` digests remain client-asserted (1.1).
