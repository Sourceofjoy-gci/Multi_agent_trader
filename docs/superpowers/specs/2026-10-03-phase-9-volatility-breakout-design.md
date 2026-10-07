# Phase 9 — Volatility-Breakout Swing on EURUSD H1

**Status:** implemented; evidence recorded 2026-10-06 — rejected, no edge (see README, Phase 9 evidence)
**Date:** 2026-10-03
**Predecessor:** Phase 8, statistical validation and the promotion workflow
(`docs/superpowers/specs/2026-09-25-phase-8-validation-design.md` and its 8a–8e children)

Section references are to the **master specification**
(`mt5-multi-agent-trading-house-spec.md`) unless prefixed with "this spec".

## 1. What this phase is for

Phase 7's Session Momentum lost money after costs in all three exit arms and was
invalidated without tuning. Phase 8 built the machinery that decides honestly
whether a strategy has an edge — trial ledger, DSR, PBO, CPCV, bootstrap, the
one-time holdout, the nine gates and signed packages — but it has never been run
against a candidate that might pass.

This phase supplies the second candidate: §10.1's other recommended v1 strategy,
**volatility-breakout swing on a single liquid symbol**, and the minimum
machinery it needs that Session Momentum did not — Bollinger features, a
strategy-declared feature request, and a backtest scope that is no longer
hard-coded to EURUSD M15.

A negative result completes this phase exactly as it completed Phase 7.

## 2. What the repository cannot supply today

Verified against the source.

**The snapshot cannot express a squeeze.** `FeatureSnapshot`
(`core/snapshot.py`) carries one bar, ATR, median spread and four session
fields. There is no rolling mean, deviation, band or history beyond that.

**The backtest scope is a pair of constants.** `cli.py:179-180` fixes
`_BACKTEST_INSTRUMENT = "fx.eurusd"` and `_BACKTEST_TIMEFRAME = Timeframe.M15`,
used by `backtest run` (`cli.py:1437`, `1472-1473`) and by holdout opening's
coverage check (`cli.py:2632`).

**The exit arms are global.** `_EXIT_POLICIES` (`cli.py:189`) prices the
fixed-target arm at 1.0R for every strategy.

**Only EURUSD M15 is stored.** The Phase 7 backfill stopped at the broker's
history wall: 2022-09-16 to 2026-09-25, 99,988 bars. No H1 bars exist.

**OFF-session bars produce no snapshot.** `BacktestEngine._snapshot`
(`research/backtest/engine.py:602`) returns `None` whenever
`session_open_price` or `bars_since_session_open` raises, which they do for every
bar in `Session.OFF` (21:00–24:00 UTC). No entry and no chandelier trail can
happen on those bars.

## 3. Decisions

| # | Decision |
|---|---|
| D-1 | Instrument and timeframe: **EURUSD H1**. Reuses the audited EURUSD contract and Phase 7 cost assumptions; H1 history is expected to reach further back than M15, giving more regimes (§12's hard rule). |
| D-2 | Entry rule: **Bollinger squeeze**, parameters from Bollinger's own published definition wherever one exists. |
| D-3 | Features reach the strategy as a **typed optional block** on `FeatureSnapshot`, computed only when the strategy declares it. Rejected: a `dict[str, Decimal]` (no type safety on the money path) and a raw bar-history window (moves indicator math out of `features/`, and validating ~200 bars per snapshot is slow). |
| D-4 | The OFF-session skip is **declared as a rule** — no entries 21:00–24:00 UTC — rather than changed in the engine. Changing it would alter Session Momentum's chandelier-arm digest and break the reproducibility of recorded Phase 7 evidence. |
| D-5 | Instrument, timeframe and exit arms move from `cli.py` constants into the **strategy registry**. They remain non-options on the command line, preserving the README's stated reason: no choosing data after seeing outcomes. |
| D-6 | Every parameter in §4 is frozen by this spec. Any change after the first real backtest is a new trial, recorded in the ledger. |

## 4. The strategy

### 4.1 Economic rationale

Volatility clusters: quiet periods are followed by active ones more often than
chance. When EURUSD's realised dispersion contracts to a local minimum, the
expansion that follows tends to resolve in one direction as positioning
unwinds. Requiring the break to agree with the longer trend selects the side
with the larger pool of participants likely to follow. This is a hypothesis to
be falsified, not a claim of edge.

### 4.2 The rule, stated completely

Identity: `vol_breakout_eurusd_h1`, version `1`, book `fx_swing`.

Definitions, all on closed H1 bars of `fx.eurusd`, closes only:

- `middle_t` = simple mean of the 20 closes ending at bar *t*.
- `sigma_t` = **population** standard deviation of those 20 closes.
- `upper_t = middle_t + 2·sigma_t`, `lower_t = middle_t − 2·sigma_t`.
- `bandwidth_t = (upper_t − lower_t) / middle_t`.
- Bar *j* is a **squeeze bar** when `bandwidth_j` equals the minimum bandwidth
  over the 125 bars ending at *j* (ties count as squeeze).
- `bars_since_squeeze_t` = `t − j*` where *j\** is the latest squeeze bar in
  `[t − 10, t]`; `None` when there is none.
- `sma_200_t` = simple mean of the 200 closes ending at *t*.

Entry, evaluated on each closed bar *t* that has a snapshot:

- **Long** when all hold: `bars_since_squeeze_t` is not `None` (so 0 ≤ it ≤ 10);
  `close_t > upper_t`; `close_{t−1} ≤ upper_{t−1}`; `close_t > sma_200_t`.
- **Short** when all hold: `bars_since_squeeze_t` is not `None`;
  `close_t < lower_t`; `close_{t−1} ≥ lower_{t−1}`; `close_t < sma_200_t`.
- **No signal** on bars whose open time is in 21:00–24:00 UTC (a 20:00-bar
  signal still fills at the 21:00 open, at that bar's recorded spread) (D-4).

The previous-close condition requires a fresh cross: a bar that merely
continues outside the band does not enter.

Exit and risk:

- `invalidation_price = middle_t`. The risk engine's stop is unchanged:
  `compute_stop_distance` takes the largest of 2.5 × ATR(14), the spread term,
  the distance to invalidation, and the broker floor.
- Size: the `fx_swing` book's 0.75% of book equity, by the existing sizing path.
- `horizon_seconds = max_holding_seconds = 432_000` (five calendar days). Weekend
  holds are permitted; swap, including Wednesday's triple, is charged by the
  existing cost model.

### 4.3 Exit A/B (§9.2)

Three arms, trial count 3:

| Arm | Parameters |
|---|---|
| `none` | Engine stop and the time stop only |
| `fixed_target` | 2.0R |
| `chandelier` | 3.0 × ATR, `min_step_points = 10` |

2.0R rather than Phase 7's 1.0R because a multi-day hold is the horizon at which
§9.2 expects letting winners run to matter. 3.0 × ATR is the top of §8.2's swing
band.

### 4.4 Declared priors

Consumed only by risk gates, never by sizing (I-3). Stated as priors, not
estimates, as Phase 7's were.

| Field | Value | Basis |
|---|---|---|
| `expected_return_bps` | 20.0 | prior |
| `expected_return_stdev_bps` | 10.0 | prior |
| `expected_cost_bps` | 3.0 | Total, including swap: 1.0 non-swap (Phase 7 cost assumptions) + 2.0 swap; TradeProposal requires swap <= total cost |
| `expected_swap_cost_bps` | 2.0 | ≈3 days long at −7.7 points/day ≈ 0.65 bps/day |
| `win_probability` | 0.45 | prior |

Amended 2026-10-04, before any backtest: `expected_cost_bps` is 3.0, not 1.0,
because `TradeProposal` defines it as total cost including swap
(`core/schemas.py`, `swap_is_included_in_total_cost`).

These must clear the `fx_swing` limits: edge after cost ≥ 2.0 bps and swap ≤ 20%
of expected edge. They do: edge 20 − 3 = 17 bps ≥ 2, and swap 2 / 17 ≈ 11.8% ≤ 20%
(`risk/engine.py:305-355`).

### 4.5 Judgements, labelled as such

The 10-bar post-squeeze window, the 200-bar trend mean (conventional), the
five-day time stop, and the 2.0R target are judgements made before any data was
seen. They are not derived and are not to be "improved" after results.

### 4.6 Unchanged cost assumptions

Commission 0.0 per lot per side; slippage 0.4 points per side; swap long −7.7,
short +2.0 points/day; triple swap Wednesday; stress multiplier 1; defective-bar
tolerance 0.

## 5. Features

- `features/indicators/bollinger.py`: pure functions — simple mean, population
  standard deviation, bandwidth — in `Decimal`. The squeeze scan compares squared
  bandwidth (`16·variance / middle²`) so the 125-bar scan takes no square root;
  the reported `bandwidth` is computed once, for bar *t*.
- `BollingerFeatures` (canonical model): `middle`, `upper`, `lower`, `bandwidth`,
  `previous_close`, `previous_upper`, `previous_lower`, `bars_since_squeeze`
  (`NonNegativeInt | None`), `sma_200`.
- `FeatureEngine.bollinger(instrument_id, timeframe, *, as_of)` reads **one fixed
  window of 200 bars** ending at `as_of` — enough for `sma_200` and for the
  squeeze scan, which needs 20 + 124 + 10 = 154 — through the existing
  point-in-time read. Fewer bars raises `InsufficientHistoryError`, as `atr`
  does. Same instrument, timeframe and `as_of` always give the same block (I-18).

## 6. Snapshot and strategy port

- `FeatureSnapshot.bollinger: BollingerFeatures | None = None`.
- `FeatureBlock` enum with one member, `BOLLINGER`.
- The `Strategy` protocol (`core/exits.py`) gains
  `required_features: frozenset[FeatureBlock]`. `SessionMomentum` declares
  `frozenset()`.
- `BacktestEngine._snapshot` computes the block only when the strategy declares
  it; `InsufficientHistoryError` from it skips the bar, the same rule as ATR
  warm-up.
- **Session Momentum must be byte-identical.** Its snapshots carry
  `bollinger=None`. The plan must first establish whether any digest serialises
  the snapshot; if one does, the field must be excluded when `None` so existing
  digests reproduce.

## 7. Scope from the registry

Each registry entry carries `(spec, factory, instrument_id, timeframe,
exit_arms)`:

| Strategy | Instrument | Timeframe | `fixed_target` | `chandelier` |
|---|---|---|---|---|
| `session_momentum_eurusd` | `fx.eurusd` | M15 | 1.0R | 3.0 ATR, 10 points |
| `vol_breakout_eurusd_h1` | `fx.eurusd` | H1 | 2.0R | 3.0 ATR, 10 points |

`_BACKTEST_INSTRUMENT`, `_BACKTEST_TIMEFRAME` and `_EXIT_POLICIES` are removed;
their four call sites read the registry. Holdout opening resolves the scope from
the registered protocol's strategy. Neither instrument nor timeframe becomes a
CLI option.

## 8. Data

`uv run trading-house data backfill --instrument fx.eurusd --timeframe H1 --from 2010-01-01`
against the demo terminal, stopping at the broker's history wall. This is an
operator step: the terminal must be running and logged in. (The 2026-10-03 full
suite run reported `Terminal: Authorization failed`, which must be fixed first.)
The stored span, bar counts and run id are recorded in the README as Phase 7's
were.

## 9. Research protocol

Order is fixed, so the protocol is locked before any outcome is seen:

1. Code merged with CI green on synthetic fixtures only.
2. H1 backfill (§8).
3. Register the trial and its protocol — window, holdout, cost grid, three arms —
   **before** any real backtest.
4. Run the three arms, then `scenarios`, `validate`, `decide` and, only if
   `RESEARCH_PASSED`, `open-holdout`, through the existing Phase 8 commands.
5. Record the outcome in the README and in the spec's `invalidation` and
   `trail_decision`. A loss completes the phase: no tuning, no rerun under the
   same id.

## 10. Testing

Every test names the change that would break it, and that mutation is made once
to prove it.

- **Indicators:** mean, population deviation and bandwidth against hand-computed
  values; squeeze detection including ties; no squeeze in window gives `None`;
  short history raises `InsufficientHistoryError`.
- **Point-in-time (property):** the block at `as_of` reads no bar whose
  `availability_time > as_of` (I-17).
- **Strategy (table):** long; short; continuation outside the band — no entry;
  trend filter blocks each side; squeeze 11 bars back — no entry; OFF-session
  bar — no snapshot, no entry; invalidation equals `middle`; time stop equals
  432,000 s.
- **Strategy (property):** identical snapshots give identical proposals (§10.2).
- **Regression:** Session Momentum's snapshots carry no block and its existing
  synthetic-fixture result digests are unchanged.
- **Acceptance (`tests/acceptance/test_phase9.py`):** the strategy is registered
  with a complete `StrategySpec`; `backtest run --strategy vol_breakout_eurusd_h1`
  replays a synthetic H1 fixture end to end; the registry's scope for it is
  EURUSD H1 and nothing else.

## 11. Deliberately excluded

- Any other symbol or timeframe for this strategy.
- Parameter sweeps of any kind (§9.2 of Phase 7 still applies).
- Changing the OFF-session engine behaviour (D-4).
- Tick data, scalping, the agent layer, an allocator or a live signal-to-order
  runner. Each is a later phase.
- Fixing the Hypothesis deadline flakes seen in the 2026-10-03 suite run; that is
  a separate task.

## 12. Handoff

If the strategy reaches `PAPER_APPROVED` and a `PAPER` package, the next phase is
the paper-trading runner (§16 phase 7 of the master roadmap). If it is rejected,
the next phase is either a third candidate or the scalping foundations (tick
ingest and a tick-level simulator), chosen by the user.
