"""Count the stored bars mislabelled by single-offset broker-time conversion.

Read-only: every query runs in a READ ONLY transaction. Changes nothing.

Before eb34062 each ingest run converted broker server time to UTC with the one
offset measured when its CLI invocation started, i.e. the Europe/Athens offset
at ingest_runs.started_at. So the server-local time of every stored bar is
recoverable exactly (event_time + that offset), and its correct UTC instant is
that server-local time read in Europe/Athens. A bar is mislabelled where the
two offsets differ.

Usage:  set -a && . ./.env && set +a && uv run python scripts/dst_bar_audit.py
Findings: docs/superpowers/audits/2026-10-07-dst-mislabelled-bars.md
"""

from __future__ import annotations

import os

import psycopg

# Per-bar derivation shared by the counting queries. Zone: Europe/Athens, the
# server_timezone the signed venue binding declares from eb34062 on.
BASE = """
WITH r AS (
    SELECT run_id, instrument_id, timeframe, started_at, outcome, bars_stored, bars_conflicting,
           (started_at AT TIME ZONE 'Europe/Athens') - (started_at AT TIME ZONE 'UTC')
               AS ingest_off
    FROM marketdata.ingest_runs
), b AS (
    SELECT b.instrument_id, b.timeframe, b.event_time, b.open, b.high, b.low, b.close,
           r.ingest_off, (b.event_time AT TIME ZONE 'UTC') + r.ingest_off AS server_local
    FROM marketdata.bars b JOIN r ON r.run_id = b.ingest_run_id
), c AS (
    SELECT b.*, server_local AT TIME ZONE 'Europe/Athens' AS corrected,
           server_local - ((server_local AT TIME ZONE 'Europe/Athens') AT TIME ZONE 'UTC')
               AS true_off
    FROM b
)
"""

COUNTS = {
    "Runs (ingest offset = Athens offset at started_at)": """
        SELECT instrument_id, timeframe, outcome, started_at, ingest_off, bars_stored,
               bars_conflicting,
               (SELECT count(*) FROM marketdata.bars x WHERE x.ingest_run_id = r.run_id) AS linked
        FROM r ORDER BY instrument_id, timeframe, started_at""",
    "Runs started within 3h after an Athens DST switch (offset could predate it)": """
        SELECT instrument_id, timeframe, started_at FROM r
        WHERE (started_at - interval '3 hours') AT TIME ZONE 'Europe/Athens'
              - ((started_at - interval '3 hours') AT TIME ZONE 'UTC') <> ingest_off""",
    "Bars by ingest offset x true offset": """
        SELECT instrument_id, timeframe, ingest_off, true_off, count(*) AS bars,
               min(event_time) AS first_stored, max(event_time) AS last_stored,
               CASE WHEN ingest_off > true_off THEN 'EARLY (look-ahead)'
                    WHEN ingest_off < true_off THEN 'LATE'
                    ELSE 'correct' END AS label
        FROM c GROUP BY 1, 2, 3, 4 ORDER BY 1, 2, 3, 4""",
    "Totals per instrument/timeframe": """
        SELECT instrument_id, timeframe, count(*) AS bars,
               count(*) FILTER (WHERE ingest_off > true_off) AS early,
               count(*) FILTER (WHERE ingest_off < true_off) AS late,
               count(*) FILTER (WHERE ingest_off = true_off) AS correct
        FROM c GROUP BY 1, 2 ORDER BY 1, 2""",
    "Server-local times ambiguous or nonexistent in Europe/Athens": """
        SELECT instrument_id, timeframe, event_time, server_local, corrected FROM c
        WHERE (corrected AT TIME ZONE 'Europe/Athens') <> server_local
           OR ((corrected - interval '1 hour') AT TIME ZONE 'Europe/Athens') = server_local
        ORDER BY 1, 2, 3""",
    "Physical bars stored twice (same corrected time)": """
        SELECT instrument_id, timeframe, count(*) AS corrected_times_with_dupes,
               count(*) FILTER (WHERE NOT same_ohlc) AS with_differing_ohlc
        FROM (SELECT instrument_id, timeframe, corrected,
                     count(DISTINCT (open, high, low, close)) = 1 AS same_ohlc
              FROM c GROUP BY 1, 2, 3 HAVING count(*) > 1) d
        GROUP BY 1, 2""",
}

# Checks the inferred ingest offset and the zone, season by season. If the
# offset is right, Friday's last bar opens at one server-local hour in both
# seasons; if the zone is right, the corrected week closes at 17:00 New York.
# A timezone fault shows in one season only; a data quirk shows in both.
SEASONS = """
WITH b AS (
    SELECT (event_time AT TIME ZONE 'UTC') + interval '3 hours' AS sl
    FROM marketdata.bars WHERE instrument_id = %(i)s AND timeframe = 'H1'
), c AS (
    SELECT sl, sl AT TIME ZONE 'Europe/Athens' AS corr,
           sl - ((sl AT TIME ZONE 'Europe/Athens') AT TIME ZONE 'UTC') = interval '3 hours'
               AS eu_summer
    FROM b
), wk AS (
    SELECT date_trunc('week', sl) AS w, bool_or(eu_summer) AS summer,
           max(sl) FILTER (WHERE extract(isodow FROM sl) = 5) AS fri_last,
           max(corr) FILTER (WHERE extract(isodow FROM sl) = 5) AS fri_corr,
           min(sl) AS first_bar
    FROM c GROUP BY 1
)
SELECT extract(year FROM w)::int AS yr, CASE WHEN summer THEN 'S' ELSE 'W' END AS eu,
       count(*) AS weeks,
       string_agg(DISTINCT to_char(fri_last, 'HH24'), ',') AS fri_last_server_hours,
       mode() WITHIN GROUP (ORDER BY to_char(fri_last, 'HH24')) AS fri_last_mode,
       mode() WITHIN GROUP (ORDER BY to_char(
           (fri_corr + interval '1 hour') AT TIME ZONE 'America/New_York', 'HH24'))
           AS ny_close_mode,
       mode() WITHIN GROUP (ORDER BY to_char(first_bar, 'Dy HH24')) AS week_open_mode
FROM wk WHERE fri_last IS NOT NULL GROUP BY 1, 2 ORDER BY 1, 2
"""

XAU_DAILY = """
SELECT extract(year FROM event_time)::int AS yr, count(*) AS bars,
       count(DISTINCT ((event_time AT TIME ZONE 'UTC') + interval '3 hours')::date) AS days
FROM marketdata.bars
WHERE instrument_id = 'metal.xauusd' AND timeframe = 'H1' AND event_time < '2017-01-01'
GROUP BY 1 ORDER BY 1
"""


def _print(cur: psycopg.Cursor[tuple[object, ...]], title: str) -> None:
    print(f"\n## {title}")
    assert cur.description is not None  # noqa: S101 -- every query here is a SELECT
    print(" | ".join(d.name for d in cur.description))
    for row in cur.fetchall():
        print(" | ".join(str(v) for v in row))


def main() -> None:
    dsn = os.environ["TRADING_HOUSE_DATABASE_DSN"]
    with psycopg.connect(dsn, options="-c default_transaction_read_only=on") as conn:
        conn.execute("SET TIME ZONE 'UTC'")
        for title, sql in COUNTS.items():
            _print(conn.execute(BASE + sql), title)
        for instrument in ("fx.eurusd", "metal.xauusd"):
            _print(conn.execute(SEASONS, {"i": instrument}), f"H1 week boundaries: {instrument}")
        _print(conn.execute(XAU_DAILY), "metal.xauusd H1 bars per server day before 2017")


if __name__ == "__main__":
    main()
