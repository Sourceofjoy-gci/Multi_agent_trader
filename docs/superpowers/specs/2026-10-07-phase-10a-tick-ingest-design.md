# Phase 10a — Tick Ingest and Storage

**Status:** implemented
**Date:** 2026-10-07
**Predecessor:** Phase 9 (`docs/superpowers/specs/2026-10-03-phase-9-volatility-breakout-design.md`)
**Successor:** Phase 10b, the tick-level simulator (separate spec)

Section references are to the **master specification**
(`mt5-multi-agent-trading-house-spec.md`) unless prefixed with "this spec".

## 1. What this phase is for

The `fx_scalp` book exists in the signed constitution and no strategy can use it:
§11.2 says bar-based simulation is insufficient for scalping, and the repository
stores only bars. Phase 10 builds the scalping foundations in two sub-projects —
10a stores bid/ask ticks point-in-time; 10b simulates fills on them. This spec is
10a only. It produces data and a reader; it trades nothing and simulates nothing.

## 2. What the broker supplies (measured 2026-10-06/07, FBS demo)

| Fact | Measured |
|---|---|
| Volume | EURUSD 170k–235k ticks per trading day (~6.4k/hour); XAUUSD ~10.3k/hour |
| Fields | `time`, `bid`, `ask`, `last`, `volume`, `time_msc`, `flags`, `volume_real` |
| Trade prints | `last`, `volume`, `volume_real` are 0 on every tick, both instruments |
| Flags seen | 96, 98, 100, 102, 226, 230 |
| Point size | EURUSD 0.00001 (5 digits); XAUUSD 0.01 (2 digits) |
| Depth | ticks returned for 2024-11-06 and every later date probed; 2024-10-09 returned "Terminal: Call failed"; 2022 and earlier returned nothing |

The broker serves a **rolling** history of roughly two years. A day not collected
before it rolls out is lost for good, so 10a must collect continuously, not once.

## 3. Decisions

| # | Decision |
|---|---|
| D-1 | Instruments: `fx.eurusd` and `metal.xauusd` (both already in the signed venue binding). |
| D-2 | Storage: immutable compressed numpy files, one per instrument per UTC day, recorded in PostgreSQL. Rejected: a PostgreSQL tick table (15–20 GB/year, slow replay decode) and Parquet (a heavy new dependency for what `numpy`, already declared, does). |
| D-3 | Prices are int64 **points** (`price / point_size`), exact like `Decimal` and vectorisable; never floats. |
| D-4 | Only completed UTC days are written; a day file is never rewritten. |
| D-5 | A day's digest covers the raw arrays, not the `.npz` bytes, so it is independent of zip metadata. |
| D-6 | `terminal.py`'s statement cap rises from 80 to 90, once, for the one tick method; `MetaTrader5` stays importable only there. |
| D-7 | Daily collection is an operator-installed Windows scheduled task; the repository ships the script and the instructions, never the installation. |

## 4. The tick model

A tick has four fields:

- `time_ms` — int64 UTC epoch milliseconds, converted from the server-frame
  `time_msc` with the gateway's established offset (`server_time_to_utc`), at ingest.
- `bid`, `ask` — int64 points.
- `flags` — uint16, MT5's tick flags as delivered.

Ingest refuses a day (outcome `FAILED`, reason recorded) when any tick has a non-zero
`last`, `volume` or `volume_real`, a non-positive bid or ask, a price that is not an
exact multiple of `point_size`, or a `time_ms` lower than the previous tick's. Ticks
sharing a millisecond keep the broker's order. Crossed quotes (`ask < bid`) are
counted, kept, and recorded as the day's `crossed_quotes`.

## 5. The file

`<tick_root>/<instrument_id>/<YYYY>/<YYYY-MM-DD>.npz`, written with
`numpy.savez_compressed`, holding exactly four arrays: `time_ms` (int64), `bid`
(int64), `ask` (int64), `flags` (uint16).

- `tick_root` is a new `RuntimeSettings` field, `TRADING_HOUSE_TICK_ROOT`, default
  `.local/ticks` (git-ignored, beside `.local/evidence`).
- Written to a temporary name and renamed into place only after its digest is
  computed; an existing file at the final path is never overwritten.
- **Day digest:** SHA-256 over `b"trading-house/ticks/v1\0"`, then the instrument id,
  the day (`YYYY-MM-DD`), the point size as a canonical decimal string, each separated
  by `\0`, then the four arrays' little-endian bytes in the order above.

## 6. The record — migration 0010

`marketdata.tick_days`, one row per instrument per UTC day:

| Column | Type |
|---|---|
| `instrument_id` | TEXT |
| `day` | DATE |
| `outcome` | TEXT, one of `COMPLETE`, `EMPTY`, `FAILED` |
| `tick_count` | INTEGER ≥ 0 |
| `first_time_ms`, `last_time_ms` | BIGINT, NULL unless `COMPLETE` |
| `crossed_quotes` | INTEGER ≥ 0 |
| `point_size` | NUMERIC |
| `file_sha256` | TEXT, NULL unless `COMPLETE` |
| `detail` | TEXT, NULL unless `FAILED` |
| `fetched_at` | TIMESTAMPTZ |

- A `COMPLETE` or `EMPTY` row is unique per `(instrument_id, day)`. A `FAILED` row
  does not block a later attempt; a later `COMPLETE` or `EMPTY` supersedes it.
- Triggers refuse UPDATE, DELETE and TRUNCATE; the runtime role is granted SELECT
  and INSERT only, as for `marketdata.bars`.
- `COMPLETE` requires `tick_count > 0` and a file digest; `EMPTY` requires
  `tick_count = 0`.

## 7. Ingest

`terminal.py` gains `ticks(server_symbol, start, end)` calling
`copy_ticks_range(..., COPY_TICKS_ALL)` with the window converted to the server frame
(`utc_to_server_time`), as bar history does since `3269e29`. The adapter converts to
UTC and to points; all logic lives outside `terminal.py`.

One request per UTC day `[00:00, 24:00)`. A day is fetched only when its end is at
least one minute in the past by the system clock.

*(amended 2026-10-07 by the Phase 10a plan: 24 hourly requests per UTC day, with
the gateway's request timeout raised to 120 s for tick commands — the default 10 s
cannot cover a day's download.)*

Commands under `data ticks` (all refuse while the market is closed, because the
server offset cannot be established, and print key-sorted JSON):

- `backfill --instrument ID --from DATE` — walks **backwards** from yesterday to
  `DATE`, skipping days with a `COMPLETE` or `EMPTY` row. It stops early at the
  **wall**: five consecutive weekday `EMPTY` days, reported with the earliest
  `COMPLETE` day.
- `update --instrument ID` — fills every completed day after the latest recorded
  `COMPLETE`/`EMPTY` day, through yesterday.
- `coverage` — per instrument: days by outcome, total ticks, earliest and latest
  `COMPLETE` day, and weekday gaps.

A day with zero ticks is `EMPTY` (weekend, holiday, or beyond the wall). A call
error is `FAILED` with the error class in `detail`, never the raw broker message.

## 8. Daily collection

`scripts/collect_ticks.ps1` runs `data ticks update` for both instruments. The
README gives the `schtasks` command for a weekday task at about 00:30 UTC. The user
installs it; nothing in the repository creates or modifies a scheduled task.

## 9. Reading and integrity

`marketdata/ticks.py` holds `TickReader`, the only way research code reads ticks:

`ticks(instrument_id, start_ms, end_ms, *, as_of_ms) -> TickArrays` returns the
ticks with `start_ms <= time_ms < end_ms` and `time_ms < as_of_ms` (I-17 for
ticks), concatenated across days in order. It refuses — never guesses — when a day
in the window has no `COMPLETE`/`EMPTY` row, has only `FAILED` rows, has a missing
file, or has a file whose recomputed digest differs from its row.

**Window digest.** `research dataset digest --ticks` prints SHA-256 over the ordered
`(day, outcome, file_sha256)` rows of a window, for a future scalp protocol's
`dataset_sha256`.

*(amended 2026-10-07 by the Phase 10a plan: `research dataset tick-digest`, a
separate command, instead of overloading `research dataset digest --ticks` —
that command requires `--timeframe`.)*

## 10. Testing

- **Unit:** exact point conversion at both point sizes; server-to-UTC conversion;
  UTC-midnight bucketing; the day digest is unchanged by re-compression; each
  refusal in §4; crossed quotes counted not dropped; wall inference; the cursor
  skips recorded days; each `TickReader` refusal; the `as_of_ms` filter.
- **Integration (testcontainers PostgreSQL):** `tick_days` refuses
  UPDATE/DELETE/TRUNCATE, enforces the uniqueness rule, and the runtime role can only
  SELECT and INSERT.
- **Live (skipped when the market is closed):** one recent hour of EURUSD and XAUUSD
  through the real terminal.
- **Acceptance (`tests/acceptance/test_phase10a.py`):** `MetaTrader5` importable only
  from `terminal.py` with the cap at 90; nothing outside `marketdata/` opens tick
  files; an end-to-end backfill → coverage → reader → window digest on a fake
  terminal.

  *(amended 2026-10-07 by the Phase 10a plan: `test_phase10a.py` asserts the
  `np.load`/`np.save*` file boundary; the terminal statement cap lives in
  `test_architecture.py`, alongside the rest of the terminal's shape checks.)*

## 11. Deliberately excluded

The simulator (10b), any scalp strategy, instruments beyond the two in D-1, order-book
depth (MT5 retail offers none), and installing the scheduled task.

## 12. Handoff to 10b

10b consumes `TickReader` and the window digest. It owns bid/ask fills, latency,
fill probability, spread blowouts and the tick replay loop.
