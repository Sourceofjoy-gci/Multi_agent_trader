# Phase 8D — Promotion and Operator Workflow Design

**Status:** approved design (autonomous /goal run), pending plans
**Date:** 2026-10-01
**Predecessors:** Phase 8C (statistical measurements), 8B3 (compounding, capacity state), 8A.1 (vouched starts)
**Umbrella:** `docs/superpowers/specs/2026-09-25-phase-8-validation-design.md` §8.1–§8.4, §9, §10

Section references are to the **umbrella design** unless prefixed "this spec".

## 1. What this phase is for

8A–8C produced sealed evidence and measurements. 8D is the first thing that **judges**, and the only thing that
may. It turns the measurements into nine gate outcomes against thresholds fixed before any result existed,
records one immutable validation report and one decision on the ledger, and gives a human the only path from
a passing candidate to `PAPER` and, separately and with a signature, to `LIVE`. It also records what the
framework was built to be able to say about Session Momentum: `REJECTED`, stage `SANDBOX`, with the reasons.

A pass never moves a stage. Only a package carrying a human authorization moves one, and nothing here creates
a `LIVE` package without a signature reference and a capital authorization that is separate from the paper one.

### 1.1 Not in 8D

- No signing (a signature is a reference to be verified, §8.3), no scheduling, no paper/live execution.
- No new ledger event type and no migration: the event vocabulary is a database CHECK, and `VALIDATED` and
  `GATE_DECIDED` already exist and carry exactly a report digest and a decision.
- No second evidence store: the report is a second *document kind* in the existing content-addressed
  `EvidenceStore` (same root, same atomic write, same digest rule), not a second store.
- No collection of a real holdout, no new data (§13: future holdout collection is an operational phase).

## 2. Delivery split

| Slice | Content | Umbrella |
|---|---|---|
| **8D1** | the signed policy digest; holdout lifecycle derived from the chain; the nine gates as a pure function; the immutable `ValidationReport` and its store; `research trial decide`, `report`, `holdout`; the legacy blocking reasons and the recorded Session Momentum decision | §8.1 (derivation), §8.2, §8.3 (report), §8.4, §10 |
| **8D2** | `research trial open-holdout` (the one-time opening that runs the cost grid on the locked window); `StrategyPackage` authorization contract; `research package create/verify`; the final acceptance of the whole of Phase 8 | §8.1 (opening), §8.3 (package) |

## 3. Decisions

| # | Decision | Why |
|---|---|---|
| P-1 | Gates are a **pure function** `evaluate_gates(GateInputs) -> tuple[GateResult, ...]` in `research/promotion.py`; reads happen in `ops/`, never inside the function | The same separation 8B2b/8B3 used; a gate that reads the chain can answer differently twice |
| P-2 | `GateResult`: `gate` (1..9), `name`, `status` ∈ {`PASS`,`FAIL`,`UNAVAILABLE`}, `measured_value: float \| None`, `threshold: float \| None`, `evidence_sha256: tuple[str, ...]`, `reason: str` | §8.2 verbatim |
| P-3 | `UNAVAILABLE` is for evidence that is missing, stale, undefined or non-finite or whose precondition (a locked holdout, a capacity model) does not exist. `FAIL` is for a **measured** value on the wrong side of its threshold. Both block. `PASS` requires a defined, finite, correct-side measurement and nothing else | "Missing, undefined or non-finite evidence must never read as a pass" |
| P-4 | Thresholds are the 8C `policy.py` constants and the protocol's own `ValidationSpec`; nothing is an argument. The report embeds a **policy digest** (canonical sha256 of the constants and the spec); evaluation refuses a package whose digest differs from the one recomputed | §10 "changed validation thresholds after registration" |
| P-5 | The nine gates, in order: (1) WFA complete: at least `MIN_WFA_FOLDS` folds; (2) locked-OOS expectancy > 0 at **both** 1.5x and 2.0x on the opened holdout; (3) DSR ≥ `DSR_MINIMUM`; (4) PBO ≤ `PBO_MAXIMUM`; (5) CPCV p5 net expectancy > 0; (6) bootstrap p5 mean daily return > 0; (7) max mark-to-market drawdown < `MAX_DRAWDOWN` on the baseline, **every** CPCV path and the compounding baseline, **and** the Monte Carlo halt/loss measurements are defined; (8) ≥ `MIN_OOS_TRADES` OOS trades and ≥ `MIN_REGIMES` regimes represented; (9) a declared capacity model passes (the 8B3 diagnostic is `UNAVAILABLE`, so this gate is `UNAVAILABLE`) | §7.8 |
| P-6 | Gate 5 reads the aggregate OOS expectancy when the CPCV paths are identical, and its reason says so (8C3 note) | A gate must not describe a number as more than it is |
| P-7 | **Research gates** are {1,3,4,5,6,7,8,9}; gate 2 is the **holdout gate**. §8.1 lets only a strategy that passes every research gate open the holdout once, so the decision is three-valued over the nine outcomes: `PAPER_APPROVED` iff all nine `PASS`; `RESEARCH_PASSED` iff all research gates `PASS` and the holdout is `LOCKED` (not yet opened); otherwise `REJECTED`. A `NOT_DEFINED` or `CONTAMINATED` holdout can never reach `RESEARCH_PASSED`: it is `REJECTED` with the reason | The circularity (holdout needs the research gates, the research gates must not need the holdout) resolved by the umbrella's own wording |
| P-8 | Holdout state is **derived from the chain**, never stored: `LEGACY_IMPORTED` ⇒ `CONTAMINATED` (§5.7); the registered protocol's `holdout.state` gives `NOT_DEFINED`/`LOCKED`; a protocol that registers `OPENED`, `CONSUMED` or `CONTAMINATED` is `CONTAMINATED` (an opening before the research gates is an inspection before lock); one sealed bundle whose provenance holdout state is `OPENED` ⇒ `OPENED`; that opening followed by any `GATE_DECIDED` ⇒ `CONSUMED`; **a second opened bundle at an already opened cost level ⇒ `CONTAMINATED`** (one opening is the grid's three bundles, one per cost level), and **a holdout opens at most once across trials: a trial is `CONTAMINATED` when another trial whose registered protocol declares the same holdout (same start, end and dataset hash) has any opened or consumed bundle ("this holdout was opened by trial X")** *(both rules amended 2026-10-02 after the 8D2 review; the original text counted every opened bundle as an opening and scoped the rule to one trial id)* | No new event type; the opening's "time and evidence digest" are the sealed event's own record |
| P-9 | The report is an immutable `ValidationReport` CanonicalModel: trial and attempt references, policy digest, source evidence digests, dataset and holdout state, the WFA/CPCV definitions, the 8C measurements, the cost scenarios and attribution status, drawdown/halt/loss, trade and regime counts, all nine `GateResult`s, the blocking reasons, `report_schema_version`, and its own digest. Stored in the `EvidenceStore`; `VALIDATED.report_sha256` and `GATE_DECIDED.report_sha256` reference it | §8.3 |
| P-10 | `research trial decide` seals the report, then appends `VALIDATED` then `GATE_DECIDED`, in that order; re-running with identical evidence is a no-op (same digest, same event ids). `research trial verify` re-reads every report digest the chain names | The existing verify contract, extended to the second document kind |
| P-11 | **Legacy blocking reasons** are computed from the evidence, not hard-coded to a trial: negative expectancy at baseline costs; no locked holdout; registration state is legacy (not preregistered); dataset-content hash unavailable; return basis is not mark-to-market; cost attribution not complete. Any one forces `REJECTED`. They apply to any trial, so a prospective trial with a null dataset hash is blocked for the same reason | §8.4 "required reasons"; the framework must be able to reject its own motivating strategy, and must not require a Session Momentum special case |
| P-12 | A legacy-imported trial reaches the same `decide` path: its measurements are mostly `UNAVAILABLE` (realized basis, no registered protocol, no mark-to-market), the research gates cannot pass, the blocking reasons list all six, the decision is `REJECTED`, the recorded stage stays `SANDBOX` | §8.4 |

## 4. 8D2 interfaces (specified here so 8D1 leaves the right seams)

- `research trial open-holdout`: refuses unless the derived holdout is `LOCKED` **and** the latest decision is
  `RESEARCH_PASSED`; runs the 1.0x/1.5x/2.0x grid over the locked window through the same `simulate`/`seal_bundle`
  path, sealing bundles whose provenance holdout state is `OPENED`; a second invocation is refused before any
  write. Opened bundles are excluded from the scenario grid and from `splits`/`validate`'s baseline choice (they are
  a different experiment, exactly as compounding bundles are).
- `StrategyPackage` gains `authorization_ref` (human paper authorization). `PAPER` requires it and a
  `PAPER_APPROVED` decision whose report digest the package names; `LIVE` additionally requires the existing
  `signature_sha256` **and** a distinct capital-authorization reference. `package create` never produces `LIVE`
  from a decision alone; an attempt is a typed refusal (§10 "automatic transition to LIVE").
- `package verify` re-checks a package file against the chain: the report digest is named by a `VALIDATED` event,
  the decision is `PAPER_APPROVED`, the stage and references are consistent, nothing is signed here.

## 5. Errors

One new typed error for promotion refusals (`PromotionRefusedError`, exit code mapped in `cli.EXIT_CODES`,
opaque public message, specifics on the private cause) covering: policy digest mismatch, a stage or authorization
rule broken, a holdout opened out of order. Evidence-shaped refusals reuse `ScenarioEvidenceError`/
`StatisticalInputError` as before.

## 6. Testing

Mutation-proven (§11.4): every gate combination gives one deterministic decision (property: the decision is a
pure function of the nine outcomes plus the holdout state); each gate independently fires `PASS`, `FAIL` and
`UNAVAILABLE` with its own case; missing evidence can never become `PASS` (every measurement replaced by
`undefined` in turn); a contaminated holdout cannot reach `RESEARCH_PASSED`; a second opening is `CONTAMINATED`;
a changed threshold changes the policy digest and is refused; the report digest binds the policy, the data and
the results (flip one byte of each, the digest and the verify both move/fail); `PAPER`/`LIVE` rules fire;
rejected legacy evidence stays `SANDBOX` with all six reasons.

## 7. Known limits

- No real holdout exists for any candidate in this repository. Gate 2 is therefore `UNAVAILABLE` for every
  candidate today; the machinery is proven on synthetic locked windows only.
- Gate 9 is `UNAVAILABLE` for every candidate until a protocol can declare a volume-to-lots model.
- The human authorization and the signature are *references*; nothing here can check that a person made them.
- Regime labels are recognised, not vouched (8C).
