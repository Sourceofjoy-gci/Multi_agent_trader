"""Create the append-only halt ledger (Phase 11).

A halt is entered by one row and cleared by a second row naming the same
``halt_id``; what is in force is every ENTERED row with no CLEARED row after
it. Same append-only discipline as ``0004`` and ``0006``, grant and trigger,
and for a sharper reason here: a halt that could be deleted is a kill switch
anyone with INSERT could quietly lift.

``(halt_id, action)`` is unique, so a halt is entered once and cleared once.
That is the database answering a double clear, not a check in Python that two
operators could race past.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0009_control_events"
down_revision: str | None = "0008_bars_closed_before_run"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET ROLE trading_house_owner")
    op.execute(
        """
        CREATE TABLE execution.control_events (
            seq BIGSERIAL PRIMARY KEY,
            halt_id TEXT NOT NULL,
            action TEXT NOT NULL,
            kind TEXT NOT NULL,
            scope TEXT NOT NULL,
            target TEXT,
            reason TEXT NOT NULL,
            actor TEXT NOT NULL,
            event_time TIMESTAMPTZ NOT NULL,
            recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT control_action_known CHECK (action IN ('ENTERED', 'CLEARED')),
            CONSTRAINT control_kind_known CHECK (
                kind IN ('safe_mode', 'kill', 'drawdown_halt')
            ),
            CONSTRAINT control_scope_known CHECK (
                scope IN ('firm', 'book', 'instrument', 'strategy')
            ),
            CONSTRAINT control_firm_has_no_target CHECK ((scope = 'firm') = (target IS NULL)),
            CONSTRAINT control_once_per_action UNIQUE (halt_id, action)
        )
        """
    )
    op.execute(
        """
        CREATE FUNCTION execution.reject_control_event_mutation()
        RETURNS TRIGGER LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'execution.control_events is append-only';
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER control_events_no_mutation "
        "BEFORE UPDATE OR DELETE ON execution.control_events "
        "FOR EACH ROW EXECUTE FUNCTION execution.reject_control_event_mutation()"
    )
    op.execute(
        "CREATE TRIGGER control_events_no_truncate "
        "BEFORE TRUNCATE ON execution.control_events "
        "FOR EACH STATEMENT EXECUTE FUNCTION execution.reject_control_event_mutation()"
    )
    op.execute("REVOKE ALL ON TABLE execution.control_events FROM PUBLIC")
    op.execute("GRANT SELECT, INSERT ON TABLE execution.control_events TO trading_house_runtime")
    op.execute(
        "GRANT USAGE, SELECT ON SEQUENCE execution.control_events_seq_seq TO trading_house_runtime"
    )


def downgrade() -> None:
    op.execute("SET ROLE trading_house_owner")
    op.execute("DROP TABLE execution.control_events")
    op.execute("DROP FUNCTION execution.reject_control_event_mutation()")
