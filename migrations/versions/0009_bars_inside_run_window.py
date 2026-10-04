"""Refuse a bar outside the window its run requested.

0008 bounds a bar by when its run finished; this bounds it by what the run
asked for. A bar that closed before the run finished but sits outside
``[requested_from, requested_to)`` was never part of that run's fetch, so the
run cannot vouch for it.

The window is half-open because ingest plans it that way: pages tile
``[oldest, newest)`` and ``update`` ends at the still-forming bar's open.
``Mt5BrokerAdapter.history`` drops MetaTrader 5's end-inclusive bar to match.

This was unsafe until ``brokers/mt5/terminal.py`` sent windows in the
broker's frame. Before that, ``copy_rates_range`` fetched a window
``offset`` hours earlier than the one the run recorded, and a real run's bars
sat up to that far before ``requested_from``. Code older than that fix must
not ingest against a database at this revision.

A missing run raises nothing here, so the foreign key still reports it.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0009_bars_inside_run_window"
down_revision: str | None = "0008_bars_closed_before_run"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET ROLE trading_house_owner")
    op.execute(
        """
        CREATE FUNCTION marketdata.reject_bar_outside_run_window()
        RETURNS TRIGGER LANGUAGE plpgsql AS $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM marketdata.ingest_runs
                WHERE run_id = NEW.ingest_run_id
                  AND NOT (NEW.event_time >= requested_from
                           AND NEW.event_time < requested_to)
            ) THEN
                RAISE EXCEPTION 'bar % is outside the window its ingest run % requested',
                    NEW.event_time, NEW.ingest_run_id
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER bars_inside_run_window "
        "BEFORE INSERT ON marketdata.bars "
        "FOR EACH ROW EXECUTE FUNCTION marketdata.reject_bar_outside_run_window()"
    )


def downgrade() -> None:
    op.execute("SET ROLE trading_house_owner")
    op.execute("DROP TRIGGER bars_inside_run_window ON marketdata.bars")
    op.execute("DROP FUNCTION marketdata.reject_bar_outside_run_window()")
