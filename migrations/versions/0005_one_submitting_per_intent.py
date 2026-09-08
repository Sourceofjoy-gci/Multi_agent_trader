"""Make a second SUBMITTING row for one intent impossible (I-6).

The application-level duplicate check in ``OrderManager.submit`` reads the
ledger and then writes to it, which is a race with a second invocation doing
the same thing a millisecond later: both read "no events for this intent",
both insert SUBMITTING, both send. No amount of care in application code
closes that window, because the window is between two statements. The
database is the only place it can be won, so this is a constraint rather
than a check.

Partial rather than plain: an intent legitimately passes through SUBMITTING
once and then accumulates further rows, so uniqueness is only ever asserted
over the SUBMITTING rows themselves.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005_one_submitting_per_intent"
down_revision: str | None = "0004_intent_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET ROLE trading_house_owner")
    op.execute(
        "CREATE UNIQUE INDEX intent_events_one_submitting "
        "ON execution.intent_events (intent_id) "
        "WHERE state = 'SUBMITTING'"
    )


def downgrade() -> None:
    op.execute("SET ROLE trading_house_owner")
    op.execute("DROP INDEX execution.intent_events_one_submitting")
