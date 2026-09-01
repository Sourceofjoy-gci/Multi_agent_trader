"""Create the append-only market-data tables (I-17)."""

from collections.abc import Sequence

from alembic import op

revision: str = "0003_market_bars"
down_revision: str | None = "0002_memory_and_trials"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET ROLE trading_house_owner")
    op.execute("CREATE SCHEMA marketdata AUTHORIZATION trading_house_owner")
    op.execute("REVOKE ALL ON SCHEMA marketdata FROM PUBLIC")

    op.execute(
        """
        CREATE TABLE marketdata.ingest_runs (
            run_id UUID PRIMARY KEY,
            instrument_id TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            requested_from TIMESTAMPTZ NOT NULL,
            requested_to TIMESTAMPTZ NOT NULL,
            started_at TIMESTAMPTZ NOT NULL,
            finished_at TIMESTAMPTZ NOT NULL,
            earliest_event_time TIMESTAMPTZ,
            bars_returned INTEGER NOT NULL,
            bars_stored INTEGER NOT NULL,
            bars_rejected INTEGER NOT NULL,
            bars_conflicting INTEGER NOT NULL,
            expected_bars INTEGER NOT NULL,
            coverage_ratio NUMERIC NOT NULL,
            outcome TEXT NOT NULL,
            detail TEXT,
            CONSTRAINT runs_counts_non_negative CHECK (
                bars_returned >= 0 AND bars_stored >= 0
                AND bars_rejected >= 0 AND bars_conflicting >= 0
            ),
            CONSTRAINT runs_range_ordered CHECK (requested_to >= requested_from),
            CONSTRAINT runs_outcome_known CHECK (outcome IN (
                'COMPLETE', 'TRUNCATED', 'SPARSE', 'EMPTY', 'FAILED'
            ))
        )
        """
    )

    op.execute(
        """
        CREATE TABLE marketdata.bars (
            instrument_id TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            event_time TIMESTAMPTZ NOT NULL,
            availability_time TIMESTAMPTZ NOT NULL,
            open NUMERIC NOT NULL,
            high NUMERIC NOT NULL,
            low NUMERIC NOT NULL,
            close NUMERIC NOT NULL,
            tick_volume BIGINT NOT NULL,
            spread INTEGER NOT NULL,
            real_volume BIGINT NOT NULL,
            quality TEXT NOT NULL,
            ingest_run_id UUID NOT NULL REFERENCES marketdata.ingest_runs (run_id),
            PRIMARY KEY (instrument_id, timeframe, event_time),
            CONSTRAINT bars_available_after_close CHECK (availability_time > event_time),
            CONSTRAINT bars_clean_prices_positive CHECK (
                quality <> 'OK'
                OR (open > 0 AND high > 0 AND low > 0 AND close > 0)
            ),
            CONSTRAINT bars_volumes_non_negative CHECK (
                tick_volume >= 0 AND real_volume >= 0
            ),
            CONSTRAINT bars_quality_known CHECK (quality IN (
                'OK', 'OHLC_INCOHERENT', 'NON_POSITIVE_PRICE',
                'NEGATIVE_SPREAD', 'MISALIGNED_TIMESTAMP'
            ))
        )
        """
    )
    # Within one (instrument_id, timeframe) partition, availability_time is
    # event_time plus that timeframe's fixed duration (see
    # trading_house.marketdata.models.availability_of) -- a constant offset,
    # not an independent value. The two orderings coincide, so this index's
    # leaf order already matches event-time order and a reader sorting by
    # event_time within a partition needs no separate sort step.
    op.execute(
        """
        CREATE INDEX bars_point_in_time
            ON marketdata.bars (instrument_id, timeframe, availability_time)
        """
    )

    op.execute("REVOKE ALL ON TABLE marketdata.bars, marketdata.ingest_runs FROM PUBLIC")

    op.execute("GRANT USAGE ON SCHEMA marketdata TO trading_house_runtime")
    op.execute(
        """
        GRANT SELECT, INSERT ON marketdata.bars, marketdata.ingest_runs
            TO trading_house_runtime
        """
    )
    # marketdata.bars is the immutable evidence and stays append-only. A run
    # row, though, records an operation that is still in progress while it is
    # being written: bars_stored and bars_conflicting are unknowable until
    # append_bars has actually landed, and outcome depends on them reaching
    # their final values. Completing that record once the write lands is
    # finishing it, not rewriting history -- so this grant is scoped to
    # exactly the columns a run cannot know at record_run time, leaving what
    # was asked for and what the broker returned (requested_from,
    # requested_to, bars_returned, bars_rejected, expected_bars,
    # coverage_ratio, earliest_event_time) immutable.
    op.execute(
        """
        GRANT UPDATE (bars_stored, bars_conflicting, finished_at, outcome)
            ON marketdata.ingest_runs TO trading_house_runtime
        """
    )


def downgrade() -> None:
    op.execute("SET ROLE trading_house_owner")
    op.execute("DROP SCHEMA marketdata CASCADE")
