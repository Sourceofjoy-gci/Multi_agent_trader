# Trading House — Phase 1 Read-Only MT5 Gateway

> **This repository still cannot trade.** It now talks to MetaTrader 5, but
> only to read. There is no order path, no strategy, no sizing, and no LLM
> agent. The gateway refuses to start against anything but a demo account,
> and `submit`, `amend_protection` and `close` raise `NotImplementedError`
> until Phase 3 brings the intent ledger that makes a lost response
> recoverable. That absence is deliberate and is enforced by tests.

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

## Phase 1 — MT5 gateway

Phase 1 connects to one MetaTrader 5 terminal and reads from it. It sends no
orders.

### Demo accounts only

`Mt5Gateway.start()` reads `account_info().trade_mode` and raises
`refusing to operate a non-demo account` (exit code 9) unless the terminal
reports a demo account. The check runs before the actor thread is created and
shuts the terminal down on its way out, so there is no state in which the
system is connected to a live account and merely not trading yet.

A contest account is refused too. Only `ACCOUNT_TRADE_MODE_DEMO` passes.

### What it can and cannot do

| Method | Phase 1 |
|---|---|
| `describe_instrument` | Reads `symbol_info` and translates it into price units |
| `snapshot` | Reads ticks; skips any instrument the terminal has no tick for rather than fabricating a quote |
| `precheck` | Calls `order_check`, which simulates without touching the market |
| `reconcile` | Reports venue positions as **unmatched**; see below |
| `health` | Reports connection state, server clock offset and quote age |
| `submit`, `amend_protection`, `close` | Raise `NotImplementedError` until Phase 3 |

`reconcile` returns an empty `positions` tuple in this phase. `PositionState`
requires `strategy_id`, `lifecycle`, `r_multiple_open`, `mae_r`, `mfe_r` and
`initial_risk_distance`, none of which are derivable from a raw MT5 position
— they need the intent ledger. Everything found is reported through
`unmatched_venue_refs`, which is exactly what that field means when there is
nothing to match against. Phase 3 migrates them into `positions`.

A position whose magic falls in no declared range — a manually opened trade,
which MT5 tags with magic `0` — is reported under every book, so that it is
never invisible. Health counts distinct `position_ticket` values, not summed
report lengths, so one manual trade is one open position.

### Where MetaTrader 5 lives

Exactly one module imports it: `src/trading_house/brokers/mt5/terminal.py`.
Everything else — the DTOs, the gateway, the contract translation, the retcode
mapping, the adapter — is pure Python that imports and tests on Linux, where
the coverage-gated CI job runs. `terminal.py` is omitted from coverage and
capped at 80 statements by `tests/acceptance/test_architecture.py`, so it
cannot quietly become a home for untested logic.

### Auditing a broker

The audit script connects read-only, refuses a non-demo account, and prints
what the broker actually reports for each bound symbol. It is how you find out
that a broker's real numbers disagree with an assumption before code does.

```bash
uv run python scripts/broker_audit.py
```

### Running the live tests

These need MetaTrader 5 running on Windows, logged into a demo account. They
skip with a specific reason otherwise, including when the logged-in account is
not a demo. Identifying the account type requires connecting, so a live
account is briefly connected to and immediately disconnected — nothing is read
from it beyond `account_info()`, and no test body runs.

```bash
uv run pytest tests/live -m mt5 -rs
```

## Phase 1.5 — market data

Phase 1.5 adds append-only bar storage: bars are graded for quality on the
way in, requests are paged around the broker's own request-size ceiling, and
every read is filtered by an explicit `availability_time` (I-17) — a bar
stamped 09:00 was not knowable until 09:01, and no consumer read path can see
it before then. Storage never rewrites a row; a revised broker history is
counted as a conflict, not silently applied.

### Commands

```bash
uv run trading-house data backfill --instrument fx.eurusd --timeframe H1 --from 2020-01-01
uv run trading-house data update
uv run trading-house data coverage
```

- **`data backfill`** walks one instrument/timeframe pair backward, from
  whatever is already stored (or now, on a fresh key) toward `--from`.
  `--instrument` and `--timeframe` carry no default — a backfill is a
  deliberate, long-running act, and defaulting either invites one nobody
  meant to start.
- **`data update`** walks every instrument in the signed venue binding
  forward, across all six timeframes, from each key's latest stored bar to
  the last fully closed boundary. There is no separate cursor table; the
  bars already on disk are the only bookmark either command needs.
- **`data coverage`** reports what the store currently holds, per instrument
  and timeframe. It reads only the database and never touches MetaTrader5.

### What `coverage()` is for

`coverage()` answers "what does the store hold", not "what can be read right
now" — those are different questions with different answers. Its
`earliest_event_time` and `latest_event_time` describe the stored range;
`latest_availability_time` is the newest instant as of which some stored bar
was actually knowable. `bars()` is the read path that enforces the
availability guarantee; `coverage()` exists so a caller can ask what a key
holds — including a key that holds nothing at all — without paying `bars()`'s
`CoverageError` for asking outside stored coverage.

### Measured history depth

Depth was measured against FBS by backfilling each instrument/timeframe pair
to the broker's own depth wall. It varies by more than an order of magnitude
across timeframes because MetaTrader 5 caps a request by result size, not by
age: a bar is one response row whether it spans a minute or a day, so a
coarser timeframe reaches proportionally further into history for the same
row budget.

| Timeframe | EURUSD | XAUUSD |
|---|---|---|
| M1 | ~3 months | ~3 months |
| M5 | ~16 months | ~17 months |
| M15 | ~4.1 years | ~4.2 years |
| H1 | ~16.3 years | ~11.6 years |
| H4 | ~25 years | ~11.6 years |
| D1 | back to 2000 | back to 2000 |

Plainly: **M1 holds roughly three months.** Anything that reasons over older
M1 history hits `CoverageError` at the store boundary rather than silently
running on a truncated window it never chose.

## Phase 2 — features

`FeatureEngine` (`trading_house.features.engine`) computes indicators from
stored bars over a window it fixes itself, not whatever the store happens to
hold. It currently offers:

- **`atr(instrument_id, timeframe, period=, as_of=)`** — Wilder's ATR.
- **`median_spread_points(instrument_id, timeframe, window=, as_of=)`** —
  the median bar spread, in points (converting to price needs the
  instrument contract, which lives behind the broker adapter — Phase 3's
  problem, not this one's).

Both are pure functions under the hood
(`trading_house.features.indicators.volatility.wilder_atr`,
`.spread.median_spread_points`) fed a bar window the engine assembles; an
indicator never touches the store itself.

`atr` asks for `period * WARMUP_MULTIPLE + 1` bars, not just `period` —
Wilder's smoothing is recursive, so its value depends on where the series
started, and a shorter window would let that seed's influence show up in
the answer. Ten periods of warm-up puts it below anything that matters, and
makes the window a fixed constant rather than a judgement call made fresh
at every call site. The `+ 1` is separate from warm-up: true range needs a
previous close, so turning `n` bars into true ranges yields only `n - 1` of
them, and the extra bar buys back the one warm-up would otherwise lose.
This is also why the same `(instrument_id, timeframe, period, as_of)`
always returns the same number regardless of how much history has piled up
behind it since (**I-18**): the window is fixed, not "everything
available."

A window the store cannot fill — a young key, or an `as_of` too close to
the start of history — raises `InsufficientHistoryError` rather than
returning a shorter answer. A stop sized on quietly less history than the
caller assumed is a wrong stop with no error to show for it.

## Phase 3 — risk and sizing

`RiskEngine` (`trading_house.risk.engine`) is the deterministic gate every
`TradeProposal` must clear before it can size a position: it checks the book,
the instrument, the side, the spread and tick freshness, then sizes the
position from a monetary loss budget and a stop distance (never from a
profit target or a model's confidence, per **I-3**) and returns an
`APPROVED`, `RESIZED` or `REJECTED` decision.

It exposes two entry points, not one:

- **`evaluate(...)`** is pure over its arguments — the same proposal and the
  same market facts always yield the same decision — so a backtester with no
  terminal can replay it exactly.
- **`evaluate_for_execution(...)`** adds the master spec's 8.1 free-margin
  headroom check via a `MarginPort`. That check needs a live account, so it
  cannot appear in the pure path; splitting the two means forgetting the
  margin check is a differently named function, not a forgotten line.

`book_equity` — the amount the sizing arithmetic risks a percentage of — is
the *book's own slice* of firm equity (`firm_equity * capital_fraction`), not
firm equity itself. Passing firm equity there would over-risk every sleeve by
however much its capital fraction understates.

The stop distance is computed first and **quantises up** to the instrument's
tick grid; the volume is then computed from that already-widened distance and
**quantises down** to the lot grid. Both roundings push the same way, so
realised risk is bounded above by the budget instead of straddling it
(**I-19**) — reversing either rounding direction would let a position exceed
the signed risk budget on roughly half of all trades.

## Phase 4 — execution

Phase 4 is the write path: an append-only intent ledger, an order manager
that sends to the venue exactly once, a reconciler that resolves whatever a
lost response left ambiguous, and the CLI commands that drive all three.

**The durability point.** `IntentLedger.append()` commits to Postgres
*before* `OrderManager.submit()` ever calls the venue — SUBMITTING is durable
on disk before an order can possibly exist at the broker. MT5 carries no
client order ID, so a timed-out send is genuinely ambiguous between "lost on
the way out" (nothing happened) and "lost on the way back" (the order
executed and the confirmation never arrived), and nothing in the response
tells the two apart. Writing SUBMITTING first is what makes that intent
findable afterwards regardless of which one occurred; a crash between the
write and the send would otherwise leave a real broker position that nothing
would ever go looking for.

**`submit()` never reconciles.** A lost response is recorded as UNKNOWN and
`submit()` returns — it does not retry, and it does not poll the venue to
find out what happened. Resolving an UNKNOWN intent is `reconcile_all()`'s
job, run either by `order reconcile` or by the same recovery path after a
crash. Folding that into `submit()` would mean the resend path and the
crash-recovery path are different code, and only one of them would ever be
exercised routinely.

**The gate runs on every order-placing command.** `require_clean_ledger`
(I-20) first runs the reconciler over every non-terminal intent, then refuses
if any of them survives that — naming it — before `order submit` builds an
intent or sends anything. So a restart self-heals rather than waiting for an
operator to type `order reconcile`, and it still fails closed on whatever the
sweep could not resolve. `order reconcile` and the read-only `order status`
are ungated: the first is what clears the condition, and gating a diagnostic
would make it refuse exactly when an intent is stuck.

### Commands

```bash
uv run trading-house order submit --intent-id ... --strategy-id ... \
  --book fx_scalp --instrument fx.eurusd --side BUY --decision approved-decision.json
uv run trading-house order reconcile
uv run trading-house order status --intent-id ...
```

- **`order submit`** reads a serialised `ApprovedRiskDecision` (or
  `ResizedRiskDecision`) from `--decision`, builds one `OrderIntent` from its
  `approved_quantity` and `stop_loss_price`, stamps the venue magic from the
  signed binding, and calls `OrderManager.submit()`. There is deliberately no
  `--quantity` and no `--stop-loss`: size and stop are Phase 3's output, and
  free parameters would put every risk gate — `max_spread_fraction_of_stop`
  included — outside the path of the only command that can trade. A REJECTED
  decision is refused. Two guards run before anything is sent: a PostgreSQL
  advisory lock, so two invocations serialise instead of both reading a clean
  ledger, and the gate (I-20).
- **`order reconcile`** resolves every non-terminal intent against the venue's
  own deal history **and its open positions**, writing back CONFIRMED (at the
  volume actually filled, which a partial fill makes smaller than the one
  requested), FAILED, or leaving it STILL_UNKNOWN and marking gateway state
  stale. Ungated — it is what clears the gate for everything else.
- **`order status`** reports one intent's full ledger history. Ungated: it is
  read-only, and it is the command an operator needs most at exactly the
  moment an intent is stuck.

## Phase 5 — the position guard

Phase 5 makes one promise: **every position this system opened is, at every
moment, carrying the stop the ledger says it carries — and if it is not, the
guard notices within a second and puts it back.** That is I-21: every open
position is verified against its recorded protection at least once per cycle,
and a missing stop is restored, or escalated after two failed restore
attempts.

**The guard is deliberately not gated by the unresolved-intent check.** Every
order-*placing* command runs `require_clean_ledger` (I-20) first. `guard run`
does not, and must not. It opens nothing, so there is nothing for that gate to
protect against — and a guard that stopped protecting live positions because
an unrelated intent was stuck would abandon money at the worst possible
moment. "Run the gate everywhere" is the plausible-looking mistake here.

**It never closes a position.** There is no close path in the guard at all.
It restores a stop, it adopts an orphan at a distance it was given, it
records, and it escalates — nothing else. Closing is a decision the guard has
no standing to make.

**Escalation is three things and no more:** gateway state is marked stale, one
row goes into the hash-chained audit ledger, and one position event is
written. There is no alerting subsystem and no safe-mode state machine in this
codebase; an operator learns of an escalation by reading the audit ledger or
running `guard status`. After escalating, the guard stops attempting
modifications on that position — terminally, for the life of that position's
record — so the first, true diagnosis is not buried under a day's worth of
repetitions of itself. Nothing in this phase clears it: `reconcile_all` only
touches the intent ledger, and `mark_reconciled()` clears gateway staleness,
not a position record. An operator finds out from `guard status` or the audit
ledger; making escalation clearable at all is a decision for a later phase,
not something this one builds.

Two failed restores escalate, not one (D-7). Today, I-8 holds structurally:
`decide()` can never emit a widening in the first place — every stop it
returns is the recorded one, the broker's own, or a freshly computed orphan
distance — and `amend_protection()` independently refuses a widening handed to
it directly, before anything reaches the broker. `decide_tighten()` is where
I-8's no-widening rule lives for a *trailing* candidate, but trailing is
deliberately out of scope this phase (spec §10) until a strategy can A/B it,
so nothing calls it yet — it, and the generative property test that pins it,
are pre-built and pre-proven ahead of that caller, not dead code.

**MAE/MFE are sampled at cycle resolution, not true extrema.** The `mae_r` and
`mfe_r` on a position event are the worst and best R-multiples the guard
*observed*, once per cycle, at whatever the closing price was when it looked —
the bid for a long, the ask for a short. A spike between two cycles did not
happen as far as these numbers are concerned. They are analytics, deliberately
`float`, and they are not a substitute for tick data.

### Commands

```bash
uv run trading-house guard run --interval-seconds 1
uv run trading-house guard status
```

- **`guard run`** starts the daemon and runs until interrupted. SIGINT sets
  the stop event rather than killing the process, so a shutdown leaves a dated
  row behind instead of a gap nobody can explain. Both stop distances it needs
  are per instrument: the minimum is read from each bound contract's stops
  level at startup, and the default adoption distance — the book's `k_sigma`
  against that instrument's current ATR — is **not supplied yet**, because
  nothing feeds an ATR to this daemon and a distance computed once at startup
  would be as invented as another symbol's. An orphan with no broker-side stop
  therefore escalates rather than being adopted at a fabricated distance (D-5);
  an orphan that already carries its own stop is adopted at it, as before.
- **`guard status`** reports every position the store still holds open, its
  recorded stop, and whether it has escalated — an escalated position is by
  design no longer watched, and appears here precisely so a human can see
  that. It reads the position store and
  nothing else — no terminal, no gate — because an escalation is exactly when
  the broker may be the thing that is broken.

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
| `trading-house guard status` | Report every position the guard watches, and any that escalated |
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

Reconciliation is the gate's fifth step, running after the audit chain is
verified and before the startup events are appended. It reports; it never
fails readiness — Phase 1 owns no positions and has no ledger to compare
against, so a manual demo trade is information, not a fault. The JSON gains
`books_reconciled` and `open_positions`.

If MetaTrader 5 is unavailable, the binding is absent, or the terminal will
not start, the venue step is simply not performed: `books_reconciled` is empty
and `open_positions` is zero. A **live account is not** treated that way — it
fails the gate with exit code 9.

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
| 8 | `broker terminal unavailable` | MetaTrader 5 is not running, will not initialise, or the server-clock probe read a stale tick from a closed market | Start the terminal and log in. During the venue step of `health` this degrades to "not performed" rather than failing the gate |
| 9 | `refusing to operate a non-demo account` | The terminal reports a real or contest account | **Stop.** Log into a demo account. This never degrades to a skipped step — it is the one venue failure that fails the gate |
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
| `tests/acceptance` | Architecture guards and the Phase 0 / 0.5 / 1 end-to-end gates |
| `tests/live` | Marked `mt5`; needs a real demo terminal, skipped everywhere else |

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

## What this repository deliberately excludes

Order submission of any kind; strategies, sizing and risk evaluation; market
data ingest and backtesting; LangGraph or any LLM SDK; web APIs and
dashboards; automatic migration at startup; and live, paper, shadow or
simulated trading. MetaTrader 5 is present but read-only, and reachable from
one module.

The absence is testable. `tests/acceptance/test_architecture.py` parses every
source module and fails on an import of `langgraph`, `openai`, `anthropic` or
`ccxt`, on an import of `MetaTrader5` from anywhere but
`brokers/mt5/terminal.py`, on private-key primitives outside
`constitution/signing.py`, and on any Alembic upgrade path in runtime code.
`tests/acceptance/test_phase1.py` fails if the string `order_send` appears
anywhere in `src/`, if any mutating adapter method stops refusing, or if the
gateway ever serves a non-demo account.
