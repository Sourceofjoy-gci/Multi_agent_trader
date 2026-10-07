"""Create the append-only record of tick days (Phase 10a).

One row per fetch of one instrument's UTC day. A ``COMPLETE`` or ``EMPTY`` row
settles the day and is unique; ``FAILED`` rows are history, so a later attempt
may settle a day an earlier one could not. The ticks themselves live in files
under ``tick_root``; this table holds each file's digest, so a file changed on
disk is refused by the reader rather than trusted.

Append-only twice over, like the audit ledger: the runtime holds SELECT and
INSERT only, and triggers refuse UPDATE, DELETE and TRUNCATE for every role.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0010_tick_days"
down_revision: str | None = "0009_bars_inside_run_window"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET ROLE trading_house_owner")
    op.execute(
        """
        CREATE TABLE marketdata.tick_days (
            instrument_id TEXT NOT NULL,
            day DATE NOT NULL,
            outcome TEXT NOT NULL CHECK (outcome IN ('COMPLETE', 'EMPTY', 'FAILED')),
            tick_count INTEGER NOT NULL CHECK (tick_count >= 0),
            first_time_ms BIGINT,
            last_time_ms BIGINT,
            crossed_quotes INTEGER NOT NULL CHECK (crossed_quotes >= 0),
            point_size NUMERIC NOT NULL CHECK (point_size > 0),
            file_sha256 TEXT,
            detail TEXT,
            fetched_at TIMESTAMPTZ NOT NULL,
            CONSTRAINT tick_days_complete_shape CHECK (
                outcome <> 'COMPLETE' OR (
                    tick_count > 0 AND file_sha256 ~ '^[0-9a-f]{64}$'
                    AND first_time_ms IS NOT NULL AND last_time_ms IS NOT NULL
                    AND first_time_ms <= last_time_ms AND detail IS NULL
                )
            ),
            CONSTRAINT tick_days_empty_shape CHECK (
                outcome <> 'EMPTY' OR (
                    tick_count = 0 AND crossed_quotes = 0 AND file_sha256 IS NULL
                    AND first_time_ms IS NULL AND last_time_ms IS NULL AND detail IS NULL
                )
            ),
            CONSTRAINT tick_days_failed_shape CHECK (
                outcome <> 'FAILED' OR (
                    detail IS NOT NULL AND detail <> '' AND file_sha256 IS NULL
                    AND tick_count = 0 AND crossed_quotes = 0
                    AND first_time_ms IS NULL AND last_time_ms IS NULL
                )
            )
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX tick_days_one_settled
            ON marketdata.tick_days (instrument_id, day)
            WHERE outcome IN ('COMPLETE', 'EMPTY')
        """
    )
    op.execute("CREATE INDEX tick_days_by_day ON marketdata.tick_days (instrument_id, day)")
    op.execute(
        """
        CREATE FUNCTION marketdata.reject_tick_day_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog
        AS $function$
        BEGIN
            RAISE EXCEPTION USING
                ERRCODE = '55000',
                MESSAGE = 'tick days are append-only';
        END;
        $function$
        """
    )
    op.execute(
        """
        CREATE TRIGGER reject_tick_day_row_mutation
        BEFORE UPDATE OR DELETE ON marketdata.tick_days
        FOR EACH ROW EXECUTE FUNCTION marketdata.reject_tick_day_mutation()
        """
    )
    op.execute(
        """
        CREATE TRIGGER reject_tick_day_truncate
        BEFORE TRUNCATE ON marketdata.tick_days
        FOR EACH STATEMENT EXECUTE FUNCTION marketdata.reject_tick_day_mutation()
        """
    )
    op.execute("REVOKE ALL ON TABLE marketdata.tick_days FROM PUBLIC")
    op.execute("REVOKE ALL ON FUNCTION marketdata.reject_tick_day_mutation() FROM PUBLIC")
    op.execute("GRANT SELECT, INSERT ON marketdata.tick_days TO trading_house_runtime")


def downgrade() -> None:
    op.execute("SET ROLE trading_house_owner")
    op.execute("DROP TABLE marketdata.tick_days")
    op.execute("DROP FUNCTION marketdata.reject_tick_day_mutation()")
