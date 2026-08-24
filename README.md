# Trading House — Phase 0 / 0.5 Safety Foundation

> **This repository still cannot trade.** There is no broker connection, no
> strategy, no sizing, no order path, and no LLM agent. That absence is
> deliberate and is enforced by tests. **Phase 1 may begin only after the
> acceptance suite below is green.**

Phase 0 establishes five guarantees:

1. Cross-module data is validated against one strict canonical schema set.
2. Every persisted timestamp is timezone-aware UTC, with `event_time`,
   `availability_time` and `processing_time` kept distinct.
3. The risk constitution is Ed25519-verified before it is parsed, and is
   immutable once loaded.
4. Every auditable event is appended transactionally to a PostgreSQL SHA-256
   hash chain that the runtime cannot update, delete, or truncate.
5. Startup reports ready only after configuration, database, migration and
   audit-integrity checks all pass.

Phase 0.5 revises the contracts those guarantees protect, without adding a
trading path: canonical data (`core/values.py`, `core/instruments.py`,
`core/schemas.py`) is venue-neutral, broker-shaped facts (MT5 magic numbers,
retcodes, server symbols) live only behind the venue-ref indirection in
`core/venue.py` and the signed venue binding, and the risk constitution is
split into four horizon-scoped books — see [Books](#books) below.

Design: [Phase 0 foundation](docs/superpowers/specs/2026-08-03-phase-0-foundation-design.md).
Revision: [Phase 0.5 architecture revision](docs/superpowers/specs/2026-08-22-trading-house-architecture-revision-design.md).

## Prerequisites

- Python 3.12 (exactly the 3.12 line; see `.python-version`)
- [uv](https://docs.astral.sh/uv/)
- Docker, for local PostgreSQL 18 and the integration suite

## Setup

```bash
uv sync --locked --all-groups
```

```bash
cp .env.example .env
```

Edit `.env` and replace every placeholder. Compose reads it automatically. The
application does not: `RuntimeSettings` declares no `env_file`, so a checked-in
`.env` can never silently configure production. Export `TRADING_HOUSE_*` into
your shell for the CLI to see them.

## Start PostgreSQL

```bash
docker compose up -d
```

The container publishes only on `127.0.0.1:5432` and creates three roles on
first initialisation. If you change role passwords later, remove the volume and
re-initialise — `docker-entrypoint-initdb.d` runs only on an empty data
directory.

## Apply migrations

Migrations run as the migrator, never as the runtime role, and never
automatically at startup.

```bash
uv run alembic -x url=postgresql+psycopg://trading_house_migrator:PASSWORD@127.0.0.1/trading_house upgrade head
```

If you prefer, set `sqlalchemy.url` in `alembic.ini` for local use. Do not commit
a password into that file.

## Database authority separation

| Role | Login | May |
|---|---|---|
| `trading_house_owner` | no | Own the schema; created by migrations |
| `trading_house_migrator` | yes | Assume the owner role and run migrations |
| `trading_house_runtime` | yes | Connect, `SELECT` the ledger, `EXECUTE audit.append_event`, read `alembic_version` |

The runtime role cannot `INSERT`, `UPDATE`, `DELETE`, `TRUNCATE`, `ALTER` or
`DROP` the ledger. Append authority exists only through the security-definer
function, which serialises on an advisory lock and computes the chain hash in
the database. Triggers reject row mutation and truncation as defence in depth.

Database owners and superusers can always alter PostgreSQL data, so permissions
alone are not presented as tamper-proof. The independently recomputed hash chain
is what makes tampering *detectable* — see `audit verify`.

## Books

The signed risk constitution declares four books, each with its own capital
fraction and horizon-scoped limits:

| Book | Horizon | Capital fraction | Asset classes |
|---|---|---|---|
| `fx_scalp` | scalp | 0.30 | fx, metal |
| `fx_swing` | swing | 0.45 | fx, metal |
| `equity_swing` | swing | 0.15 | equity_cfd |
| `sleeve` | swing | 0.10 | fx, metal |

All four trade through one MT5 account and therefore share one margin pool.
A book accounts for its own risk — capital fraction, per-trade risk, drawdown
halts, leverage — but does not *ring-fence* it: an `fx_scalp` EURUSD long and
an `fx_swing` EURUSD long are the same underlying market exposure held under
two book labels, and only the firm-level limits in `risk_constitution.yaml`
(`max_aggregate_open_risk_pct`, `max_correlated_cluster_risk_pct`,
`max_single_instrument_risk_pct`, `max_gross_leverage`) see and cap that
combined exposure. See `tests/acceptance/test_risk_authority.py`.

Book identity is venue-neutral. The signed venue binding
(`config/venue_binding.mt5.yaml`) is the only place that maps each book to an
MT5 magic-number range, and each canonical instrument (`fx.eurusd`,
`metal.xauusd`) to a server symbol — see `constitution binding` below.

## Operator commands

```bash
uv run trading-house --help
```

| Command | Purpose |
|---|---|
| `trading-house constitution verify` | Verify the signature and report version and hashes |
| `trading-house constitution binding` | Verify the signed venue binding and report its venue, books, and instruments |
| `trading-house constitution sign` | Sign exact constitution bytes offline |
| `trading-house db check` | Confirm the database is reachable and at the expected revision |
| `trading-house audit verify` | Independently recompute and verify the hash chain |
| `trading-house health` | Run the full readiness gate |

Every command prints deterministic, key-sorted JSON on stdout and errors on
stderr. No command prints a path, credential, or key.

### Verify the constitution

```bash
uv run trading-house constitution verify
```

### Verify the venue binding

```bash
uv run trading-house constitution binding
```

Reports the venue (`mt5`), and the sorted books and instruments the signed
binding covers. The binding carries everything that would change if the firm
switched brokers — magic-number ranges per book, server symbols per
instrument — so the constitution itself never has to.

### Sign a constitution offline

Signing happens on an offline machine. The private key is never committed, never
read by the runtime, and never leaves that machine.

```bash
uv run trading-house constitution sign --constitution config/risk_constitution.yaml --private-key .local/keys/risk_constitution.private.pem --signature-output config/risk_constitution.yaml.sig
```

The command refuses to overwrite an existing signature without `--force`, writes
Base64 plus one newline through a same-directory temporary file and a single
rename, and drops the key reference before exit.

**Private-key handling.** Keys live under `.local/`, which is gitignored. Only
`config/risk_constitution.public.pem` is committed. The acceptance suite fails if
anything matching `*private*` appears in `config/`. `.gitattributes` marks
`*.pem` and `*.sig` binary so no platform rewrites the bytes the signature covers.

Two artifacts are signed offline with the same key and the same command shape,
and both are checked in alongside their signatures:

| Signed artifact | Signature | Carries |
|---|---|---|
| `config/risk_constitution.yaml` | `config/risk_constitution.yaml.sig` | Venue-neutral risk limits, per book and firm-wide |
| `config/venue_binding.mt5.yaml` | `config/venue_binding.mt5.yaml.sig` | The MT5-specific projection: magic-number range per book, server symbol per instrument |

Re-sign the binding the same way as the constitution, pointing at its own path:

```bash
uv run trading-house constitution sign --constitution config/venue_binding.mt5.yaml --private-key .local/keys/risk_constitution.private.pem --signature-output config/venue_binding.mt5.yaml.sig --force
```

### Check readiness

```bash
uv run trading-house health
```

## Recovering from a startup failure

Each failure has a stable exit code and a fixed, redacted message.

| Exit | Error | Meaning | Recovery |
|---|---|---|---|
| 2 | `configuration invalid` | Missing `TRADING_HOUSE_DATABASE_DSN`, unreadable or malformed constitution YAML | Export the DSN; confirm the YAML parses and its limits are in range |
| 3 | `signature verification failed` | Signature, public key, or constitution bytes do not agree | Restore the committed trio, or re-sign offline. **Do not edit the YAML to make it load** |
| 4 | `database connection failed` | PostgreSQL unreachable, or the session refused UTC | `docker compose up -d`; check the DSN host, port and credentials |
| 5 | `migration revision mismatch` | Schema is not at the exact expected revision | Run `alembic upgrade head` as the migrator. Never migrate from the runtime process |
| 6 | `audit integrity verification failed` | The hash chain does not recompute | **Stop.** Do not append. Preserve the database and investigate; a mismatch means the ledger was altered outside the append function |
| 7 | `audit append failed` | The append transaction did not complete | Check connectivity and privileges; the ledger is unchanged because appends are transactional |
| 1 | `unexpected failure` | An unmapped error, reported with a correlation id | Re-run with `--debug` to see the traceback locally |

## Tests and quality gates

```bash
uv run pytest
```

```bash
uv run pytest -m "not integration" --no-cov
```

```bash
uv run ruff format --check . && uv run ruff check . && uv run mypy
```

```bash
uv lock --check
```

The full suite requires Docker: integration and acceptance tests provision
PostgreSQL 18 through testcontainers, create the three roles, and apply
migrations with migrator credentials. Coverage is gated at 95% and is only
meaningful with the integration tests included — without them the repository
measures about 82%, so the Windows CI job runs `-m "not integration" --no-cov`
and the gate is enforced on Linux.

### Test layers

| Path | Layer |
|---|---|
| `tests/unit` | Contracts, clocks, errors, signing, canonical hashing, CLI |
| `tests/property` | Hypothesis invariants for UTC, signatures, canonical JSON, chains |
| `tests/integration` | Real PostgreSQL: migrations, privileges, appends, tamper detection |
| `tests/acceptance` | Architecture guards and the Phase 0 / 0.5 end-to-end gates |

### The Phase 0 acceptance gate

```bash
uv run pytest tests/acceptance -q
```

This applies migrations with migrator credentials, verifies the checked-in
constitution, appends as the runtime role, recomputes the chain, runs the
readiness gate, re-verifies, and asserts no broker or agent dependency exists.

`tests/acceptance/test_phase0_5.py` adds the Phase 0.5 gate: the revised,
venue-neutral constitution verifies and still declares all four books, the
signed venue binding verifies and covers every one of those books, the
constitution YAML names no venue, and `core/values.py`, `core/instruments.py`
and `core/schemas.py` never reference a broker-specific encoding (magic
numbers, retcodes, fill modes). `core/venue.py` is deliberately exempt — it is
the one place `Mt5VenueRef` is allowed to carry those facts.

## What this foundation deliberately excludes

MetaTrader 5 or any broker; strategies, sizing, risk evaluation or execution;
market data and backtesting; LangGraph or any LLM SDK; web APIs and dashboards;
automatic migration at startup; and live, paper, shadow or simulated trading.

The absence is testable. `tests/acceptance/test_architecture.py` parses every
source module and fails on an import of `MetaTrader5`, `langgraph`, `openai`,
`anthropic` or `ccxt`, on private-key primitives outside
`constitution/signing.py`, and on any Alembic upgrade path in runtime code.
