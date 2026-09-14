"""Create the append-only position event store (Phase 5, Task 2).

The guard's memory: once a second it checks every position the system opened
still carries the stop the ledger says it carries, and this table is that
ledger. Same append-only discipline as ``0004_intent_events.py`` -- grant and
trigger, not application discipline -- for the same reason: "we never call
UPDATE" is not evidence, a database that refuses the statement is.

``lifecycle`` is deliberately a SUPERSET of ``core/schemas.py:238``'s five
``PositionState.lifecycle`` values. This store also carries daemon-only rows
(``trading_house.execution.loop``'s ``SYSTEM_TICKET`` / ``GUARD_STOPPED`` /
``GUARD_ESCALATED``) that no position state machine needs -- an event log
legitimately records daemon events no position ever passes through. The CHECK
below is widened to name both groups explicitly, in one list, rather than left
unconstrained: an unconstrained TEXT column would let one typo become a
permanent row in a table nothing can ever fix up.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0006_position_events"
down_revision: str | None = "0005_one_submitting_per_intent"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET ROLE trading_house_owner")

    op.execute(
        """
        CREATE TABLE execution.position_events (
            seq BIGSERIAL PRIMARY KEY,
            position_ticket BIGINT NOT NULL,
            lifecycle TEXT NOT NULL,
            event_time TIMESTAMPTZ NOT NULL,
            recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            payload JSONB NOT NULL,
            CONSTRAINT position_lifecycle_known CHECK (lifecycle IN (
                -- core/schemas.py:238's five PositionState.lifecycle values
                'OPEN_PROTECTED', 'BREAKEVEN_ELIGIBLE', 'TRAILING',
                'EXIT_PENDING', 'CLOSED',
                -- daemon-only rows loop.py writes under SYSTEM_TICKET; no
                -- PositionState ever carries these
                'GUARD_STOPPED', 'GUARD_ESCALATED'
            ))
        )
        """
    )
    op.execute(
        "CREATE INDEX position_events_latest ON execution.position_events "
        "(position_ticket, seq DESC)"
    )

    # A sibling of 0004's reject_intent_event_mutation(): that function's
    # RAISE message names execution.intent_events literally, so reusing it
    # here would tell an operator the wrong table was mutated.
    op.execute(
        """
        CREATE FUNCTION execution.reject_position_event_mutation()
        RETURNS TRIGGER LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'execution.position_events is append-only';
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER position_events_no_mutation "
        "BEFORE UPDATE OR DELETE ON execution.position_events "
        "FOR EACH ROW EXECUTE FUNCTION execution.reject_position_event_mutation()"
    )
    op.execute(
        "CREATE TRIGGER position_events_no_truncate "
        "BEFORE TRUNCATE ON execution.position_events "
        "FOR EACH STATEMENT EXECUTE FUNCTION execution.reject_position_event_mutation()"
    )

    op.execute("REVOKE ALL ON TABLE execution.position_events FROM PUBLIC")
    op.execute("GRANT SELECT, INSERT ON TABLE execution.position_events TO trading_house_runtime")
    op.execute(
        "GRANT USAGE, SELECT ON SEQUENCE execution.position_events_seq_seq TO trading_house_runtime"
    )


def downgrade() -> None:
    op.execute("SET ROLE trading_house_owner")
    op.execute("DROP TABLE execution.position_events")
    op.execute("DROP FUNCTION execution.reject_position_event_mutation()")
