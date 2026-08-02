# Phase 0 Safety Foundation Design

**Status:** Approved design
**Date:** 2026-08-03
**Source specification:** `mt5-multi-agent-trading-house-spec.md`
**Delivery scope:** Phase 0 only

## Purpose

Build the safety and integrity foundation required by the MT5 Multi-Agent
Trading House specification before any broker, strategy, portfolio, risk-engine,
execution, or LLM-agent behavior is introduced.

Phase 0 establishes five guarantees:

1. Cross-module data is validated against one strict canonical schema set.
2. All persisted and exchanged timestamps are timezone-aware UTC values, with
   `event_time`, `availability_time`, and `processing_time` kept distinct.
3. The risk constitution is verified with Ed25519 before it is parsed and is
   immutable after loading.
4. Every auditable event is appended transactionally to a PostgreSQL SHA-256
   hash chain that the runtime cannot update, delete, or truncate.
5. Startup reports ready only after configuration, database, migration, and
   audit-integrity checks pass.

The repository currently contains documentation only and has no Git history.
Phase 0 therefore also establishes the Python project, dependency lock, local
PostgreSQL environment, migrations, automated tests, CI, and baseline Git
history.

## Scope

### Included

- Python 3.12 `src/`-layout project managed with `uv`.
- Strict, frozen Pydantic v2 schemas from section 4 of the source
  specification.
- UTC clock abstraction and timestamp validators.
- Typed, frozen risk-constitution models covering section 1.4.
- Ed25519 signing and verification of the exact constitution file bytes.
- PostgreSQL 18 migrations and connection management using Psycopg 3.
- Append-only, SHA-256 hash-chained audit ledger.
- Database roles and privileges separating migration ownership from runtime
  append/read authority.
- Health and verification CLI commands.
- Unit, property, integration, and Phase 0 acceptance tests.
- Ruff, strict mypy, pytest, coverage enforcement, and CI on Linux and Windows.
- Docker Compose for local PostgreSQL integration testing.

### Excluded

- The `MetaTrader5` dependency or any MT5 import.
- Broker accounts, terminals, credentials, symbol discovery, or orders.
- Position sizing, risk evaluation, circuit breakers, safe-mode transitions,
  reconciliation, or position guarding.
- Market-data ingestion, features, strategies, backtesting, and promotion.
- LangGraph or any other LLM/agent framework.
- Web APIs, dashboards, alerts, and production deployment.
- Automatic database migration during runtime startup.
- Live, paper, shadow, canary, or simulated trading.

The absence of trading and agent paths is intentional and testable. Later
phases must build on these interfaces rather than bypass them.

## Selected Approach

Use a safety-first modular monolith. The system is one installable Python
package whose modules have narrow public interfaces. PostgreSQL is the only
external runtime dependency in Phase 0.

This approach gives later phases stable contracts and strong authority
boundaries without introducing service authentication, distributed
transactions, network failure modes, or event-projection infrastructure before
they are needed.

Rejected alternatives:

- A full event-store/CQRS foundation adds projections and event-versioning
  complexity without a current consumer.
- Separate configuration, schema, and audit microservices add operational and
  networking risk before the execution plane exists.

## Technology Baseline

- Python 3.12
- `uv` for environment and lock-file management
- Pydantic v2 and pydantic-settings
- Psycopg 3 with binary PostgreSQL support
- Alembic for explicit migrations
- PostgreSQL 18 with `pgcrypto`
- `cryptography` for Ed25519
- PyYAML safe loading after signature verification
- RFC 8785 canonical JSON serialization
- Typer for the operator CLI
- pytest, pytest-cov, and Hypothesis
- Ruff and mypy strict mode
- Docker Compose for local PostgreSQL
- GitHub Actions-compatible CI definitions

Runtime dependency versions are constrained to compatible release series and
fully resolved in `uv.lock`. CI installs from the lock file.

## Repository Structure

The existing workspace is the project root; it will not receive a redundant
`trading-house/` child directory.

```text
.
├── pyproject.toml
├── uv.lock
├── README.md
├── .env.example
├── .gitignore
├── compose.yaml
├── config/
│   ├── risk_constitution.yaml
│   ├── risk_constitution.yaml.sig
│   └── risk_constitution.public.pem
├── migrations/
│   ├── env.py
│   └── versions/
├── src/trading_house/
│   ├── __init__.py
│   ├── cli.py
│   ├── core/
│   │   ├── clock.py
│   │   ├── errors.py
│   │   └── schemas.py
│   ├── constitution/
│   │   ├── loader.py
│   │   ├── models.py
│   │   └── signing.py
│   ├── database/
│   │   ├── connection.py
│   │   └── migrations.py
│   ├── audit/
│   │   ├── canonical.py
│   │   ├── models.py
│   │   ├── repository.py
│   │   └── verification.py
│   └── ops/
│       └── health.py
├── tests/
│   ├── unit/
│   ├── property/
│   ├── integration/
│   └── acceptance/
└── docs/superpowers/
    ├── specs/
    └── plans/
```

Each module has one responsibility. Database-specific logic stays out of the
canonical schemas, and cryptographic signing stays out of the runtime loader.

## Public Interfaces

Later phases depend on these Phase 0 interfaces:

```python
def load_constitution(
    constitution_path: Path,
    signature_path: Path,
    public_key_path: Path,
) -> Constitution: ...

class Clock(Protocol):
    def now(self) -> datetime: ...

class AuditLedger(Protocol):
    def append(self, event: AuditEvent) -> AuditRecord: ...
    def verify(self) -> IntegrityReport: ...
```

The canonical contracts in `trading_house.core.schemas` are the only supported
cross-module message types. Ad-hoc dictionaries may not cross module
boundaries.

## Canonical Schema Rules

All canonical models inherit from a common Pydantic base configured with:

- strict validation for Python inputs;
- unknown fields forbidden;
- immutable instances;
- validation on construction;
- deterministic JSON serialization.

Every datetime must have timezone information. Inputs with a valid non-UTC
offset are converted to UTC at the boundary; naive datetimes are rejected. A
`Stamped` model additionally enforces:

```text
event_time <= availability_time <= processing_time
```

Phase 0 implements and freezes the source specification's `Book`, `Side`,
`Stamped`, `RegimeAssessment`, `TradeProposal`, `RiskDecision`, `OrderIntent`,
`PositionState`, and `AgentOpinion` contracts.

Additional invariants include:

- identifiers and source strings are non-empty and bounded;
- prices, horizons, holding periods, liquidity, volumes, and monetary amounts
  use explicit positive or non-negative constraints;
- probabilities lie in `[0, 1]`, with the proposal's degenerate `0` and `1`
  win probabilities rejected;
- a regime probability distribution sums to `1` within a small numeric
  tolerance;
- `TradeProposal` has no volume field;
- `OrderIntent.sl` is required and strictly positive;
- `PositionState.current_sl` and `initial_risk_distance` are strictly positive;
- model serialization always emits UTC timestamps.

### Risk-decision ambiguity resolution

The source specification shows one `RiskDecision` model even though rejected
decisions cannot always have a computed stop or executable volume. Phase 0
resolves this using a discriminated union:

- `ApprovedRiskDecision`: positive approved volume, stop, risk money, and risk
  percentage;
- `ResizedRiskDecision`: the same executable fields plus a resized verdict;
- `RejectedRiskDecision`: zero approved volume, no executable stop or target,
  zero risk money, and at least one rejection reason.

The exported `RiskDecision` type validates on `verdict`, preventing a rejected
decision from being turned into an order.

## UTC Clock Discipline

`SystemClock.now()` returns `datetime.now(timezone.utc)`. Consumers depend on
the `Clock` protocol, not directly on wall-clock functions. Tests use a
`FixedClock`, so time-dependent behavior is deterministic without monkeypatching
global functions.

PostgreSQL connections set the session timezone to UTC. The database uses
`TIMESTAMPTZ`, and records returned to Python are checked for timezone
awareness. Raw MT5 server-time conversion belongs to Phase 1, not Phase 0.

## Risk Constitution

The checked-in constitution contains exactly the risk limits and prohibitions
from section 1.4 of the source specification.

Typed model validation enforces:

- version is a positive integer;
- `signature_required` is exactly `true`;
- both `core` and `sleeve` books exist;
- their capital fractions sum to exactly `1` using decimal arithmetic;
- percentage fields are bounded and all count/leverage limits are positive;
- every structural prohibition contains its required literal value;
- every safe-mode threshold is positive;
- loaded models are frozen.

No defaults are supplied for security-critical limits. A missing limit is an
error, not an invitation to guess.

## Ed25519 Authority Separation

### Signing flow

1. The operator runs an offline signing command with explicit input, signature,
   and private-key paths.
2. The command reads the YAML as exact bytes and signs those bytes.
3. The command writes a Base64-encoded 64-byte Ed25519 signature.
4. The private key remains external and is never copied into the application
   configuration, container, or audit store.

### Runtime verification flow

1. Read the constitution, signature, and public key as bytes.
2. Decode and structurally validate the public key and signature.
3. Verify the signature against the exact constitution bytes.
4. Only after verification, decode the YAML as UTF-8 and parse it with
   `yaml.safe_load`.
5. Validate the parsed value into a frozen `Constitution`.
6. Return the model with a metadata object containing the constitution SHA-256,
   version, and public-key fingerprint for audit use.

Whitespace, encoding, comments, or newline changes invalidate the signature.
The loader never rewrites or canonicalizes YAML before verification.

The bootstrap developer key lives in a gitignored local secrets directory. The
repository stores only the public key and the signature produced for the
checked-in constitution. Production key generation and custody are an operator
responsibility outside this codebase.

## PostgreSQL Audit Ledger

### Event representation

An `AuditEvent` contains:

- schema version;
- event UUID;
- event type;
- UTC occurrence time;
- actor and actor type;
- correlation and causation identifiers when present;
- canonical payload;
- optional source-component and subject identifiers.

The application validates the event and serializes the full event envelope to
RFC 8785 canonical JSON bytes. The database stores both those bytes and a
queryable `JSONB` representation.

### Ledger row

Each row contains:

- monotonic `sequence_number` primary key;
- event UUID with a uniqueness constraint;
- canonical event bytes;
- queryable event JSON;
- previous hash;
- entry hash;
- database receipt time as UTC `TIMESTAMPTZ`.

### Append transaction

The runtime role cannot insert directly. It executes a security-definer
`audit.append_event` function that:

1. obtains a transaction-scoped advisory lock dedicated to the ledger;
2. validates that the canonical bytes decode to the supplied JSON event;
3. rejects an existing event UUID;
4. allocates the next sequence number;
5. reads the previous entry hash, using a fixed genesis hash for the first row;
6. computes
   `SHA-256(domain_separator || sequence_number || previous_hash || canonical_event_bytes)`
   with `pgcrypto`;
7. inserts one row and returns the committed record.

The transaction commits all steps or none. The advisory lock serializes
concurrent writers, keeping a single unambiguous tail.

The function is owned by a non-login migration role, pins an empty trusted
`search_path`, schema-qualifies every referenced object, and revokes default
`PUBLIC` execution before granting execution to the runtime role. These rules
prevent object-shadowing and accidental broad access through the
security-definer boundary.

### Immutability controls

Migrations create separate ownership and runtime roles. The runtime role may:

- connect;
- use the audit schema;
- execute the append function;
- select audit records required for verification.

It may not directly insert, update, delete, truncate, alter, or drop ledger
objects. A trigger rejects updates and deletes as defense in depth. Migration
credentials are not available to the runtime process.

Database owners and superusers can always alter PostgreSQL data, so database
permissions alone are not presented as tamper-proof. The independently
verifiable hash chain makes such changes detectable.

### Integrity verification

Verification reads the ledger in sequence order and checks:

- sequence continuity;
- unique event identifiers;
- canonical bytes parse to the stored queryable JSON;
- the genesis hash;
- every previous-hash link;
- every recomputed entry hash;
- UTC-aware event and receipt timestamps.

It stops at the first invalid row and returns a structured `IntegrityReport`
with the failing sequence and reason. An empty ledger is valid.

## Startup and Health Gate

Startup has no degraded-ready state in Phase 0:

```text
verify constitution signature and schema
→ connect to PostgreSQL
→ set and verify UTC database session
→ check the exact expected Alembic revision
→ verify the full audit chain
→ append startup and constitution-loaded events
→ report READY
```

Any failed step returns a typed error, a stable nonzero CLI exit code, and no
ready status. Runtime startup checks migrations but never applies them.

CLI commands:

```text
trading-house constitution verify
trading-house constitution sign
trading-house db check
trading-house audit verify
trading-house health
```

Signing is an operator command and is not imported by the runtime health path.

## Error Handling

The public error hierarchy contains:

- `ConfigurationError`
- `SignatureVerificationError`
- `SchemaValidationError`
- `DatabaseUnavailableError`
- `MigrationMismatchError`
- `AuditAppendError`
- `AuditIntegrityError`

Low-level dependency exceptions are chained as causes while operator-facing
messages remain stable and redact DSNs, passwords, key material, and raw
environment values. Security errors fail closed. No module substitutes a
fallback constitution, timestamp, database, signature, or audit result.

## Testing Strategy

### Unit tests

- strict schema acceptance and rejection;
- extra-field rejection and model immutability;
- UTC conversion, naive-time rejection, and timestamp ordering;
- each canonical cross-field invariant;
- constitution cross-field and literal validation;
- valid signing and verification;
- missing, malformed, wrong-key, and tampered-file signature failures;
- deterministic canonical serialization and hash preimages;
- fixed and system clock behavior;
- stable CLI error mapping and secret redaction.

### Property tests

- arbitrary valid timezone offsets normalize to the same UTC instant;
- invalid timestamp orderings never construct `Stamped` models;
- any one-byte constitution mutation invalidates its signature;
- canonical serialization is deterministic across key ordering;
- arbitrary valid event sequences produce a reproducible hash chain;
- arbitrary extra model fields are rejected;
- executable order intents cannot be constructed without a positive stop.

### PostgreSQL integration tests

- migrations apply to a clean PostgreSQL 18 database;
- migration revision checks detect behind and ahead states;
- appends commit complete rows and failed appends leave no partial state;
- concurrent writers produce a continuous single chain;
- duplicate event UUIDs are rejected;
- the runtime role cannot insert, update, delete, truncate, or run DDL;
- mutation triggers reject direct changes by a test owner where applicable;
- administrative tampering, missing sequences, altered canonical bytes, and
  broken links are detected;
- PostgreSQL connection and receipt timestamps remain UTC-aware;
- database outages fail the health gate without leaking credentials.

### Acceptance tests

From a fresh checkout:

1. `uv sync --locked` succeeds.
2. Docker Compose starts PostgreSQL.
3. migrations apply with migration-owner credentials.
4. the committed constitution verifies against the committed public key and
   signature.
5. the runtime role appends and reads audit events through its permitted
   surface.
6. audit verification reports a valid chain.
7. the health command reports ready.
8. the complete test, coverage, lint, and type-check suite passes.
9. a repository scan confirms that no source module imports `MetaTrader5` or an
   LLM/agent framework.

## CI Design

The Linux job runs formatting checks, lint, strict typing, unit/property tests,
PostgreSQL integration tests, acceptance tests, and coverage enforcement using
a PostgreSQL 18 service container.

The Windows job installs from the same lock and runs lint, typing, unit, and
property tests. PostgreSQL integration behavior is proven once on Linux; the
Windows job guards the platform compatibility needed for later MT5 phases.

CI never receives a private signing key. Tests generate ephemeral keys in
temporary directories. The checked-in constitution is verification-only.

## Documentation and Operator Experience

The Phase 0 README documents:

- prerequisites;
- local `uv` and Docker setup;
- migration-owner and runtime-role separation;
- constitution verification and offline signing;
- health and audit verification commands;
- test and quality commands;
- why Phase 0 cannot trade;
- the handoff boundary to Phase 1.

An `.env.example` contains variable names and non-secret local placeholders,
never usable production credentials.

## Definition of Done

Phase 0 is complete when all acceptance tests pass in CI on a fresh checkout and
the following statements are demonstrably true:

- canonical schemas are strict, frozen, UTC-disciplined, and documented;
- modified or unsigned risk configuration prevents startup;
- no runtime component can sign or rewrite the constitution;
- audit appends are atomic, serialized, hash-chained, and immutable to the
  runtime database role;
- audit corruption is detected before ready status;
- migrations are explicit and never applied automatically by runtime;
- no MT5, order execution, strategy, or LLM path exists;
- repository setup and operator commands are reproducible from committed files.

## Future-Phase Boundary

Phase 1 may add the MT5 gateway only after this phase is accepted. It must:

- import canonical contracts rather than redefining them;
- use the Phase 0 clock at every timestamp boundary;
- load the verified frozen constitution through the Phase 0 loader;
- append gateway, configuration, and operational events through the Phase 0
  audit interface;
- preserve the rule that only the gateway module may import `MetaTrader5`.

Phase 0 deliberately makes no promise of profitability or trading readiness.
It supplies the controls required to begin broker integration safely.
