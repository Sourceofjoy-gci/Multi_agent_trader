"""Create the append-only memory and research trial tables (I-13)."""

from collections.abc import Sequence

from alembic import op

revision: str = "0002_memory_and_trials"
down_revision: str | None = "0001_audit_ledger"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET ROLE trading_house_owner")
    op.execute("CREATE SCHEMA memory AUTHORIZATION trading_house_owner")
    op.execute("CREATE SCHEMA research AUTHORIZATION trading_house_owner")
    op.execute("REVOKE ALL ON SCHEMA memory FROM PUBLIC")
    op.execute("REVOKE ALL ON SCHEMA research FROM PUBLIC")

    op.execute(
        """
        CREATE TABLE memory.observed_facts (
            fact_id TEXT PRIMARY KEY,
            written_by TEXT NOT NULL,
            instrument_id TEXT NOT NULL,
            metric TEXT NOT NULL,
            value DOUBLE PRECISION NOT NULL,
            observed_at TIMESTAMPTZ NOT NULL,
            availability_time TIMESTAMPTZ NOT NULL,
            previous_hash BYTEA NOT NULL,
            entry_hash BYTEA NOT NULL,
            CONSTRAINT facts_written_by_deterministic CHECK (written_by = 'deterministic'),
            CONSTRAINT facts_previous_hash_size CHECK (
                pg_catalog.octet_length(previous_hash) = 32
            ),
            CONSTRAINT facts_entry_hash_size CHECK (
                pg_catalog.octet_length(entry_hash) = 32
            ),
            CONSTRAINT facts_availability_not_before_observation CHECK (
                availability_time >= observed_at
            )
        )
        """
    )

    # Spec 2.4: both memory stores are append-only and hash-chained. previous_hash
    # and entry_hash are added here, matching memory.observed_facts exactly, so the
    # chain has somewhere to live. Populating them (computing entry_hash from the
    # row plus the prior tail hash, as audit.append_event does) is a later phase --
    # this migration only puts the columns and their size CHECKs in place so no
    # further migration is needed once that computation exists.
    op.execute(
        """
        CREATE TABLE memory.agent_beliefs (
            belief_id TEXT PRIMARY KEY,
            agent_run_id TEXT NOT NULL,
            claim TEXT NOT NULL,
            availability_time TIMESTAMPTZ NOT NULL,
            previous_hash BYTEA NOT NULL,
            entry_hash BYTEA NOT NULL,
            CONSTRAINT beliefs_previous_hash_size CHECK (
                pg_catalog.octet_length(previous_hash) = 32
            ),
            CONSTRAINT beliefs_entry_hash_size CHECK (
                pg_catalog.octet_length(entry_hash) = 32
            )
        )
        """
    )

    op.execute(
        """
        CREATE TABLE research.trials (
            trial_id TEXT PRIMARY KEY,
            spec_id TEXT NOT NULL,
            agent_run_id TEXT NOT NULL,
            status TEXT NOT NULL,
            sharpe DOUBLE PRECISION,
            registered_at_sequence BIGINT NOT NULL,
            previous_hash BYTEA NOT NULL,
            entry_hash BYTEA NOT NULL,
            CONSTRAINT trials_registered_at_sequence_positive CHECK (
                registered_at_sequence > 0
            ),
            CONSTRAINT trials_status_known CHECK (
                status IN ('completed', 'abandoned', 'failed')
            ),
            CONSTRAINT trials_previous_hash_size CHECK (
                pg_catalog.octet_length(previous_hash) = 32
            ),
            CONSTRAINT trials_entry_hash_size CHECK (
                pg_catalog.octet_length(entry_hash) = 32
            )
        )
        """
    )

    op.execute(
        """
        CREATE FUNCTION memory.reject_row_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog
        AS $function$
        BEGIN
            RAISE EXCEPTION USING
                ERRCODE = '55000',
                MESSAGE = 'memory and research records are append-only';
        END;
        $function$
        """
    )
    op.execute(
        """
        CREATE TRIGGER reject_row_mutation
        BEFORE UPDATE OR DELETE ON memory.observed_facts
        FOR EACH ROW EXECUTE FUNCTION memory.reject_row_mutation()
        """
    )
    op.execute(
        """
        CREATE TRIGGER reject_row_mutation
        BEFORE UPDATE OR DELETE ON memory.agent_beliefs
        FOR EACH ROW EXECUTE FUNCTION memory.reject_row_mutation()
        """
    )
    op.execute(
        """
        CREATE TRIGGER reject_row_mutation
        BEFORE UPDATE OR DELETE ON research.trials
        FOR EACH ROW EXECUTE FUNCTION memory.reject_row_mutation()
        """
    )

    op.execute(
        "REVOKE ALL ON TABLE memory.observed_facts, memory.agent_beliefs, research.trials "
        "FROM PUBLIC"
    )
    op.execute("REVOKE ALL ON FUNCTION memory.reject_row_mutation() FROM PUBLIC")

    op.execute("GRANT USAGE ON SCHEMA memory, research TO trading_house_runtime")
    op.execute(
        "GRANT SELECT ON TABLE memory.observed_facts, memory.agent_beliefs, research.trials "
        "TO trading_house_runtime"
    )
    # I-13 at the database boundary: the runtime role may read facts but can never
    # INSERT one directly. Only memory.agent_beliefs and research.trials get INSERT.
    op.execute(
        "GRANT INSERT ON TABLE memory.agent_beliefs, research.trials TO trading_house_runtime"
    )


def downgrade() -> None:
    op.execute("SET ROLE trading_house_owner")
    op.execute(
        "REVOKE INSERT ON TABLE memory.agent_beliefs, research.trials FROM trading_house_runtime"
    )
    op.execute(
        "REVOKE SELECT ON TABLE memory.observed_facts, memory.agent_beliefs, research.trials "
        "FROM trading_house_runtime"
    )
    op.execute("REVOKE USAGE ON SCHEMA memory, research FROM trading_house_runtime")
    op.execute("DROP TRIGGER reject_row_mutation ON research.trials")
    op.execute("DROP TRIGGER reject_row_mutation ON memory.agent_beliefs")
    op.execute("DROP TRIGGER reject_row_mutation ON memory.observed_facts")
    op.execute("DROP FUNCTION memory.reject_row_mutation()")
    op.execute("DROP TABLE research.trials")
    op.execute("DROP TABLE memory.agent_beliefs")
    op.execute("DROP TABLE memory.observed_facts")
    op.execute("DROP SCHEMA research")
    op.execute("DROP SCHEMA memory")
