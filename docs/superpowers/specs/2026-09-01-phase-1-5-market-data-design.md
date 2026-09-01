# Phase 1.5 — Market Data Ingest, Quality Gates, and Storage

**Status:** approved design
**Date:** 2026-09-01
**Predecessor:** Phase 1, the read-only MT5 gateway (`docs/superpowers/specs/2026-08-25-phase-1-mt5-gateway-design.md`)

## 1. What this phase is for

Phase 1 proved the system can read a broker without touching it. Phase 1.5
turns those reads into stored market data that a strategy or a backtest can
rely on, and — more importantly — makes it impossible to rely on data that is
not there.

The phase exists because of one failure mode. Asking MetaTrader 5 for six
months of M1 history returns **one bar and no error**. Not an exception, not a
warning: a successful call returning almost nothing. A naive backfill stores
that, reports success, and every backtest afterwards runs on a window it did
not choose and cannot see. Storage and validation are the easy half of this
phase. Making short data loud is the engineering.

## 2. Evidence

Every number below was measured against the live FBS demo terminal on
2026-08-31, not assumed. The design is shaped by it.

### 2.1 History depth is radically timeframe-dependent

| Timeframe | EURUSD | XAUUSD |
|---|---|---|
| M1 | ~3 months | ~3 months |
| M5 | ~16 months | ~17 months |
| M15 | ~4.1 years | ~4.2 years |
| H1 | ~16.3 years | ~11.6 years |
| H4 | ~25 years | ~11.6 years |
| D1 | back to 2000-01-03 (6,934 bars) | back to 2000-01-03 (6,806 bars) |

This single table kills the obvious design. Deriving every timeframe from M1
would yield three months of H1 where the broker holds sixteen years.

### 2.2 Requests are capped by row count, not by date range

50,000 bars in one `copy_rates_from_pos` call succeeds; 100,000 fails with
`(-2, 'Terminal: Invalid params')`. A `copy_rates_range` spanning 2000 to now
fails for M1 through H1 for the same reason, while succeeding for D1 — the
constraint is the size of the result, not the age of the data.

### 2.3 Bars are clean; gaps are constant and mostly normal

Across 100,000 real M1 bars (EURUSD and XAUUSD):

| Check | Violations |
|---|---|
| OHLC ordering | 0 |
| Non-positive price | 0 |
| Negative spread | 0 |
| Timestamp misaligned to timeframe | 0 |
| Zero tick volume | 0 |
| **Discontinuities** | **116 (EURUSD), 36 (XAUUSD)** |

Only about seven of those discontinuities per symbol are weekends. The rest are
quiet minutes with no ticks, and the XAUUSD daily trading break. A gap detector
that treats every missing minute as a defect produces roughly 109 false alarms
in seven weeks and catches nothing real.

## 3. Decisions

| | Decision | Why |
|---|---|---|
| **D-1** | Store **closed bars only, M1 and upward**. No tick storage. | Bars answer swing strategies and honest swing backtests within the PostgreSQL already deployed. Tick storage is ~50M rows/symbol/year and would make this a storage-engineering phase. Scalp backtests will under-model spread; recorded as a known limitation rather than hidden. |
| **D-2** | **Store every bar with its quality verdict; reads return only clean bars unless explicitly asked otherwise.** | Keeps the forensic record of what the broker actually sent, allows re-adjudicating a gate later, and makes silent consumption of bad data impossible by default rather than by discipline. |
| **D-3** | Ingest runs as **CLI commands**, not a daemon. | There is no orchestration plane yet. Commands are deterministic, restartable and testable; the orchestration plane later calls the same functions instead of reimplementing them. |
| **D-4** | **FX and metals only.** Equities are their own phase. | Equities need a session calendar or the gap detector fires nightly, and corporate-action handling or an unadjusted split reads as a −50% bar and poisons any backtest spanning it. MT5 exposes no corporate-action feed. Neither problem is about ingest machinery. |
| **D-5** | Bars table **plus an ingest-run ledger**. | A backfill that asked for a year and received a week must leave a record. Without the ledger, "less data" is indistinguishable from "the market was closed". Same discipline as the audit ledger: the interesting event is the one where nothing happened. |
| **D-6** | **Higher timeframes are fetched natively, never aggregated from M1.** | Forced by §2.1. A side benefit: since we never aggregate, our bars and the broker's cannot disagree, and a class of reconciliation bug does not exist. |
| **D-7** | Gates reject **impossibility, never implausibility**. | A tunable threshold in the write path makes stored data depend on the tuning — a backtest run before and after would differ with no code change. Implausibility is a research judgement made over the full series. |
| **D-8** | **`as_of` is a required argument on every read.** | No default is correct for both live and backtest callers, and guessing wrong means a backtest silently sees the future. |
| **D-9** | Ingest depends on a narrow **`HistoryProvider`** protocol, not on `BrokerAdapter` and not on MT5. | Bars are a data concern, not a trading one. Keeps `BrokerAdapter` at eight methods while keeping the venue-neutral promise true for market data. |

## 4. Data model

Two append-only tables.

```
market_bar
  instrument_id      InstrumentId   -- venue-neutral; the signed binding maps it
  timeframe          Timeframe      -- M1 | M5 | M15 | H1 | H4 | D1
  event_time         timestamptz    -- the bar's OPEN, converted to UTC at the boundary
  availability_time  timestamptz    -- when it became knowable = open + duration
  open, high, low, close  numeric   -- Decimal; floats never reach the domain
  tick_volume        bigint
  spread             integer        -- points, as MT5 reports
  real_volume        bigint
  quality            BarQuality     -- OK | OHLC_INCOHERENT | NON_POSITIVE_PRICE
                                    --    | NEGATIVE_SPREAD | MISALIGNED_TIMESTAMP
  ingest_run_id      uuid -> ingest_run
  PRIMARY KEY (instrument_id, timeframe, event_time)

ingest_run
  run_id             uuid
  instrument_id, timeframe
  requested_from, requested_to    timestamptz
  started_at, finished_at         timestamptz
  earliest_event_time             timestamptz  -- how far back it actually reached
  bars_returned, bars_stored, bars_rejected, bars_conflicting  integer
  expected_bars      integer
  coverage_ratio     numeric
  outcome            IngestOutcome
  detail             text          -- redacted; never a DSN or credential
```

### 4.1 `availability_time` is the look-ahead guard

An M1 bar stamped 09:00 is not knowable until 09:01. Storing only the open time
leaves that leak available to anyone who forgets; storing both, and filtering
every read on availability, makes the correct thing the default. This is what
invariant I-10 actually requires — Phase 1 delivered only its timezone half.

`availability_time = event_time + timeframe_duration`. Bar 0, the forming bar,
is never ingested: `copy_rates_range` bounded by the last completed boundary,
per §3.8 of the master specification.

### 4.2 `coverage_ratio` is how short data becomes loud

`expected_bars` is timeframe-aware and comes from a session model: for FX and
metals the week runs continuously from the Sunday open to the Friday close. The
measured boundaries on FBS are a close near 21:00 UTC on Friday and a reopen
near 21:00 UTC on Sunday (§2.3 observed ~48h weekend gaps), and XAUUSD carries
an additional daily break of about an hour. Those constants live in
`sessions.py`, not scattered through the ingest path.

`coverage_ratio` is `bars_returned / expected_bars`. A run below **0.5** is
recorded `SPARSE` rather than passing as success. The threshold is deliberately
generous: it must catch the one-bar-for-six-months case of §1 without firing on
a thin holiday week. The ratio is always stored, not just the
verdict, so a threshold chosen badly today can be re-adjudicated later without
re-fetching anything.

### 4.3 A conflicting re-fetch is a finding, not an update

Brokers revise history. When a re-fetch disagrees with a stored bar, the first
observation stands, `bars_conflicting` is incremented on the run, and nothing is
silently rewritten. A later phase can set policy with the evidence in hand.

## 5. Quality gates

Four checks, all objective, no thresholds:

1. **OHLC ordering** — `low <= min(open, close) <= max(open, close) <= high`
2. **Positive prices** — every price `> 0`
3. **Non-negative spread**
4. **Timestamp alignment** — `event_time` aligned to its timeframe boundary

Each is a broker or parsing error that cannot be true. §2.3 shows they will
almost never fire on this broker, and that is the point: they are the assertion
that catches the day something upstream breaks, including our own conversion
code. Gate 4 in particular is the tripwire for a wrong server-clock offset.

**No outlier or spike rejection.** A 500-pip minute is either a real flash crash
or a broker error, and nothing available at write time distinguishes them (D-7).

**A gap is not a bar, so it cannot carry a bar's flag.** Absence lives on the
run: `coverage_ratio`, plus a gap report listing discontinuities longer than
four times the timeframe's own duration — long enough that the quiet single
minutes of §2.3 stay quiet, short enough that a missing hour on a Tuesday does
not. Counting is robust where per-gap judgement is
noise.

**"Clean" means: passed all four gates.** That is what reads return by default.

## 6. Ingest and paging

```bash
trading-house data backfill --instrument fx.eurusd --timeframe H1 --from 2015-01-01
trading-house data update
```

**The stored data is the cursor.** Backfill resumes from `min(event_time)` for
the key and walks backwards; update resumes from `max(event_time)` and walks
forward. No cursor table exists to drift out of sync with the bars it describes.

**Pages are sized in bars, not dates.** Each page targets 20,000 bars — a safe
margin under the measured cap of §2.2 — converted to a window per timeframe.
Every call goes through the Phase 1 gateway at `Priority.MARKET_DATA`, the
lowest band, so a multi-hour backfill can never delay a protection call.

**Depth exhaustion is an outcome, not an error.** On an empty page or the
single-bar artifact of §1, ingest retries once after a pause — MT5 downloads
history lazily and a second request genuinely can return more — and then records
the wall in `earliest_event_time`.

| Outcome | Meaning |
|---|---|
| `COMPLETE` | Reached `requested_from` with acceptable coverage |
| `TRUNCATED` | Hit the broker's depth wall before `requested_from` |
| `SPARSE` | Reached `requested_from`, but `coverage_ratio` below threshold |
| `EMPTY` | Nothing returned at all |
| `FAILED` | The run errored |

"H1 reaches 2010, M1 stops in May" becomes a stored fact rather than something
rediscovered by writing a backtest that quietly ran on the wrong window.

`data update` takes no arguments: it iterates every instrument in the signed
venue binding across all six timeframes, since bars are small and §2.1 shows
depth varies enough that each timeframe has to be fetched on its own anyway.
`data backfill` requires an explicit instrument and timeframe, because a
backfill is a deliberate, long-running act.

One `ingest_run` row per invocation per key, aggregating its pages.

## 7. Read contract

```python
def bars(instrument_id, timeframe, *, start, end, as_of,
         include_defective=False) -> Sequence[Bar]
def coverage(instrument_id, timeframe) -> Coverage
```

**`as_of` is required and has no default** (D-8). Only bars with
`availability_time <= as_of` are returned. Live callers pass the clock;
backtests pass their simulated time.

**Asking beyond coverage raises a typed error naming the real window.** It does
not return a short result with a healthy exit code. This is the same judgement
as Phase 1's demo guard: the failure shape that has already cost this project
twice is the successful-looking short answer. A caller who wants whatever exists
calls `coverage()` and clamps, deliberately and visibly.

**Reads return frozen `Bar` models with `Decimal` prices**, clean-only unless
`include_defective=True`. Dataframe conversion, if research wants it, lives in
the research layer — the store stays venue-neutral and tool-neutral, as
`BrokerAdapter` does.

## 8. Boundaries

```
src/trading_house/marketdata/
  models.py    Bar, Timeframe, BarQuality, Coverage, IngestOutcome
  quality.py   the four gates                     — pure
  sessions.py  week model, expected-bar counting  — pure
  paging.py    window planning per timeframe      — pure
  ingest.py    orchestration
  store.py     PostgreSQL reader / writer
```

`marketdata/` contains **no MT5 import**, enforced by acceptance test. It
depends on a one-method `HistoryProvider` protocol (D-9), implemented by
`Mt5BrokerAdapter`.

Phase 1 surfaces widen by exactly one method: `TerminalPort` gains a ranged bar
fetch, going from ten methods to eleven, and `terminal.py` grows from 58 to
roughly 66 statements against its 80-statement cap. A new `Mt5Bar` DTO joins
`boundary.py`. Server epochs are converted to UTC at that boundary using the
established offset, as ticks already are.

New migration: `0003_market_bars.py`.

## 9. Testing

| Layer | Covers |
|---|---|
| Unit | Each gate, each boundary case, timeframe alignment, session arithmetic, page planning |
| Property | `availability_time > event_time` always; page windows tile a range with no overlap and no hole; any bar passing the gates satisfies OHLC ordering |
| Integration | Append-only enforcement, conflict-on-refetch, coverage queries, and **point-in-time reads** |
| Live (`mt5`) | Backfill a small real window; assert the recorded coverage matches what the broker gave |
| Acceptance | No float reaches a stored price; every read path filters on `availability_time`; `marketdata/` imports no MT5 |

**The single most important test in this phase** inserts a bar whose
`availability_time` is after the requested `as_of` and asserts it does not come
back. Everything else is hygiene; that one is the difference between a backtest
that means something and one that does not.

## 10. New invariant

**I-17** — *Every market-data read is filtered by `availability_time` against an
explicit `as_of`. No read path can expose a bar before it was knowable.*
Enforced by acceptance test, as I-5 and I-11 are.

## 11. Deliberately excluded

| Item | Where it lands |
|---|---|
| Tick storage and microstructure | A later phase, if scalp research demands it |
| Equities: session calendars, corporate actions | Their own phase (D-4) |
| Indicators and feature engineering | Phase 2, `features/` |
| The backtest engine | Later; this phase only makes it possible to write an honest one |
| Any ingest daemon or scheduler | The orchestration plane (D-3) |
| Non-MT5 data sources | Later; `HistoryProvider` is the seam they will use |
| Outlier and spike adjudication | Research, over the full series (D-7) |

## 12. Handoff

A consumer of this store gets three guarantees: every bar it returns passed the
four impossibility gates, no bar is visible before it was knowable, and a
request for data that does not exist fails loudly instead of returning less.

What it does not guarantee, and callers must check: that the coverage window is
long enough for their purpose. `coverage()` is not optional reading — M1 holds
about three months, and no amount of gate discipline changes that.
