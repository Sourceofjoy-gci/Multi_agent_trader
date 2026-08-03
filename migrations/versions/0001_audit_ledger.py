"""Create the locked, append-only PostgreSQL audit ledger."""

from collections.abc import Sequence

from alembic import op

revision: str = "0001_audit_ledger"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET ROLE trading_house_owner")
    op.execute("CREATE SCHEMA audit AUTHORIZATION trading_house_owner")
    op.execute("CREATE SCHEMA audit_crypto AUTHORIZATION trading_house_owner")
    op.execute("REVOKE ALL ON SCHEMA audit FROM PUBLIC")
    op.execute("REVOKE ALL ON SCHEMA audit_crypto FROM PUBLIC")
    op.execute("CREATE EXTENSION pgcrypto WITH SCHEMA audit_crypto")
    op.execute("REVOKE ALL ON ALL FUNCTIONS IN SCHEMA audit_crypto FROM PUBLIC")

    op.execute(
        """
        CREATE TABLE audit.ledger (
            sequence_number BIGINT PRIMARY KEY,
            event_id UUID NOT NULL UNIQUE,
            canonical_event BYTEA NOT NULL,
            event_json JSONB NOT NULL,
            previous_hash BYTEA NOT NULL,
            entry_hash BYTEA NOT NULL,
            received_at TIMESTAMPTZ NOT NULL DEFAULT pg_catalog.clock_timestamp(),
            CONSTRAINT ledger_sequence_positive CHECK (sequence_number > 0),
            CONSTRAINT ledger_previous_hash_size CHECK (
                pg_catalog.octet_length(previous_hash) = 32
            ),
            CONSTRAINT ledger_entry_hash_size CHECK (
                pg_catalog.octet_length(entry_hash) = 32
            ),
            CONSTRAINT ledger_genesis_hash CHECK (
                sequence_number <> 1
                OR previous_hash = pg_catalog.decode(pg_catalog.repeat('00', 32), 'hex')
            )
        )
        """
    )

    op.execute(
        """
        CREATE FUNCTION audit.reject_ledger_row_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog
        AS $function$
        BEGIN
            RAISE EXCEPTION USING
                ERRCODE = '55000',
                MESSAGE = 'audit ledger is append-only';
        END;
        $function$
        """
    )
    op.execute(
        """
        CREATE TRIGGER reject_ledger_row_mutation
        BEFORE UPDATE OR DELETE ON audit.ledger
        FOR EACH ROW EXECUTE FUNCTION audit.reject_ledger_row_mutation()
        """
    )
    op.execute(
        """
        CREATE FUNCTION audit.reject_ledger_truncate()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog
        AS $function$
        BEGIN
            RAISE EXCEPTION USING
                ERRCODE = '55000',
                MESSAGE = 'audit ledger is append-only';
        END;
        $function$
        """
    )
    op.execute(
        """
        CREATE TRIGGER reject_ledger_truncate
        BEFORE TRUNCATE ON audit.ledger
        FOR EACH STATEMENT EXECUTE FUNCTION audit.reject_ledger_truncate()
        """
    )

    op.execute(
        """
        CREATE FUNCTION audit.append_event(p_event_bytes BYTEA, p_event_json JSONB)
        RETURNS audit.ledger
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog
        AS $function$
        DECLARE
            next_sequence BIGINT;
            supplied_event_id UUID;
            tail_hash BYTEA;
            calculated_hash BYTEA;
            inserted_row audit.ledger%ROWTYPE;
        BEGIN
            PERFORM pg_catalog.pg_advisory_xact_lock(19572, 1);

            IF p_event_bytes IS NULL OR p_event_json IS NULL THEN
                RAISE EXCEPTION USING
                    ERRCODE = '22004',
                    MESSAGE = 'audit event inputs must not be null';
            END IF;

            IF pg_catalog.convert_from(p_event_bytes, 'UTF8')::pg_catalog.jsonb
                IS DISTINCT FROM p_event_json THEN
                RAISE EXCEPTION USING
                    ERRCODE = '22000',
                    MESSAGE = 'canonical event bytes do not match event json';
            END IF;

            supplied_event_id := (p_event_json ->> 'event_id')::pg_catalog.uuid;

            IF EXISTS (
                SELECT 1 FROM audit.ledger WHERE event_id = supplied_event_id
            ) THEN
                RAISE EXCEPTION USING
                    ERRCODE = '23505',
                    MESSAGE = 'audit event id already exists',
                    CONSTRAINT = 'ledger_event_id_key';
            END IF;

            SELECT COALESCE(pg_catalog.max(sequence_number), 0::BIGINT) + 1::BIGINT
            INTO next_sequence
            FROM audit.ledger;

            IF next_sequence = 1 THEN
                tail_hash := pg_catalog.decode(pg_catalog.repeat('00', 32), 'hex');
            ELSE
                SELECT entry_hash
                INTO tail_hash
                FROM audit.ledger
                WHERE sequence_number = next_sequence - 1;
                IF tail_hash IS NULL THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '55000',
                        MESSAGE = 'audit ledger tail is inconsistent';
                END IF;
            END IF;

            calculated_hash := audit_crypto.digest(
                pg_catalog.convert_to('trading-house:audit:v1', 'UTF8')
                || pg_catalog.int8send(next_sequence)
                || tail_hash
                || p_event_bytes,
                'sha256'
            );

            INSERT INTO audit.ledger (
                sequence_number,
                event_id,
                canonical_event,
                event_json,
                previous_hash,
                entry_hash
            ) VALUES (
                next_sequence,
                supplied_event_id,
                p_event_bytes,
                p_event_json,
                tail_hash,
                calculated_hash
            )
            RETURNING * INTO inserted_row;

            RETURN inserted_row;
        END;
        $function$
        """
    )

    op.execute("REVOKE ALL ON TABLE audit.ledger FROM PUBLIC")
    op.execute("REVOKE ALL ON FUNCTION audit.reject_ledger_row_mutation() FROM PUBLIC")
    op.execute("REVOKE ALL ON FUNCTION audit.reject_ledger_truncate() FROM PUBLIC")
    op.execute("REVOKE ALL ON FUNCTION audit.append_event(BYTEA, JSONB) FROM PUBLIC")
    op.execute(
        "ALTER DEFAULT PRIVILEGES FOR ROLE trading_house_owner "
        "REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC"
    )

    op.execute("GRANT USAGE ON SCHEMA audit TO trading_house_runtime")
    op.execute("GRANT SELECT ON TABLE audit.ledger TO trading_house_runtime")
    op.execute(
        "GRANT EXECUTE ON FUNCTION audit.append_event(BYTEA, JSONB) TO trading_house_runtime"
    )
    op.execute("GRANT SELECT ON TABLE public.alembic_version TO trading_house_runtime")


def downgrade() -> None:
    op.execute("SET ROLE trading_house_owner")
    op.execute("REVOKE SELECT ON TABLE public.alembic_version FROM trading_house_runtime")
    op.execute(
        "REVOKE EXECUTE ON FUNCTION audit.append_event(BYTEA, JSONB) FROM trading_house_runtime"
    )
    op.execute("REVOKE SELECT ON TABLE audit.ledger FROM trading_house_runtime")
    op.execute("REVOKE USAGE ON SCHEMA audit FROM trading_house_runtime")
    op.execute("DROP FUNCTION audit.append_event(BYTEA, JSONB)")
    op.execute("DROP TRIGGER reject_ledger_truncate ON audit.ledger")
    op.execute("DROP TRIGGER reject_ledger_row_mutation ON audit.ledger")
    op.execute("DROP FUNCTION audit.reject_ledger_truncate()")
    op.execute("DROP FUNCTION audit.reject_ledger_row_mutation()")
    op.execute("DROP TABLE audit.ledger")
    op.execute("DROP EXTENSION pgcrypto")
    op.execute("DROP SCHEMA audit_crypto")
    op.execute("DROP SCHEMA audit")
    op.execute(
        "ALTER DEFAULT PRIVILEGES FOR ROLE trading_house_owner GRANT EXECUTE ON FUNCTIONS TO PUBLIC"
    )
