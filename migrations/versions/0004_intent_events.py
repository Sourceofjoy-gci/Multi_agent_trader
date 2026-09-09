"""Create the append-only intent ledger (I-6, I-20)."""

from collections.abc import Sequence

from alembic import op

revision: str = "0004_intent_events"
down_revision: str | None = "0003_market_bars"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET ROLE trading_house_owner")
    op.execute("CREATE SCHEMA execution AUTHORIZATION trading_house_owner")
    op.execute("REVOKE ALL ON SCHEMA execution FROM PUBLIC")

    op.execute(
        """
        CREATE TABLE execution.intent_events (
            seq BIGSERIAL PRIMARY KEY,
            intent_id TEXT NOT NULL,
            state TEXT NOT NULL,
            event_time TIMESTAMPTZ NOT NULL,
            recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            payload JSONB NOT NULL,
            CONSTRAINT intent_state_known CHECK (state IN (
                'SUBMITTING', 'CONFIRMED', 'UNKNOWN',
                'RECONCILING', 'FAILED', 'REJECTED'
            ))
        )
        """
    )
    op.execute("CREATE INDEX intent_events_latest ON execution.intent_events (intent_id, seq DESC)")

    op.execute(
        """
        CREATE FUNCTION execution.reject_intent_event_mutation()
        RETURNS TRIGGER LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'execution.intent_events is append-only';
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER intent_events_no_mutation "
        "BEFORE UPDATE OR DELETE ON execution.intent_events "
        "FOR EACH ROW EXECUTE FUNCTION execution.reject_intent_event_mutation()"
    )
    op.execute(
        "CREATE TRIGGER intent_events_no_truncate "
        "BEFORE TRUNCATE ON execution.intent_events "
        "FOR EACH STATEMENT EXECUTE FUNCTION execution.reject_intent_event_mutation()"
    )

    op.execute("REVOKE ALL ON TABLE execution.intent_events FROM PUBLIC")
    op.execute("GRANT USAGE ON SCHEMA execution TO trading_house_runtime")
    op.execute("GRANT SELECT, INSERT ON TABLE execution.intent_events TO trading_house_runtime")
    op.execute(
        "GRANT USAGE, SELECT ON SEQUENCE execution.intent_events_seq_seq TO trading_house_runtime"
    )


def downgrade() -> None:
    op.execute("SET ROLE trading_house_owner")
    op.execute("DROP SCHEMA execution CASCADE")
