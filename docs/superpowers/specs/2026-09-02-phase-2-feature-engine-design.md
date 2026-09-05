# Phase 2 — The Feature Engine

**Status:** approved design
**Date:** 2026-09-02
**Predecessor:** Phase 1.5, market data ingest and storage (`docs/superpowers/specs/2026-09-01-phase-1-5-market-data-design.md`)

## 1. What this phase is for

Phase 1.5 made stored bars trustworthy and point-in-time. Phase 2 turns them
into the two numbers that decide how far away a stop goes and therefore how
large a position is.

**This is not an analytics phase.** §8.2 of the master specification computes
stop distance as the maximum of four terms, and two of them come from here:

```python
d = max(
    k_sigma * atr,                    # volatility term      <- Phase 2
    k_spread * spread_price,          # cost term            <- Phase 2
    structural,                       # thesis invalidation  <- the strategy
    min_stop_distance(c, ...),        # broker floor         <- Phase 1
)
```

That distance feeds `compute_volume` in §8.1. A wrong ATR is a wrong stop and
a wrong lot size on a live order. Phase 2 is a risk component wearing the
costume of an indicator library, and its correctness bar is the execution
path's, not research's.

## 2. Decisions

| | Decision | Why |
|---|---|---|
| **D-1** | Ship **only what the execution path consumes**: ATR, median spread, and the true-range primitive ATR is built from. | Every indicator shipped before a strategy asks for it is one to test, version and keep correct with no caller to justify it. Research adds more through the pure-function contract without touching the engine. |
| **D-2** | **Insufficient history raises a typed error.** Never a number, never `None`. | The caller is position sizing. It cannot trade what it cannot size, so it must not receive something it will treat as a valid ATR. Matches the bar store, which raises rather than returning less than asked. |
| **D-3** | **The engine fetches; indicators stay pure.** `FeatureEngine` holds the store; indicators take a sequence of bars. | A caller cannot obtain a feature without going through a point-in-time read, because only the engine holds the store. The guarantee Phase 1.5 built stays structural rather than conventional. This project has twice found that "held by discipline" fails the moment a second caller appears. |
| **D-4** | **Fixed lookback window**, not "whatever the store holds". | Wilder's ATR is recursive, so its value depends on where the series started. See §3. |
| **D-5** | **Wilder's smoothing**, not an SMA of true range. | The original definition, and the one §8.2's `k_sigma` guidance (1.0–1.5 for scalp) assumes. |
| **D-6** | Values are **`Decimal`**, unquantised. | ATR is a price distance flowing into stop arithmetic. A float here puts float back on the money path Phase 0 removed. Quantisation to tick size belongs where the stop becomes an actual price, in Phase 3. |
| **D-7** | **Spread is returned in points**, not price. | Converting needs `price_increment` from the instrument contract, which lives behind the broker adapter. Returning points keeps `features/` from depending on `brokers/`; Phase 3's sizing already holds the contract. |

## 3. The problem this design exists to solve

Wilder's ATR is a recursive smoothing:

```
ATR_0 = mean(TR_1 .. TR_period)                       -- the seed
ATR_t = (ATR_{t-1} * (period - 1) + TR_t) / period
```

Every value depends on the one before it, and the chain terminates in a seed
computed from wherever the series happened to start. **The same instant yields
different ATRs depending on how much history was fetched.**

Left alone, that makes the engine non-deterministic in the worst way: the ATR
for a given `as_of` would drift as the store accumulated history, so a backtest
re-run months later would size positions differently with no code change and
nothing to point at. It is the same failure class Phase 1.5 rejected when it
refused to put a tunable threshold in the write path.

**The lookback is therefore fixed and explicit.** The engine fetches exactly
`period * WARMUP_MULTIPLE` bars ending at `as_of` and computes over precisely
that window. `WARMUP_MULTIPLE` is **10** — comfortably past where the seed's
influence decays, and a constant rather than a judgement call at each site.

If the store cannot supply the full window, the engine raises
`InsufficientHistoryError` rather than computing from a shorter one (D-2). An
ATR from three bars is noise wearing the costume of volatility, and it would
size a real position.

That error joins the existing `TradingHouseError` family in `core/errors.py`.
It gets no `ExitCode`, because no CLI command surfaces it in this phase —
Phase 3 decides how sizing reports it.

## 4. What ships

```python
# features/indicators/volatility.py  -- pure, no I/O
def true_range(high: Decimal, low: Decimal, previous_close: Decimal) -> Decimal
def wilder_atr(bars: Sequence[Bar], period: int) -> Decimal

# features/indicators/spread.py  -- pure, no I/O
def median_spread_points(bars: Sequence[Bar]) -> Decimal

# features/engine.py
class FeatureEngine:
    def __init__(self, store: BarStore) -> None: ...
    def atr(self, instrument_id: InstrumentId, timeframe: Timeframe, *,
            period: int, as_of: datetime) -> Decimal: ...
    def median_spread_points(self, instrument_id: InstrumentId,
                             timeframe: Timeframe, *, window: int,
                             as_of: datetime) -> Decimal: ...
```

**Median, not latest, for spread.** A single spike would otherwise widen every
stop derived from it. The broker audit already reports median spread for the
same reason.

The return is `Decimal` even though stored spreads are integer points: an
even-sized window's median is the mean of its two middle values, which is not
an integer. `median_spread_points` takes exactly `window` bars ending at
`as_of` and raises `InsufficientHistoryError` if the store holds fewer — the
same fixed-window rule as ATR (D-4), for the same reason.

**`true_range` is exposed** because ATR is built from it anyway and a strategy
may want it directly. Zero extra cost.

**Realised volatility is deliberately not shipped.** Nothing in §8.2 asks for
it and no strategy exists to want it.

## 5. Boundaries

```
features/
  indicators/volatility.py   true_range, wilder_atr     -- pure
  indicators/spread.py       median_spread_points       -- pure
  engine.py                  FeatureEngine
```

`features/` imports `marketdata/` and nothing else from this project — not
`brokers/`, not `risk/`. Features derive from bars; the arrow never reverses.
An acceptance test pins it, as `marketdata/` is pinned against MetaTrader5.

The engine depends on the `BarStore` protocol, not on `PostgresBarStore`, so
tests drive it without a database.

## 6. Testing

**Two tests carry this phase.**

**Determinism against history depth.** Compute ATR at a fixed `as_of` from a
store holding 200 bars, then from one holding 2,000, and assert the same value.
A path-dependent implementation using "whatever exists" fails it. This is
Phase 2's equivalent of Phase 1.5's point-in-time test; everything else is
hygiene beside it.

**ATR against an independently worked example.** The fixture is hand-computed
from Wilder's definition with the arithmetic written out in a comment, so a
reader verifies the number without running the code. This project has found
four separate cases where a test's expected value and the implementation were
authored from the same belief and neither was ever checked against anything
external. `wilder_atr(bars) == wilder_atr(bars)` would pass against anything.

Beyond those: unit tests per pure function; a property that ATR is never
negative and never exceeds the widest true range in its window; a test that
`InsufficientHistoryError` fires at exactly the boundary, one bar short and one
bar sufficient; and an integration test through a real store confirming a
feature at `as_of` does not change when later bars land.

## 7. New invariant

**I-18** — *A feature is computed over a fixed lookback, so the same
instrument, timeframe, period and `as_of` always yield the same value.*
Enforced by the determinism test. It is the reproducibility half of what I-17
does for visibility.

## 8. Deliberately excluded

| Item | Where it lands |
|---|---|
| Every indicator not named in §8.2 — RSI, VWAP, moving averages, imbalance, session ranges | When a strategy asks for one, through the pure-function contract |
| A materialised feature store | Later, if recompute-on-demand is measurably too slow |
| Incremental or streaming computation | The orchestration plane, if the hot loop needs it |
| Any feature over ticks | Phase 1.5 stores none |
| Realised volatility | Not consumed by §8.2 (D-1) |

## 9. Handoff to Phase 3

Phase 3's sizing gets two guarantees: a feature is either correct for its
`as_of` or it raises, never a degraded number; and the same inputs always give
the same output, so a backtest is reproducible.

What it must supply itself: the `structural` invalidation level from the
strategy, the instrument contract for the broker floor, and the conversion of
spread points to price. Phase 2 deliberately does not reach for any of them.
