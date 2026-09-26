"""Create the append-only trial ledger, in both databases (Phase 8A, Task 3).

The audit ledger already proves that a database can own a hash chain better than
a process can. This is the same discipline for a different chain, and the two
stay apart on purpose: ``trading-house:audit:v1`` and
``trading-house:trial-ledger:v1`` are different preimages, so a digest from one
can never be replayed into the other even though the encoding is the same shape.

It runs in *both* databases, which is the other half of the two-database split:
``trading_house_research`` is a second deployment target for this repository's
schema rather than a schema of its own, so the ledger tables exist in the
application database too. The separation is operational -- each database can be
backed up, migrated or dropped on its own schedule -- and not a read boundary.
See the README for what that means and does not claim.

Three things here are not the obvious ones:

* **The runtime holds no write privilege at all.** Not ``INSERT`` -- none. The
  chain advances only through ``append_trial_ledger_event``, which is
  ``SECURITY DEFINER`` with ``search_path`` pinned to ``pg_catalog`` and computes
  the event hash server-side from the sequence and the previous hash it holds
  under lock. A client that wanted a forged sequence would have to become the
  table owner, and the owner is ``NOLOGIN``.
* **A lost race is told apart from a lost write.** The caller supplies the head
  it chained onto; if that head has moved, the function raises 40001 and the
  caller re-reads and retries the *same* event. Silently appending onto a head
  the caller never saw would let it believe its event sits where it does not.
* **An event id is an identity, not a receipt.** A retried event is recognised
  by id *before* the head is compared, so a retry that raced another append is
  idempotent instead of a spurious conflict.

The genesis event's previous hash is 32 zero bytes, and the CHECK constraints
pin every digest to 32 bytes. Sequence numbers are allowed to skip: a
rolled-back insert burns one, and integrity is the previous-hash link, never
``sequence - 1``. The one place the sequence is load-bearing is that same
genesis check, which constrains ``sequence = 1`` and no other row -- so a chain
whose first row has been renumbered satisfies it vacuously, and the verifier
anchors the first row's sequence at 1 for exactly that reason.

The heads row is a cache, not a record. A tampered ``last_sequence`` or
``last_event_hash`` costs availability and nothing else: every append then
fails its 40001 check, loudly, until the row is rebuilt from the tail of
``trial_ledger_events`` -- which is possible precisely because that table is
append-only and self-contained. Repair is a ``SELECT`` and an ``UPDATE`` on a
row the runtime cannot see; no event is ever at risk from a bad head.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0007_trial_ledger_events"
down_revision: str | None = "0006_position_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("SET ROLE trading_house_owner")

    op.execute(
        """
        CREATE TABLE research.trial_ledger_events (
            sequence BIGSERIAL PRIMARY KEY,
            event_id UUID NOT NULL UNIQUE,
            scope_kind TEXT NOT NULL CHECK (scope_kind IN ('protocol', 'trial', 'attempt')),
            scope_id TEXT NOT NULL,
            trial_id TEXT,
            attempt_id TEXT,
            event_type TEXT NOT NULL CHECK (event_type IN (
                'preregistered', 'legacy_imported', 'execution_started',
                'result_recorded', 'failed', 'evidence_sealed',
                'validated', 'gate_decided'
            )),
            spec_sha256 BYTEA NOT NULL,
            canonical_event BYTEA NOT NULL,
            event_json JSONB NOT NULL,
            payload_sha256 BYTEA NOT NULL,
            occurred_at TIMESTAMPTZ NOT NULL,
            recorded_at TIMESTAMPTZ NOT NULL DEFAULT pg_catalog.clock_timestamp(),
            previous_hash BYTEA NOT NULL,
            event_hash BYTEA NOT NULL,
            legacy_import BOOLEAN NOT NULL DEFAULT FALSE,
            legacy_reason TEXT,
            CONSTRAINT ledger_event_previous_hash_size CHECK (
                pg_catalog.octet_length(previous_hash) = 32
            ),
            CONSTRAINT ledger_event_hash_size CHECK (pg_catalog.octet_length(event_hash) = 32),
            CONSTRAINT ledger_event_spec_hash_size CHECK (
                pg_catalog.octet_length(spec_sha256) = 32
            ),
            CONSTRAINT ledger_event_payload_hash_size CHECK (
                pg_catalog.octet_length(payload_sha256) = 32
            ),
            CONSTRAINT ledger_genesis_previous_hash CHECK (
                sequence <> 1
                OR previous_hash = pg_catalog.decode(pg_catalog.repeat('00', 32), 'hex')
            )
        )
        """
    )
    op.execute(
        "CREATE INDEX ledger_events_trial_idx ON research.trial_ledger_events (trial_id, sequence)"
    )

    # One row, forever. It exists so the chain's tail is a row the function
    # locks rather than an aggregate it recomputes under contention, and so
    # ``last_sequence`` cannot be derived from ``max(sequence)`` by a client
    # that is allowed to guess. The runtime has no privilege on it at all.
    op.execute(
        """
        CREATE TABLE research.trial_ledger_heads (
            singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
            last_sequence BIGINT NOT NULL DEFAULT 0,
            last_event_hash BYTEA NOT NULL DEFAULT pg_catalog.decode(
                pg_catalog.repeat('00', 32), 'hex'
            )
        )
        """
    )
    op.execute("INSERT INTO research.trial_ledger_heads (singleton) VALUES (TRUE)")

    op.execute(
        """
        CREATE FUNCTION research.reject_trial_ledger_mutation()
        RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog
        AS $function$
        BEGIN
            RAISE EXCEPTION USING
                ERRCODE = '55000',
                MESSAGE = 'trial ledger is append-only';
        END;
        $function$
        """
    )
    op.execute(
        """
        CREATE TRIGGER reject_trial_ledger_row_mutation
        BEFORE UPDATE OR DELETE ON research.trial_ledger_events
        FOR EACH ROW EXECUTE FUNCTION research.reject_trial_ledger_mutation()
        """
    )
    op.execute(
        """
        CREATE TRIGGER reject_trial_ledger_truncate
        BEFORE TRUNCATE ON research.trial_ledger_events
        FOR EACH STATEMENT EXECUTE FUNCTION research.reject_trial_ledger_mutation()
        """
    )

    # ``p_payload_sha256`` is a fourth argument rather than something this
    # function derives, because the digest is over the *canonical* payload bytes
    # -- sorted keys, compact separators -- and PostgreSQL's own JSON text form
    # is neither. A digest computed here from ``p_event_json -> 'payload'``
    # would be over different bytes than the ones the client hashed, and the
    # verifier would be comparing two definitions of the same field. The
    # alternative, a canonicaliser in plpgsql, is a second canonicalization
    # rule for the whole chain to disagree with; the argument is not.
    #
    # The consequence, stated rather than left to be discovered: this column is
    # client-supplied and is NOT in the chain preimage below, so a client that
    # supplies the wrong digest still writes a correctly chained row. Nothing
    # else in the chain notices, which is exactly why the verifier re-derives
    # this one column and reports ``payload_digest_mismatch``. It is a
    # convenience index over the payload, and it is only trustworthy because
    # something independent re-checks it.
    op.execute(
        """
        CREATE FUNCTION research.append_trial_ledger_event(
            p_canonical_event BYTEA,
            p_event_json JSONB,
            p_expected_previous_hash BYTEA,
            p_payload_sha256 BYTEA
        )
        RETURNS research.trial_ledger_events
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog
        AS $function$
        DECLARE
            supplied_event_id UUID;
            existing_row research.trial_ledger_events%ROWTYPE;
            head_row research.trial_ledger_heads%ROWTYPE;
            next_sequence BIGINT;
            calculated_hash BYTEA;
            inserted_row research.trial_ledger_events%ROWTYPE;
        BEGIN
            PERFORM pg_catalog.pg_advisory_xact_lock(19573, 1);

            IF p_canonical_event IS NULL OR p_event_json IS NULL
                OR p_expected_previous_hash IS NULL OR p_payload_sha256 IS NULL THEN
                RAISE EXCEPTION USING
                    ERRCODE = '22004',
                    MESSAGE = 'trial ledger event inputs must not be null';
            END IF;

            IF pg_catalog.convert_from(p_canonical_event, 'UTF8')::pg_catalog.jsonb
                IS DISTINCT FROM p_event_json THEN
                RAISE EXCEPTION USING
                    ERRCODE = '22000',
                    MESSAGE = 'canonical event bytes do not match event json';
            END IF;

            supplied_event_id := (p_event_json ->> 'event_id')::pg_catalog.uuid;

            -- Before the head comparison, not after: an event already in the
            -- chain is a successful retry even when the chain has moved on, and
            -- reporting that as a lost race would send the caller looking for a
            -- concurrency bug that is not there.
            SELECT * INTO existing_row
            FROM research.trial_ledger_events
            WHERE event_id = supplied_event_id;

            IF FOUND THEN
                IF existing_row.canonical_event IS DISTINCT FROM p_canonical_event THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '23505',
                        MESSAGE = 'trial ledger event id already exists',
                        CONSTRAINT = 'trial_ledger_events_event_id_key';
                END IF;
                RETURN existing_row;
            END IF;

            SELECT * INTO head_row
            FROM research.trial_ledger_heads
            WHERE singleton
            FOR UPDATE;

            IF head_row.last_event_hash IS DISTINCT FROM p_expected_previous_hash THEN
                RAISE EXCEPTION USING
                    ERRCODE = '40001',
                    MESSAGE = 'trial ledger head moved; re-read and retry';
            END IF;

            next_sequence := head_row.last_sequence + 1::BIGINT;

            calculated_hash := audit_crypto.digest(
                pg_catalog.convert_to('trading-house:trial-ledger:v1', 'UTF8')
                || pg_catalog.int8send(next_sequence)
                || head_row.last_event_hash
                || p_canonical_event,
                'sha256'
            );

            INSERT INTO research.trial_ledger_events (
                sequence,
                event_id,
                scope_kind,
                scope_id,
                trial_id,
                attempt_id,
                event_type,
                spec_sha256,
                canonical_event,
                event_json,
                payload_sha256,
                occurred_at,
                previous_hash,
                event_hash,
                legacy_import,
                legacy_reason
            ) VALUES (
                next_sequence,
                supplied_event_id,
                p_event_json ->> 'scope_kind',
                p_event_json ->> 'scope_id',
                p_event_json ->> 'trial_id',
                p_event_json ->> 'attempt_id',
                p_event_json ->> 'event_type',
                pg_catalog.decode(p_event_json ->> 'spec_sha256', 'hex'),
                p_canonical_event,
                p_event_json,
                p_payload_sha256,
                (p_event_json ->> 'occurred_at')::pg_catalog.timestamptz,
                head_row.last_event_hash,
                calculated_hash,
                COALESCE((p_event_json ->> 'legacy')::pg_catalog.bool, FALSE),
                p_event_json ->> 'legacy_reason'
            )
            RETURNING * INTO inserted_row;

            UPDATE research.trial_ledger_heads
            SET last_sequence = next_sequence, last_event_hash = calculated_hash
            WHERE singleton;

            RETURN inserted_row;
        END;
        $function$
        """
    )

    op.execute("REVOKE ALL ON TABLE research.trial_ledger_events FROM PUBLIC")
    op.execute("REVOKE ALL ON TABLE research.trial_ledger_heads FROM PUBLIC")
    op.execute("REVOKE ALL ON FUNCTION research.reject_trial_ledger_mutation() FROM PUBLIC")
    op.execute(
        "REVOKE ALL ON FUNCTION research.append_trial_ledger_event(BYTEA, JSONB, BYTEA, "
        "BYTEA) FROM PUBLIC"
    )

    # Schema USAGE on ``research`` is 0002's grant, for every table in the
    # schema including these. Re-granting it here would make ``downgrade()``
    # revoke a privilege 0002 owns, and a database cycled down to 0006 would
    # no longer match a fresh 0006.
    op.execute("GRANT SELECT ON TABLE research.trial_ledger_events TO trading_house_runtime")
    op.execute(
        "GRANT EXECUTE ON FUNCTION research.append_trial_ledger_event(BYTEA, JSONB, BYTEA, "
        "BYTEA) TO trading_house_runtime"
    )


def downgrade() -> None:
    op.execute("SET ROLE trading_house_owner")
    op.execute(
        "REVOKE EXECUTE ON FUNCTION research.append_trial_ledger_event(BYTEA, JSONB, BYTEA, "
        "BYTEA) FROM trading_house_runtime"
    )
    op.execute("REVOKE SELECT ON TABLE research.trial_ledger_events FROM trading_house_runtime")
    op.execute("DROP TRIGGER reject_trial_ledger_truncate ON research.trial_ledger_events")
    op.execute("DROP TRIGGER reject_trial_ledger_row_mutation ON research.trial_ledger_events")
    op.execute("DROP FUNCTION research.reject_trial_ledger_mutation()")
    op.execute("DROP FUNCTION research.append_trial_ledger_event(BYTEA, JSONB, BYTEA, BYTEA)")
    # ledger_events_trial_idx belongs to the table and goes with it.
    op.execute("DROP TABLE research.trial_ledger_events")
    op.execute("DROP TABLE research.trial_ledger_heads")
