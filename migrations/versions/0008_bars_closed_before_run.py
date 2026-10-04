"""Refuse a bar its run could not have fetched.

A run fetches only closed bars, and it records itself after the fetch, so
every bar it stores closed no later than the run's ``finished_at``. On
2026-10-01 a hand-typed superuser ``INSERT`` filed 2,880 synthetic bars, dated
up to six weeks into the future, under a run that had finished on 2026-09-28.
The foreign key only asked whether the run existed, and no grant binds a
superuser, so nothing refused it. A trigger does bind one.

The run's requested window is deliberately not the test. ``copy_rates_range``
receives the UTC window unshifted while the bars it returns are shifted back
by the server offset (``brokers/mt5/terminal.py``), so a real run's bars can
sit up to that offset outside ``[requested_from, requested_to]``. Closing time
against ``finished_at`` holds whatever the offset is.

``finalize_run`` may move ``finished_at`` later, only ever to the present.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0008_bars_closed_before_run"
down_revision: str | None = "0007_trial_ledger_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET ROLE trading_house_owner")
    op.execute(
        """
        CREATE FUNCTION marketdata.reject_bar_after_run()
        RETURNS TRIGGER LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.availability_time > (
                SELECT finished_at FROM marketdata.ingest_runs
                WHERE run_id = NEW.ingest_run_id
            ) THEN
                RAISE EXCEPTION 'bar % closes after its ingest run % finished',
                    NEW.event_time, NEW.ingest_run_id
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER bars_closed_before_run "
        "BEFORE INSERT ON marketdata.bars "
        "FOR EACH ROW EXECUTE FUNCTION marketdata.reject_bar_after_run()"
    )


def downgrade() -> None:
    op.execute("SET ROLE trading_house_owner")
    op.execute("DROP TRIGGER bars_closed_before_run ON marketdata.bars")
    op.execute("DROP FUNCTION marketdata.reject_bar_after_run()")
