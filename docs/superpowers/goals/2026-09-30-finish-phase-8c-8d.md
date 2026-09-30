# Goal prompt — finish Phase 8C/8D and close the recorded gaps

## The prompt

/goal Finish the unimplemented remainder of the Phase 8 validation framework so the trading
research system is production-ready: the §6.5 compounding rerun and the capacity diagnostic with
its explicit `UNAVAILABLE` state, all of 8C Statistical Validation (§7.1–§7.8), and all of 8D
Promotion and Operator Workflow (§8.1–§8.4), plus the four known defects already recorded in
`README.md` and `.superpowers/sdd/progress.md`. Before changing anything, read
`docs/superpowers/specs/2026-09-25-phase-8-validation-design.md` end to end — it is the
authority — then `README.md` ("What Phase 8A does not implement" and the 8B2b sections) for the
recorded gaps, and `.superpowers/sdd/progress.md` for what the last four slices learned about
this codebase. Deliver one subproject slice at a time, in the design's own order (8B remainder,
then 8C, then 8D), each with its own design spec, plan, implementation, review loop and commit
range — never two subprojects in one pass. Honour the design's hard rule that statistical code is
never built against an assumed equity schema, and its rule that no threshold is ever fixed after
results exist: 8D's nine gates come from the frozen validation policy, and 8B2b's report must
stay a report. Add `numpy` as the only new dependency (§7.1) and create `research/validation/`
and `research/promotion.py` as the §9 module boundaries require; do not invent a parallel ledger
API, a second evidence store, or a promotion seam that bypasses the sealed bundle — §9 and
`ops/scenarios.py` already own those. Every refusal must fail closed, every gate must emit
`PASS`/`FAIL`/`UNAVAILABLE` with its measured value, threshold, evidence digest and reason, and
missing, stale, undefined or non-finite evidence must never read as a pass. For each slice, prove
the work is load-bearing rather than asserted: break the protected behaviour and watch the
intended test fail, then restore it, and record that output in the slice's report. Add or update
tests for everything you build, and keep `src/` free of `assert` (`typing.cast` is the house
idiom) and every model a `CanonicalModel`. Before each slice is called done, have another
reviewer check spec compliance and code quality against that slice's design, and fix what it
finds. Gate every slice on `uv run pytest` (currently 1878 passed, 4 skipped, coverage 98.33% —
it must not regress, and coverage must stay above the 95% floor), `uv run ruff format --check .`,
`uv run ruff check .`, `uv run mypy`, `uv lock --check`, `git diff --check`, and a clean
`git status --short`; for anything touching the ledger, statistics or promotion, also drive the
real CLI against the live local PostgreSQL with both databases migrated and report the actual
command output, not a description of it. Do not open a PR and do not report the work done until
the ledger file records, for every slice, the commit range, the test counts, the review verdicts,
and the operator flow you drove by hand. Stop and report rather than continue if a design clause
turns out to be unimplementable, if a threshold appears to have been set after seeing results, if
the work needs a secret, permission, or data this repository does not have, if the same failure
repeats three times, or if the scope of one slice grows past its own subproject — and name exactly
what blocks it and what you already completed.
