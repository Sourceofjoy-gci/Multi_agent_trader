# Phase 8B1 — Mark-to-Market Equity and Canonical Daily Returns Design

**Status:** approved design, pending plan
**Date:** 2026-09-27
**Predecessor:** Phase 8A, Canonical Evidence and Trial Ledger
**Umbrella:** `docs/superpowers/specs/2026-09-25-phase-8-validation-design.md` (Phase 8)
**Implements:** umbrella §6.1 and §6.2

Section references are to the **umbrella design** unless prefixed with "this spec".

## 1. What this slice is for

8A built the ledger and the evidence store but produced no
`MARK_TO_MARKET` return series: `ReturnSeriesBasis.MARK_TO_MARKET` is declared at
`research/trial_ledger.py:82-85` with no producer, and `BacktestResult` carries no
equity curve by design. Every bundle sealed so far is therefore
`REALIZED_CLOSED_TRADES`, which §5.5 rules out for promotion-grade validation.

8B1 makes the engine emit a mark-to-market equity observation at every processed
bar close, makes that series a sealed, self-verifying evidence artifact, and
derives the canonical daily UTC return series from it. It is the first slice of
umbrella §4.1 item 2 and exists solely to produce the evidence §6.1–6.2 describe.
It implements no statistics: no walk-forward, no CPCV, no PSR/DSR, no PBO, no
bootstrap, no drawdown gate. Those are 8C.

### 1.1 What this slice explicitly does not do

- No per-trade cost attribution (umbrella §6.3). Spread and slippage stay folded
  into the fill price and discarded, exactly as they are today.
- No cost scenarios or stress reruns (umbrella §6.4). `CostModel.stress_multiplier`
  is untouched.
- No compounding rerun (umbrella §6.5). Sizing stays constant-notional.
- No capacity diagnostics (umbrella §4.1). No volume-to-lots model is invented and
  no unavailable state is added.
- No new ledger event type, no new CLI command group, and no new dependency.
  NumPy remains absent (umbrella D-4: "8A and 8B do not need it").

## 2. Decisions this slice makes

| # | Decision | Why |
|---|---|---|
| B1-1 | The equity series is a sidecar on a new `BacktestOutcome`, not a field on `BacktestResult` | `BacktestResult.digest()` covers the whole model, so any field added there moves every digest — including four pinned constants that name artifacts which exist on no machine but the one that ran them. A sidecar keeps them exactly as they are. |
| B1-2 | The engine keeps discarding a position left open when bars run out; the `firm_equity + net_pnl` reconciliation applies only when the final observation is flat | `engine.py:430-434` refuses to manufacture a trade from a range's edge, and that refusal is right. Forcing flatness would move `net_pnl`, every result digest, and every known-answer number. |
| B1-3 | A non-flat run is recorded, exits 0, and is reported not promotion-grade | It is honest evidence that fails a later gate. Treating it as an error would push operators toward the force-close B1-2 rejects. |
| B1-4 | Marks are valued at the mid bar close, with no exit-side spread or slippage | A mark is a valuation, not a claim about what closing would fetch. Inventing a transaction cost on every bar would corrupt the series and double-count against §6.3's later attribution. |
| B1-5 | Every processed bar is sealed as canonical JSON in the existing `EvidenceStore` | One canonical form, one digest rule, one read path. The alternative — a second compact blob format — adds normalization rules and a read path to save space on a local store that was never the bottleneck. |
| B1-6 | The series is cross-checked against the trades rather than trusted | `result.py:91-99` omits an equity curve because storing derivable state risks disagreement with the trades it came from. The sidecar answers that objection directly: three assertions bind the series to the result. |
| B1-7 | `backtest run` gains the capability; no new command | 8A already provides `start` and `record`. The operator flow stays register → start → run → record. |

## 3. Why a sidecar, and what it costs

`research/backtest/result.py:89-100` documents the omission of an equity curve
and instructs: *"Do not add the field back."* 8B1 does not add the field back; it
addresses the reason.

The stated reason is that under D-4 equity is constant, so the curve is exactly
the running sum of `net_pnl` over `trades`, and storing it "would duplicate state
that can disagree with the trades it was derived from". Both halves of that are
specific to constant-notional execution. §6.1 breaks the first: once an open
position is marked, the curve is no longer derivable from closed trades, because
an unrealized mark is in no trade's `net_pnl`. The second half survives and is
answered in §6 below rather than dismissed.

The digest cost of putting the series inside `BacktestResult` is concrete and
measured. `digest()` is `sha256(model_dump_json())` over the whole model
(`result.py:148-157`), so a new field moves every result digest. Four constants
name artifacts that exist on no machine but the one that produced them, and so
can never be re-derived:

- the three Phase 7 result digests at `tests/acceptance/test_phase8a.py:111-115`,
  cross-checked against `SESSION_MOMENTUM_SPEC.versioning`;
- the same three at `tests/acceptance/test_phase7.py:64-71`;
- the production copy of the same digests in
  `src/trading_house/strategies/impl/session_momentum.py:50-57`;
- `_CANONICAL_BUNDLE_SHA256` at `tests/property/test_trial_evidence.py:198`, a
  bundle digest, which therefore embeds a result.

A sidecar also keeps the three v1 bundles already sealed in the operator's
evidence store readable. `EvidenceBundle` binds itself to `result.digest()`
(`evidence.py:160-164`), types `result` as `BacktestResult` outright, and
`CanonicalModel` sets `extra="forbid"` (`core/values.py:13`), so a v1 result
shape that stopped validating would turn `research trial verify` into a failure
on a chain it had itself sealed and verified.

The cost of the sidecar is one wrapper type and one attribute access per
existing `run()` caller. That is the trade this slice takes.

## 4. Module layout

One new module, `src/trading_house/research/backtest/mark.py`, holding
`EquityObservation`, `EquitySeries`, `BacktestOutcome`, `derive_daily_returns`,
and `MAX_EQUITY_OBSERVATIONS`.

It sits inside `research/backtest/` so it is held to `BACKTEST_ALLOWED`
(`tests/acceptance/test_architecture.py:72-80`) as well as `RESEARCH_ALLOWED`.
That set admits `trading_house.research.backtest` but **not**
`trading_house.research`, so `mark.py` may not import from `research/evidence.py`.

`DailyReturnPoint` is therefore defined in `mark.py` and re-exported from
`research/evidence.py`, which already imports `BacktestResult` from
`research.backtest.result` and so may import from `research.backtest.mark`. The
serialized shape is unchanged — `{"day": ..., "value": ...}` — so canonical bytes,
every bundle digest, and `_CANONICAL_BUNDLE_SHA256` are unaffected by the move.
`mark.py` needs no allowlist widening: the only contract type it touches,
`InstrumentContract`, is in `trading_house.core.instruments`, and
`trading_house.core` is already allowed.

Rejected alternatives: folding the series into `result.py` puts engine-emission
machinery in a module that is currently pure data plus validators, and invites
exactly the drift its docstring warns against; putting it in `research/evidence.py`
is the wrong layer, since the series is a product of the engine and the evidence
store only stores bytes.

## 5. Emission: what "processed bar" means

`engine.py:348-360` is the bar loop. A bar is **processed** when it passed the
defective-bar check at `engine.py:349-350` and was counted at `engine.py:351`.
That is the same set `result.bars_seen` counts, which yields a checkable
invariant: **the number of equity observations equals `result.bars_seen`.**

A processed bar gets a mark even when the engine did nothing else with it —
a cold snapshot, a skipped session, a bar the strategy returned no proposal for,
a bar whose slot was occupied, or a bar the risk engine rejected. A bar the
engine never looked at, because `quality` was not `OK`, gets no mark, because
there is no close to mark it at.

The mark is emitted **after** the open and close handling at `engine.py:353-360`
and before the snapshot at `engine.py:362`. That ordering is what makes the
observation mean "the state at the close of this bar": the bar's own fill and any
intrabar stop or target have already been applied, so `open_positions` is the
count *after* them and `cumulative_realized_pnl` already includes a trade that
closed on this bar. `bar.availability_time` is the bar's close
(`marketdata/models.py:63-66`) and is read directly off the bar.

## 6. Data model

### 6.1 `EquityObservation`

```python
class EquityObservation(CanonicalModel):
    marked_at: datetime
    equity: Decimal
    cumulative_realized_pnl: Decimal
    unrealized_pnl: Decimal
    open_positions: NonNegativeInt
```

`marked_at` is normalized to UTC by the same private `_utc()` helper wrapping
`ensure_utc` that `research/evidence.py:67-72` and
`research/trial_ledger.py` already use, converting `TimestampError` to
`ValueError`. The copy is deliberate and already commented in those modules.

`unrealized_pnl` is valued at the mid close (B1-4), using the same conversion
`_gross_pnl` uses at `engine.py:659-682`:

```text
unrealized_pnl = side * lots * (bar.close - entry_price) * point_value
```

`entry_price` is the entry fill's own price, which already contains the
half-spread and slippage offset (`fills.py:54-55`). `open_positions` is
structurally 0 or 1, because the engine holds a single `_Position | None`
(`engine.py:343`); the field is a count rather than a flag so the field means
what it says if that ever changes.

### 6.2 `EquitySeries`

```python
class EquitySeries(CanonicalModel):
    firm_equity: Decimal
    observations: tuple[EquityObservation, ...]
```

Validated on its own, without reference to any result:

- `observations` is non-empty;
- `marked_at` is strictly increasing across the series — missing, duplicate, or
  non-increasing UTC timestamps fail, per §6.1;
- at **every** point,
  `equity == firm_equity + cumulative_realized_pnl + unrealized_pnl`, which is
  §6.1's identity and is the load-bearing assertion in the whole slice.

`is_flat` is a derived read-only property, not a field:

```python
@property
def is_flat(self) -> bool:
    return self.observations[-1].open_positions == 0
```

A stored boolean would duplicate `open_positions` and could disagree with it.

### 6.3 `BacktestOutcome`

```python
class BacktestOutcome(CanonicalModel):
    result: BacktestResult
    equity: EquitySeries
```

This is where the series is bound to the trades it came from, which is the
objection `result.py:91-99` raises. Three assertions, each a real disagreement
that must fail closed:

1. `len(equity.observations) == result.bars_seen` — the series covers exactly the
   bars the result says it processed;
2. `equity.firm_equity == result.firm_equity` — the series was not marked against
   a different capital base than the result reports;
3. when `equity.is_flat`, the final observation's `cumulative_realized_pnl`
   equals `result.net_pnl`.

Assertion 3 is where B1-2 lands. It is stated as a precondition, not an
assumption: when the engine discarded a position left open when bars ran out,
the series' final cumulative realized P&L legitimately excludes that mark and
`result.net_pnl` excludes it too, and the assertion simply does not apply.
Nothing is forced flat to make it pass. When the final observation *is* flat, the
assertion is what turns §6.1's "the final flat observation must reconcile to
`firm_equity + net_pnl`" into a checked fact, because with `unrealized_pnl == 0`
the per-point identity already reduces it to `equity == firm_equity + net_pnl`.

### 6.4 `derive_daily_returns`

`DailyReturnPoint` moves here from `research/evidence.py:80-86`, unchanged in
shape, so that `mark.py` can return it without importing from `research/`:

```python
class DailyReturnPoint(CanonicalModel):
    day: date
    value: Decimal
```

```python
def derive_daily_returns(
    series: EquitySeries, *, first_day: date, last_day: date
) -> tuple[DailyReturnPoint, ...]:
```

Bundle assembly calls it with `first_day = result.start.date()` and
`last_day = max(result.end.date(), every observation's date)`.

The extension on the end is not optional. `BacktestResult.end` is
`request.end`, and `Backtester._replay_bars` asks the store for one bar more
than that, because `start`/`end` are inclusive bar open times while the store's
range is half-open. The last observation is therefore stamped at
`request.end + duration(timeframe)`, which for an M15 run ending at 23:45 falls
on the *next* UTC day. Stopping at `end.date()` drops that bar's equity change
from the series while `net_pnl` still counts its P&L — a daily series that does
not reconcile with the result printed beside it, and one nothing above it would
notice, because the series is an opaque tuple inside the bundle.
`research/legacy_import.py:110-122` documents the same defect and the same fix
in the realized basis; the two derivations must agree on where a run ends.

The front needs no such extension: the first observation is stamped at or after
`request.start`, so a run whose first bar closes on the following UTC day gets a
leading all-zero day, which is the honest reading of a window that opened on that
date. Extending backwards would attribute P&L to a day the run never touched.

A caller wanting a different range passes different dates; the function is not
hard-wired to the result.

Implements §6.2 exactly, over every UTC calendar date from `first_day` through
`last_day` inclusive:

- end-of-day equity is the final available mark within that UTC day;
- a day with no available mark carries the prior end-of-day equity forward and
  so returns a literal `Decimal(0)` — a calendar-day series has no way to omit a
  day, and `return_series_basis` is what stops a reader mistaking it for
  mark-to-market or for a measured zero;
- the first day's denominator is the fixed initial `firm_equity`;
- later denominators are the preceding UTC day's end equity and must be strictly
  positive — a non-positive denominator raises `EquityEvidenceError` rather than
  receiving a convenient default (§7.1 of the umbrella, applied to the
  denominator rather than a statistic);
- an open position's unrealized P&L stays in the return, because it is inside
  `equity`.

The result is rectangular by construction: no artificial rows between CPCV test
segments, and weekends present rather than missing, which is what makes §7.4's
`sqrt(365)` annualization correct.

The flat-day `Decimal(0)` matches what 8A's `derive_realized_daily_returns`
already emits at `research/legacy_import.py`, so the two bases produce the same
shape and differ only in what the marks contain.

### 6.5 Where the series is sealed

The per-bar series is evidence, and evidence is sealed in the bundle. `EvidenceBundle`
gains one field for it:

```python
mark_to_market: EquitySeries | None = None
```

Two rules, both fail-closed:

- a bundle whose `return_series_basis` is `MARK_TO_MARKET` **must** carry the series;
- a bundle whose basis is anything else **must not** — a `REALIZED_CLOSED_TRADES` bundle
  carrying a mark-to-market series would claim two different returns for one run, and a
  reader would have no way to tell which one a downstream number used.

Three more, and they are the same three `BacktestOutcome` asserts on the same pair
(`research/backtest/mark.py`): the sealed series and the carried result must share one
`firm_equity`; the series must hold exactly one observation per `bars_seen`; and a flat
run's final cumulative realized total must equal `result.net_pnl`. A series that satisfies
every rule about itself can still be another run's, or a truncated one, and the field exists
to be auditable — so the checks live on the bundle too, which is what a later reader is
handed rather than a command's memory. All three reuse `mark.py`'s wording and its
`is_flat` gate, so a disagreement reads the same whether it was caught at construction or at
`EvidenceStore.read`, and a non-flat run is still buildable here: refusing it would hide
the defect rather than name it.

The third is the only one of the three that binds the series to the *trades* rather than
to the result's shape — the first two accept a series from the right capital base with the
right number of bars whose realized total belongs to some other run — so leaving it on
`BacktestOutcome` alone would have left the sealed artifact, the one that is re-read years
later, checking two thirds of what the type that produced it checks.

Sealing every bar is the decision B1-5 makes on purpose — one canonical form, one digest
rule, one read path — and it is a trade, so the price is stated rather than implied. What
follows is what an operator pays to *verify* that evidence later.

These are **measurements, not projections** — but be precise about what was measured. Phase 7's
bar store was deleted (§2.2), so its 99,988-bar run cannot be replayed and these figures were
*not* taken from it. They were taken by driving the production engine and the production
`EvidenceStore` over a synthetic series of that same length, and they are therefore a
**calibration of the unit cost at that scale**, not a reproduction of that run's artifact. The
bar count is the same, and the cost is a function of the bar count, so the calibration is what
carries; the run it is named after is the scale reference, not the source of the numbers.

| quantity | measured |
|---|---|
| sealed bundle size, 99,988 observations | ≈ 12.7 MiB |
| `EvidenceStore.verify`, per document | ≈ 0.84 s |

Both are linear in the **processed bar count** — the window length times the bar
frequency — because that is the number of observations in the document and the number of
bytes and pydantic validations `read` walks. Nothing about them is per-trade or per-day, so
a coarser timeframe is the lever that moves both, exactly as it is for the ceiling in §7.

What multiplies the total rather than its unit cost is the **number of documents**: 8B2
adds three cost scenarios per run and 8B3 adds a compounding rerun, so a candidate's
evidence stops being one document and becomes several, and `research trial verify` pays
`verify` once per document over the whole chain. The cost is therefore linear in
candidates × scenarios × bars, and it is paid at verification time — a long ledger is
verified in full — not at write time.

This is the accepted price of B1-5, not a defect and not a case for reopening the
decision: a compact second format would save the bytes and spend a normalization rule, a
second digest definition, and a second read path, on a local store that was never the
bottleneck. A change should be made when *verifying* stops being cheap at the scale a real
operator reaches, and the measurement above is what tells a later reader whether it has.

The field is defaulted, not required, and that is deliberate. `CanonicalModel` sets
`extra="forbid"`, so a **required** field would make every v1 document already sealed in an
operator's evidence store unreadable, and `research trial verify` would start failing on a
chain it sealed and verified itself. Absent means "this bundle has no mark-to-market series",
and the coupling rule above is what makes that unambiguous rather than a hole. The legacy
importer, which has no marks to seal, leaves it `None`.

**Defaulted is necessary and not sufficient.** `EvidenceStore.read` re-serializes what it
decoded and refuses any document whose bytes are not today's canonical encoding, so a field
that merely defaulted to `None` would write `"mark_to_market":null` into every bundle and
fail that check for every v1 document — the same outcome the required field produces, by a
path nothing in this section predicted. The field is therefore declared with
`Field(default=None, exclude_if=...)`, so an absent series leaves no key and a v1 bundle's
canonical bytes are exactly what they were. `_CANONICAL_BUNDLE_SHA256` and the three legacy
bundle addresses do not move, and that is the observable consequence rather than a matter of
taste: `tests/acceptance/test_phase8b1.py` pins a legacy bundle's address and reads it back
through the production `EvidenceStore`. The price is that a document spelling the key out as
an explicit `null` is not canonical and will not verify; no write path in this repository can
produce one, since every write goes through `canonical_bytes`.

`bundle_schema_version` stays `1`. The extension is additive and backward compatible: a v1
document still parses, still means what it meant, and a document carrying the new field is
identified by its `return_series_basis` rather than by its version. Bumping the literal would
require a parallel v1 read path to keep the legacy bundles verifiable, which buys a version
number that says nothing the basis field does not already say.

Without this field the series is computed and thrown away: `derive_daily_returns` consumes it,
and nothing else carries it out of the command. That is not a smaller design, it is a different
one — the daily series is a *reduction* of the series, and a reduction whose input is not itself
retained cannot be re-derived, re-audited, or checked against a later `BacktestOutcome`.

## 7. Fail-closed behaviour

One new typed error, `EquityEvidenceError`, on the next free exit code,
`ExitCode.EQUITY_EVIDENCE = 18` (`core/errors.py`, mapped in `cli.EXIT_CODES`).
It covers the three refusals of a whole **run**: a run whose observation count
exceeds the ceiling, a requested daily range whose first day is after its last,
and a day whose prior close is not strictly positive to divide by.
`public_message` carries no free text, matching the existing convention, and no
DSN, driver text, or raw exception ever reaches the operator.

It deliberately does **not** cover a series violating its own identity or its
cross-checks against the result. Those are pydantic `ValidationError`s, because
they are defects in a document rather than in a request, and the CLI reports them
as `ConfigurationError` (exit 2). Keeping the two apart matters: a caller reaching
for `EquityEvidenceError` to mean "equity evidence I cannot trust" would otherwise
miss every document-level refusal, and exit 18 would read as a disk limit when
the real fault is a malformed bundle.

`MAX_EQUITY_OBSERVATIONS = 2_000_000` is a module constant in `mark.py`, not a
setting: a value that exists only to catch a mistake should not be something an
operator can tune away. The measured four-year M15 Phase 7 run produced 99,988
bars, so the ceiling sits roughly twenty times above a realistic run and exists
to stop an accidental multi-year M1 run from filling the disk.

```python
# ponytail: an O(1) guard on a count the loop already knows. It is here to fail
# with a number instead of filling the disk; raise it only if a real run needs
# more, and prefer a coarser timeframe to a larger budget.
```

Exceeding the ceiling raises `EquityEvidenceError` naming the count and the
limit, with the remedy in the operator documentation: narrow the window, or use
a coarser timeframe. The series is never silently subsampled — a subsample would
be evidence of something other than the run, which is the one thing this
framework exists to prevent.

**Not an error:** a non-flat final observation. `backtest run` exits 0, the
bundle records, and the payload reports `"mark_to_market_flat": false`.

## 8. CLI surface

`backtest run` gains seven options (`cli.py:1160-1265`):

| Option | Meaning |
|---|---|
| `--mark-to-market` | Emit an `EvidenceBundle` carrying the per-bar series and mark-to-market daily returns, instead of the bare result artifact. |
| `--trial-id` | The declared candidate this run belongs to. |
| `--attempt-id` | The started attempt this run belongs to. |
| `--spec-sha256` | The preregistered specification's digest. |
| `--agent-run-id` | The agent run that produced the candidate, recorded in `EvidenceProvenance.agent_run_id`. |
| `--occurred-at` | The run's own timestamp, carried into `EvidenceProvenance.occurred_at`. |
| `--registered-at` | When the operator registered the attempt, carried into `EvidenceProvenance.registered_at`. |

All six identity and provenance options are required together with
`--mark-to-market` and refused without it, so a bundle cannot be produced without
the identity it claims.

`--agent-run-id` and `--registered-at` exist because `EvidenceBundle` requires
`EvidenceProvenance.agent_run_id` and `registered_at` (8A,
`research/evidence.py:116-137`), and neither can be derived without lying.
`backtest run` deliberately holds no ledger connection, so it cannot read the
authoritative `agent_run_id` off the `PREREGISTERED` event the way a
ledger-coupled command could; and `registered_at` is not `occurred_at`, because a
run happens before it is recorded and defaulting one to the other would assert
they were simultaneous. Both are declared provenance on the same footing as
`--spec-sha256` and `--occurred-at`: the operator states them, the bundle carries
them, and the ledger's own `recorded_at` remains the only registration-order
authority. The authoritative `agent_run_id` stays on the `PREREGISTERED` event,
so a later phase can cross-check the declared value rather than trust it.

Together with 8A's `research trial start` this closes the hole the 8A
review found in `effective_specifications`: the identity is still
operator-supplied, but it is now checked against a started, preregistered
attempt rather than merely preserved. The README's existing statement that the
ledger cannot vouch that `spec_sha256` matches a declared candidate stays true
and stays written down.

A missing or extra identity option is a `ConfigurationError` (exit 2), not an
`EquityEvidenceError`: an operator who left a flag off has made a mistake, not
produced evidence that cannot be trusted.

Without `--mark-to-market`, output is byte-identical to today. The emitted
document keeps the exact shape `research/legacy_import.py:180-197` reads —
`{"status": "ok", "result": ..., "digest": ..., "margin_modelled": false}` — so
the Phase 7 artifact contract and the legacy importer are untouched.

`return_series_basis` is set to `MARK_TO_MARKET`, which is what finally gives
that declared enum member a producer. The ledger needs no new event type:
`EvidenceSealedPayload` already carries `evidence_sha256`, and the basis lives
inside the bundle, so `verify` is unchanged. `record` is unchanged too except in
what it reads: it now accepts two document shapes rather than one, for the reason
given below.

The operator flow is 8A's, unchanged in shape:

```powershell
trading-house research trial register --protocol protocol.json
trading-house research trial start --trial-id trial-1 --attempt-id att-1 `
    --spec-sha256 <digest> --started-at 2026-09-27T12:00:00
trading-house backtest run --mark-to-market --trial-id trial-1 --attempt-id att-1 `
    --spec-sha256 <digest> --occurred-at 2026-09-27T12:30:00 --registered-at 2026-09-27T12:00:00 `
    --agent-run-id run-1 ... > run.json
trading-house research trial record --trial-id trial-1 --attempt-id att-1 --evidence run.json
trading-house research trial verify
```

`record` therefore reads two document shapes, and only because `backtest run` is the producer
of one of them: a bare `EvidenceBundle`, or a command payload carrying one under `"bundle"`.
It reads nothing else, and anything else is `ConfigurationError` — the same refusal it already
gives a mistyped path. The alternative, having `backtest run` emit a bare bundle with no status
envelope, would break the convention every command in this CLI follows and would lose the
`mark_to_market_flat` signal the design reports.

`register` is unaffected: it still wants a bare `TrialProtocol`, and the unwrap is scoped to
the command whose sibling produced the wrapper.

## 9. Honesty constraints carried into the documentation

- The per-bar series is a **mid-price valuation**, not a liquidation value. A
  position marked at `bar.close` is worth more than closing it would fetch,
  because `fills.py:54-55` charges half-spread and slippage into the entry fill
  and `fills.py:92-103` charges neither into stop or target exits. Every
  drawdown figure 8C derives from this series is therefore mark-to-market, never
  realizable, and the README says so where the series is described.
- A mark immediately before an exit will not equal that exit's realized P&L. The
  exit is priced from its trigger with exit-side costs the mark does not carry
  (`fills.py:89-104`). The series is a valuation path; the trades are the
  accounting. They are related, and they are not the same claim.
- The Phase 7 imports stay `REALIZED_CLOSED_TRADES`, `CONTAMINATED`, and
  non-promotable forever. Their source bar database was deleted
  (umbrella §2.2), so a mark-to-market series can never be reconstructed for
  them. 8B1 adds a capability; it does not retroactively improve any existing
  evidence.
- The v1 evidence schema is not extended into. A mark-to-market bundle is
  distinguishable from a legacy one by its `return_series_basis` and by the
  presence of the series, and the legacy path is untouched.

### 9.1 Known gap, recorded rather than fixed here

`research trial record` accepts the bundle's declared `spec_sha256` and does
not cross-check it against the sealed `PREREGISTERED` event's candidate. The
digest is therefore carried and preserved but unvouched, in the same way the
8A review established for the counters: the chain keeps the label, it does not
attest to it. Closing this means the record command resolving the candidate
inside the preregistration, which is ledger-side gate work and belongs with the
promotion gates rather than in a slice whose subject is equity evidence.

The declared value is not therefore lost or unverifiable — it is in the sealed
bundle, and the `PREREGISTERED` payload in the same chain carries the candidate
family a reader can compare it against. What is missing is an automatic
refusal when they disagree.


## 10. Testing

**Unit** — per-point identity; strictly increasing timestamps; empty-series
refusal; `is_flat` true and false; the conditional flat reconciliation and its
deliberate absence when not flat; each §6.2 daily rule including the flat-day
`Decimal(0)`, the first-day denominator, the strictly-positive rule, and the
non-positive-denominator `EquityEvidenceError`; a mid-mark whose unrealized P&L
matches the `_gross_pnl` conversion.

**Property** — Hypothesis, over generated Decimal sequences and bar timestamps:
the mark identity holds for every point of any well-formed series; a daily series
is always exactly `(last_day - first_day).days + 1` long; daily values are
finite; a series with strictly increasing timestamps is accepted and one with a
repeated timestamp is refused.

**Known answer** — the existing proof at `tests/unit/research/backtest/test_engine.py:81`
(`== Decimal("3.37")`) is extended to assert the series' own values, not only
`net_pnl`, so a change to the emission point or the mark formula fails a number
rather than a shape.

**Integration** — a real run with `--mark-to-market` produces a bundle whose
`return_series_basis` is `MARK_TO_MARKET`; `research trial record` seals it;
`research trial verify` reports a valid chain and re-reads the 30 MB-scale
document; a run ending with an open position records successfully, exits 0, and
reports `"mark_to_market_flat": false`; a tampered observation is rejected by the
identity validator rather than surfacing as a plausible series.

**Acceptance** — `tests/acceptance/test_phase8b1.py` carries the regression guard
for B1-1 specifically: the four pinned digest constants are still exactly their
literals, and the three sealed v1 legacy bundles still verify. If a future change
mutates `BacktestResult`, that test fails and names itself.

## 11. Definition of done

8B1 is complete when:

- the engine emits one mark per processed bar, and the count equals
  `result.bars_seen`;
- the per-point identity holds at every observation;
- a flat run reconciles its final cumulative realized P&L to `result.net_pnl`;
- a non-flat run records, exits 0, and is reported not promotion-grade;
- a mark-to-market bundle seals through 8A and `verify` re-reads it;
- the daily series is rectangular over the requested range with the §6.2 rules;
- all four pinned digest constants and the three v1 legacy bundles are unchanged;
- `backtest run` without the flag emits a byte-identical artifact;
- no new dependency, no new ledger event type, no change to `BacktestResult`.

## 12. Slices that follow

- **8B2** — umbrella §6.3 versioned per-trade cost attribution and §6.4 cost
  scenarios at 1.0x, 1.5x, and 2.0x, including the swap-credit rule the current
  `CostModel` cannot express because it is piecewise in `sign(swap)`.
- **8B3** — umbrella §6.5 compounding rerun through the real risk engine, and
  the capacity diagnostics with an explicit unavailable state.

Each is a separate spec → plan → implementation cycle, per the umbrella's §1.
