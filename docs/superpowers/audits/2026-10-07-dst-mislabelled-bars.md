# Audit: bars mislabelled by single-offset broker-time conversion

**Date:** 2026-10-07 · **Status:** measurement only — no data changed · **Script:** [`scripts/dst_bar_audit.py`](../../../scripts/dst_bar_audit.py)

## Why

FBS-Demo's server clock follows EU daylight-saving rules: UTC+2 in winter, UTC+3 in summer
(Europe/Athens). Until eb34062 ("convert broker time with the binding's declared server
timezone", on `feat/phase-10a-ticks`, not yet on `main`), `brokers/mt5/boundary.py` converted
every historical server timestamp with the single offset measured when the ingest CLI
started. Every bar from the season opposite to the ingest's is therefore labelled an hour off.

This audit counts those bars in `marketdata.bars`. It is step 1 of the correction; the
correction design (a new bar generation, append-only history untouched) and the evidence
addenda for Phases 7 and 9 follow once Phase 10a is merged.

## Method

The offset is measured once per CLI invocation (`gateway.server_utc_offset_seconds`)
immediately before `ingest_runs.started_at` is stamped, so a run's offset is the
Europe/Athens offset at its `started_at`. From that:

- `server_local = event_time + ingest_offset` — recovers the broker's own label exactly;
- `corrected = server_local` read in Europe/Athens — the correct UTC instant;
- a bar is **EARLY** when ingest offset > true offset (its `availability_time` precedes its
  real close: look-ahead), **LATE** when smaller, **correct** when equal.

Every query runs in a `READ ONLY` transaction.

## Result

All 13 runs started on 2026-10-04/05, in EU summer time: **every run converted with +3h**.
No run started within 3h after a DST switch. So every winter bar is stored one hour early,
every summer bar is correct, and **no bar is late**. No physical bar is stored twice (no two
rows share a corrected time), so no run's bars were lost to a conflict caused by the shift.

**250,556 of 865,795 stored bars (28.9%) are labelled one hour early.**

| Instrument | TF | Stored | Early (winter) | Correct | Early bars span (stored `event_time`) |
|---|---|---:|---:|---:|---|
| fx.eurusd | D1 | 14,379 | 6,929 | 7,450 | 1971-01-03 → 2026-03-26 |
| fx.eurusd | H1 | 100,000 | **40,760** | 59,240 | 2010-10-31 → 2026-03-27 |
| fx.eurusd | H4 | 50,387 | 21,608 | 28,779 | 1971-01-03 → 2026-03-27 |
| fx.eurusd | M1 | 99,999 | 0 | 99,999 | — (2026-06-29 → 2026-10-05, summer only) |
| fx.eurusd | M15 | 100,089 | **40,953** | 59,136 | 2022-10-30 → 2026-03-27 |
| fx.eurusd | M5 | 99,999 | 30,952 | 69,047 | 2025-10-26 → 2026-03-27 |
| metal.xauusd | D1 | 7,814 | 3,200 | 4,614 | 1996-03-11 → 2026-03-26 |
| metal.xauusd | H1 | 70,889 | 28,766 | 42,123 | 1996-03-11 → 2026-03-27 |
| metal.xauusd | H4 | 22,242 | 9,069 | 13,173 | 1996-03-11 → 2026-03-27 |
| metal.xauusd | M1 | 99,999 | 0 | 99,999 | — (2026-06-24 → 2026-10-05, summer only) |
| metal.xauusd | M15 | 99,999 | 38,912 | 61,087 | 2022-10-30 → 2026-03-27 |
| metal.xauusd | M5 | 99,999 | 29,407 | 70,592 | 2025-10-26 → 2026-03-27 |

Datasets behind recorded evidence:

- **Phase 9** (`phase9-vol-breakout-eurusd-h1`, EURUSD H1, 2010-08-25 → 2026-10-05):
  40,760 of 100,000 bars early.
- **Phase 7** (Session Momentum, EURUSD M15): 40,953 of 100,089 early. Two runs: the
  99,999-bar backfill of 2026-10-04 21:18 UTC and a 90-bar update of 2026-10-05 19:47 UTC
  (all summer).

### Runs

| Instrument | TF | Outcome | started_at (UTC) | Offset | Stored | Conflicting |
|---|---|---|---|---|---:|---:|
| fx.eurusd | D1 | TRUNCATED | 2026-10-05 19:48:13 | +3h | 14,379 | 0 |
| fx.eurusd | H1 | TRUNCATED | 2026-10-05 19:47:15 | +3h | 100,000 | 0 |
| fx.eurusd | H4 | TRUNCATED | 2026-10-05 19:47:54 | +3h | 50,387 | 0 |
| fx.eurusd | M1 | TRUNCATED | 2026-10-05 19:46:02 | +3h | 99,999 | 0 |
| fx.eurusd | M15 | COMPLETE | 2026-10-04 21:18:14 | +3h | 99,999 | 0 |
| fx.eurusd | M15 | COMPLETE | 2026-10-05 19:47:14 | +3h | 90 | 0 |
| fx.eurusd | M5 | TRUNCATED | 2026-10-05 19:46:42 | +3h | 99,999 | 0 |
| metal.xauusd | D1 | TRUNCATED | 2026-10-05 19:50:38 | +3h | 7,814 | 0 |
| metal.xauusd | H1 | TRUNCATED | 2026-10-05 19:50:01 | +3h | 70,889 | 0 |
| metal.xauusd | H4 | TRUNCATED | 2026-10-05 19:50:26 | +3h | 22,242 | 0 |
| metal.xauusd | M1 | TRUNCATED | 2026-10-05 19:48:18 | +3h | 99,999 | 0 |
| metal.xauusd | M15 | TRUNCATED | 2026-10-05 19:49:26 | +3h | 99,999 | 0 |
| metal.xauusd | M5 | TRUNCATED | 2026-10-05 19:48:53 | +3h | 99,999 | 0 |

`bars_stored` equals the number of bars linked to each run.

## Checks against the data

The counts rest on two inferences; both are checked against the bars themselves.

**The inferred ingest offset (+3h) is the one used.** If it is, Friday's last bar opens at
the same server-local time in both seasons. It does: H1 23:00 (673 Fridays) and M15 23:45
(196), an hour earlier in the US-only-DST weeks (and see the 2013–14 quirk below). A wrong
inferred offset would put one season's Fridays at 22:00 or 00:00.

**Europe/Athens is the server zone across the history.** After correction, the week should
close at 17:00 New York. A zone fault shows in one season only; a data quirk shows in both.

EURUSD H1, mode per year and EU season (S = summer, W = winter):

| Years | Friday last bar (server) | Week close (New York) | Week opens (server) |
|---|---|---|---|
| 2010–2012 | 23:00 S and W | 17:00 S and W | Mon 00:00 |
| 2013–2014 | **22:00 S and W** | **16:00 S and W** | Mon 00:00 |
| 2015–2026 | 23:00 S and W | 17:00 S and W | Mon 00:00 (01:00 S 2016–17) |

2013–14 ends an hour early in *both* seasons — a feature of the broker's history for those
years, not a timezone change. EURUSD M15: all 211 Fridays close at exactly 17:00 New York.

XAUUSD H1 closes at 17:00 New York in both seasons from 2018; 16:00 in 2015–16 (both seasons)
and in winter 2017;
and see below for 1996–2014.

## Findings for the correction design

1. **One-directional, recomputable.** Every mislabelled bar is a winter bar shifted −1h.
   Because `server_local` is recoverable exactly, a corrected generation could be derived
   from the stored rows (`event_time + 3h`, read in Europe/Athens) without re-fetching, or
   re-fetched with the zone-based conversion — a choice for the correction spec.
2. **XAUUSD "H1" before 2015 is daily data.** 1996–2014 holds 4,604 rows, exactly one per
   server day, each at 00:00 server time; 2015 is the first year with intraday bars, and 2016
   has only 166 trading days. Timezone correction shifts these rows but cannot make them
   hourly; they are a separate data-quality issue.
3. **Pre-1981 Athens rules are unverified.** Two EURUSD bars (D1 and H4, server-local
   1975-11-26 00:00 and 1980-04-01 00:00) fall on Greece's own historical midnight DST
   switches. Europe/Athens before 1981 is not the modern EU rule, so the zone choice for
   1970s D1/H4 history cannot be confirmed. Neither trial uses it.
4. **The task premise holds for this database**: the opposite season is labelled one hour
   *early*. That is only because every run happened in summer; a winter ingest under the old
   code would have labelled summer bars an hour *late*.

## Reproduce

```bash
set -a && . ./.env && set +a && uv run python scripts/dst_bar_audit.py
```

Requires the compose postgres to be running. Re-run after the correction to confirm the new
generation shows no EARLY or LATE rows.
