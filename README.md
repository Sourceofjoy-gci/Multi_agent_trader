# Trading House — Phase 7 Session Momentum

> **This repository can place orders, and only ever against a demo account.**
> Phases 1.5 to 6 added market-data ingest, the signed risk constitution and
> sizing, an idempotent order path, a position guard, and a deterministic
> backtester that replays stored bars through the same risk engine. Phase 7
> registers the first real strategy and the three-arm runner that measures it.
>
> **There is still no funded account, LLM agent, or demonstrated edge.** The
> MT5 gateway refuses every non-demo login, and MetaTrader 5 is reachable from
> exactly one module; both properties are enforced by
> `tests/acceptance/test_architecture.py` and `test_phase1.py`. Session
> Momentum is a falsifiable hypothesis, not a claim of profit. A negative
> result is a completed phase and is passed to the next one unchanged.

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

Phase 8A added a **second database**, `trading_house_research`, and it is a
second deployment target for the *same* migration history — not a schema of its
own. Migrate it too, or every `research trial` command refuses with exit code 5:

```bash
uv run alembic -x url=postgresql+psycopg://trading_house_migrator:PASSWORD@127.0.0.1/trading_house_research upgrade head
```

Both databases must sit at the same head. If you prefer, set `sqlalchemy.url`
in `alembic.ini` for local use — once per database — and do not commit a
password into that file.

## Database authority separation

| Role | Login | May |
|---|---|---|
| `trading_house_owner` | no | Own the schema; created by migrations |
| `trading_house_migrator` | yes | Assume the owner role and run migrations |
| `trading_house_runtime` | yes | Connect, `SELECT` the ledger, `EXECUTE audit.append_event` and `EXECUTE research.append_trial_ledger_event`, read `alembic_version` |

The runtime role cannot `INSERT`, `UPDATE`, `DELETE`, `TRUNCATE`, `ALTER` or
`DROP` the ledger. Append authority exists only through the security-definer
function, which serialises on an advisory lock and computes the chain hash in
the database. Triggers reject row mutation and truncation as defence in depth.

The same three roles exist in both databases and the same separation holds in
each. On the two ledger tables specifically, `trading_house_runtime` holds
`SELECT` on `research.trial_ledger_events`, nothing at all on the
`research.trial_ledger_heads` cache, and `EXECUTE` on the append function — no
`INSERT`, `UPDATE`, `DELETE` or `TRUNCATE` on either. That claim is scoped to
those tables, because the research database carries the full migration history
and the role holds `INSERT` on several other tables there. See
[Phase 8A](#phase-8a--canonical-evidence-and-trial-ledger).

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

## Phase 6 — the backtester

Phase 6 makes one promise: **a strategy's proposals go through the real risk
engine, fill pessimistically against stored bars in point-in-time order, and
produce a result two different processes agree on byte for byte.** Nothing is
sized here — `RiskEngine.evaluate_for_execution` sizes, and the simulator reads
`approved_quantity` and `stop_loss_price` off the decision it returned (D-3).
Sizing has exactly one home, so a measured edge belongs to the strategy rather
than to a second copy of the arithmetic that no live order goes through.

Per bar, in this order, and the order is the design: a queued entry fills at
**this** bar's open (never the close that generated the signal); an open
position is resolved against this bar, the stop before the target (D-2) and the
stop before the time stop; `as_of` becomes the bar's `availability_time`; a
snapshot is built at that instant, or the bar is skipped because the feature
windows are still cold; under a chandelier exit policy an open position's stop
is trailed from that snapshot's ATR; the strategy sees that snapshot and nothing
else (I-17); the risk engine decides; an executable decision is queued to fill
on the next bar.

**The trail runs after the bar's own exits, not before them.** A stop set from a
bar's high must not then be tested against that same bar's low: OHLC cannot say
which came first, and reading it the other way flatters rather than punishes,
because a raised long stop that the bar's low reaches fills *better* than the
one it replaced. The level set at the end of bar *i* governs bar *i+1*, which is
also what the live guard does — it reacts to a bar that has closed. A bar with
no snapshot trails nothing.

**What it does not promise, stated as plainly:**

- **Bar resolution only.** There is no tick stream. `tick_spread_points` is the
  closing bar's own spread and `tick_time` is that bar's `availability_time` —
  constructed invariants, not a convention. Any strategy whose edge lives in the
  difference between the two is one D-1 refuses anyway.
- **No strategy has a demonstrated edge yet.** Phase 7 registered Session
  Momentum and completed its three-arm evidence run. All three arms lost money
  after costs; that negative result completes Phase 7 and does not trigger
  tuning or promotion.
- **Market impact is not modelled.** Fills assume the requested size was always
  available at the price the model computed. A size that would move the book
  fills exactly as a small one does.
- **Inference cost is not modelled.** An agent's latency and its money cost
  appear nowhere in the cost equation.
- **Margin is not modelled.** Nothing in this repo can supply a margin
  requirement, so section 8.1's free-margin headroom gate is switched off rather
  than fed an invented number. A run assumes margin was always available. The
  emitted payload says so rather than leaving it to this paragraph: it carries
  `"margin_modelled": false` beside the digest, so a reader of the JSON — Phase
  8's trial ledger included — sees the assumption without reading the README.
- **Compounding does not happen.** `--firm-equity` is constant for the whole run
  (D-4), so a measured edge cannot be an artefact of position sizes growing with
  the strategy's own luck.
- **The cost breakdown in the result is partial.** Section 7.1 lists five
  modelled terms; `SimulatedTrade` names only two of them — `commission` and
  `swap`. Spread and slippage are charged inside the fill prices, so they are
  already inside `gross_pnl`, which is therefore gross of commission and swap
  and *net* of spread and slippage. `net_pnl` is the correct total either way;
  it is the attribution that is incomplete. Splitting spread and slippage into
  their own fields changes the model, the digest and every known-answer number
  this phase's proof is built on, so it lands with Phase 8's cost attribution
  rather than at the end of this one.
- **Spread is charged asymmetrically by exit kind, deliberately.** A round trip
  that exits on its stop or its target pays half a spread — the entry crossing
  only — because those exits fill at a resolved price level rather than at a
  quote. One that exits on the time stop pays a full spread, because a time
  stop fills at the next bar's open exactly as an entry does and crosses the
  half-spread a second time. Spec section 7 defines each case separately and
  the code follows it; this is a stated property, not an oversight.
- **A fill is stamped at the instant of its price** — `bar.event_time`, not
  `bar.availability_time`. Phase 6 shipped the latter and this list carried the
  defect forward as a stated non-promise; Phase 7 corrected it, so `entry_at`
  and `exit_at` now name the instant the fill happened, `max_holding_seconds`
  is honoured as H rather than H plus one bar, and `swap_cost` sees the right
  date pair (D-9). Every known-answer number in the phase moved with it.

**Five refusals, each rather than a plausible-looking number:**

| Kind | Refused because |
|---|---|
| `coverage` | The requested range reaches outside what the store holds — or runs backwards, which reaches no bar at all and would otherwise report an empty run |
| `defective_bar` | Defective bars exceed the request's declared fraction; tolerated bars are counted in the result and skipped rather than silently shortening the run |
| `exit_policy` | The proposal and declared arm disagree, or a Chandelier candidate cannot be a positive price; neither may run under the wrong arm's name |
| `lookahead` | A proposal claimed availability later than the snapshot that produced it |
| `horizon` | The strategy's horizon is shorter than ten bars of the timeframe (D-1), below which the number being measured is the simulator's own pessimism |

Interior gaps are deliberately **not** refused: FX closes every weekend, so a
gap rule would refuse every run spanning a Saturday.

**`max_concurrent_positions` is enforced nowhere at decision time.** It appears
only in a constitution self-consistency validator, and neither risk-engine entry
point takes open-position state. This is a live-system gap, not a backtest one.
D-7 sidesteps it — the simulator holds one position at a time, so it can never
report concurrency the live path does not limit — and closing it belongs to
whichever phase gives the risk engine a portfolio view.

**The result is reproducible across processes, not across changes.**
`tests/integration/research/test_backtest_determinism.py` runs the command
twice in separate interpreters under different `PYTHONHASHSEED` values and
compares the bytes, which is what catches set or dict ordering reaching the
output. It does not establish that the digest survives an unrelated edit:
`digest()` hashes a `Decimal`'s string form while the reconciliation validators
compare by value, so `Decimal("93")` and `Decimal("93.00")` both validate and
hash differently. That question is open.

## Phase 7 — Session Momentum and the exit A/B

### Strategy and rationale

`session_momentum_eurusd` is the registry's only strategy. Its hypothesis is
narrow: information repriced in thin overnight EURUSD conditions can continue to
be absorbed when London liquidity returns. On the first closed M15 bar at the
London open, it trades the sign of the prior completed session's return and
holds until 16:00 UTC. A missing prior window and an exactly flat return are
different states and neither produces a proposal.

The rule is forced onto EURUSD M15 and the `fx_swing` book. Its structural
invalidation is a pre-declared 10 pips from entry; `RiskEngine` may widen the
executable stop for volatility, spread, contract, and broker-distance
constraints. The declared economics were evaluated by the completed three-arm
run. Every arm lost money after costs, so the thesis is invalidated and no
tuning or promotion follows. The pre-run expected-return, cost, and
win-probability values remain recorded for provenance; this evidence does not
recalibrate them. Expected swap is explicitly zero because the position is flat
before the daily rollover; omission would not mean zero.

### Session features

`FeatureSnapshot` adds four point-in-time fields, all computed from closed bars:

- `session`: the window containing the bar's event time.
- `prior_session_return`: close-to-close return of the immediately preceding
  completed window, or `None` when that window has no complete data.
- `session_open_price`: the current window's first open.
- `bars_since_session_open`: zero on its first closed bar.

The windows are fixed UTC ranges: Asian `00:00–07:00`, London `07:00–16:00`,
New York `12:00–21:00`, and `OFF` elsewhere; London wins their overlap. Fixed
UTC is deliberate: timezone-aware definitions would make results depend on a
machine's tzdata. The cost is an explicit DST limitation—these windows do not
follow the exchange's local-time clock changes.

### Risk gates

The existing spread, spread-to-stop, and tick-staleness gates still bind every
proposal. Phase 7 adds three proposal-level comparisons from the signed
constitution: `min_expected_edge_after_cost_bps`,
`max_position_duration_seconds`, and `max_swap_cost_pct_of_expected_edge`. Each
applicable book limit must clear before the real risk engine can size a
proposal.

### Three arms, three trials

`--exit-policy` is required and accepts exactly three predeclared arms:

| Arm | Fixed parameters |
|---|---|
| `none` | Engine stop plus the 16:00 UTC time stop |
| `fixed_target` | `r_multiple = 1.0` |
| `chandelier` | `atr_multiple = 3.0`, `min_step_points = 10` |

The fixed-target strategy proposal carries `target_r_multiple = 1.0`; the other
two carry `None`. `RiskEngine`, not the strategy or simulator, computes the target
price from the stop distance it actually emitted. These parameters are not CLI
options: exposing them would invite a sweep, and every extra trial would weaken
the Deflated Sharpe that later consumes this evidence. A rerun after seeing any
result is another trial and must be counted.

### Completed evidence

The backfill was `TRUNCATED` at the broker history wall. It stored the span
`2022-09-16T01:30:00Z` through `2026-09-25T03:15:00Z` for `fx.eurusd` on the
FBS demo terminal: 99,988 clean M15 bars, 0 defective bars, 6 duplicates, and
0 conflicts. The backfill run ID is
`ce9fb2af-a640-471b-9ffb-93bbf39172ad`.

| Arm | Trades | Net P&L after costs | Defective fraction | Run ID | Result digest |
|---|---:|---:|---:|---|---|
| `none` | 1035 | `-1174.29200000015850` | `0/99988` | `d5a77ec90521cad5706d1caf0b45a6e4cc26c8291c5bb9739520bb31331af895` | `a10b0fa43fc4ddb0185b3a376caab7c9369ec49e3e347d13171d31ab989f022b` |
| `fixed_target` (1.0R) | 1035 | `-15578.74700000010180` | `0/99988` | `af7f728e84cca8101a3a10f4698d72f35da85fc6b069333f66588d0bbb150d73` | `fe5efadef7f5ea2448957e0bec507f2133755df676ade140b05a120cfb5fbc6b` |
| `chandelier` (3.0 ATR, 10-point step) | 1035 | `-25120.06200000013010` | `0/99988` | `4708f33cba5ee0f08c4b28ef56a79673d44209988d9576c899500f335b085051` | `69867de09ef3993e5aabca78b225b21bb1ef82a4aa4fbcd63a9fd52dfe2e0c54` |

Ranking: `none > fixed_target > chandelier`. The actual trail decision is
`trail=none`, and the trial count is `3`. All three arms lost money after costs;
this negative result completes Phase 7. It does not trigger tuning or
promotion.

Recorded run assumptions:

- Contract digest: `59121ba95a21afb81e48f4de9c9358705375c678954fa2249dbbd80b41b86f90`;
  verified constitution SHA-256:
  `a87e63fb8c46912b1bc21bae3e55535b88ae4abc613cb87988399c4c28e5d58b`.
- Firm equity `100000`; ATR period `14`; spread window `20`.
- Commission `0.0` per lot per side (official FBS publishes no commission);
  slippage `0.4` points per side (predeclared prior); swap long `-7.7` and
  short `+2.0` points/day (terminal audit).
- Triple swap on Wednesday; stress multiplier `1`; defective-bar tolerance
  `0`.


`--defective-bar-tolerance` is a finite decimal fraction in `[0, 1]`, parsed as
`Decimal` and compared as an exact rational. Its default is zero. A run skips
tolerated defective bars, reports their count, and refuses when their exact
fraction exceeds the declared ceiling. This keeps real broker data usable
without selecting clean date ranges after seeing outcomes.

The command is deliberately fixed to EURUSD M15: neither scope is a CLI option.
The saved evidence contains one run per arm; it is not a prompt to rerun, tune,
or promote the strategy.

### Commands

```bash
uv run trading-house backtest run \
  --strategy session_momentum_eurusd \
  --exit-policy none \
  --start 2022-09-16T01:30:00 --end 2026-09-25T03:15:00 \
  --firm-equity 100000 --contract contract.json \
  --atr-period 14 --spread-window 20 \
  --commission-per-lot-per-side 0.0 --slippage-points-per-side 0.4 \
  --swap-long-points-per-day -7.7 --swap-short-points-per-day 2.0 \
  --triple-swap-weekday 2 --stress-multiplier 1 \
  --defective-bar-tolerance 0
```

For `fixed_target` and `chandelier`, use the corresponding predeclared arm; the
saved result digests and run IDs above are the evidence of those three runs.

- **Every cost is required and none is defaulted** (D-5). `InstrumentContract`
  has no commission field and `FinancingModel` is an enum tag with no rate
  table, so each cost is a declared input taken from the broker's published
  contract specification and recorded in the result. Two options default and
  neither is a cost: `--stress-multiplier`, the 1.5x–2x sensitivity knob
  section 12 asks for, and `--defective-bar-tolerance`, whose strict default is
  zero. `--stress-multiplier` is bounded strictly above zero, because at zero
  every cost in the equation
  vanishes and below zero every one becomes a credit — which is the
  zero-commission backtest D-5 forbids, reached through the one option D-5's
  own guard exempts. Values below 1 are permitted, as the legitimate
  sensitivity probe in the other direction, but a result produced below 1
  flatters the strategy and is not evidence it passes anything. And because
  the swap rates are signed and the multiplier applies to the whole model,
  stressing a *positive* carry increases profit, so a positive-carry strategy
  clears the section 12 gate more easily at 2x than at 1x; section 7.1
  mandates one multiplier over the whole model, so this is a stated
  consequence rather than a second code path. The classic flattering
  backtest is one that silently assumed zero commission; omitting a cost here
  refuses.
- **`--contract` is a file** for the same reason `order submit --decision` is:
  nothing in this repo can produce an `InstrumentContract` without a live
  MetaTrader 5 terminal, and a research command that needs one cannot be
  replayed. The facts come from a vetted file, never from flags.
- **Bad input remains typed at the boundary.** The five run-level refusals print
  `{"status": "error", "detail": "backtest refused", "refusal": "<kind>"}` on
  stderr. Non-finite or excessively large money values, non-positive equity, an
  invalid tolerance, a contract outside the forced EURUSD scope, an unreadable or
  malformed contract, a schema-invalid contract or cost, and an unknown strategy
  all leave through the command's stable configuration error with key-sorted
  JSON and no correlation id. The tolerance parser rejects `NaN`, `Infinity`,
  values below zero, and values above one as `Decimal` input before the run.
  The `backtest run` command normalizes a missing or invalid `--exit-policy` at
  its parser boundary to the same fixed JSON error without echoing the supplied
  value; unrelated commands retain their normal parser behavior. Typer rejects
  numeric options below one. No boundary failure carries free text from a DSN,
  path, or broker message.

## Phase 8A — Canonical evidence and trial ledger

Phase 8A makes one promise: **every trial the foundry runs is recorded before it
runs, its evidence is sealed to a digest, and neither can be edited afterwards.**
It is the bookkeeping half of Phase 8. It implements no validation, no
statistics and no promotion, and the last section below says exactly what that
leaves undone.

### Two databases, and why

| Database | Role |
|---|---|
| `trading_house` | The application database: audit chain, intents, positions, bars, agent runs |
| `trading_house_research` | The trial ledger's deployment target: the database the ledger is *used* from. Carries the same full migrated schema as `trading_house`, the two ledger tables included — they are not absent from the application database, this phase only points the ledger at the second one |

The split is operational, not a security boundary. Both databases run the *same*
migration history, so `trading_house_research` contains the **full migrated
schema** — `audit`, `memory`, `research`, `marketdata`, `execution` and the rest —
and `trading_house_runtime` reaches both with the grants it has in the
application database. In particular that role *does* hold `INSERT` on
`research.trials`, `memory.agent_beliefs`, `marketdata.bars`,
`marketdata.ingest_runs`, `execution.intent_events` and
`execution.position_events` in the research database. What keeps the ledger
append-only is narrower than that: on the two ledger tables the runtime role
holds no write privilege at all. What the second database buys is that the
ledger can be backed up, restored, migrated or dropped on its own schedule, and
that no migration of the application schema can disturb it.
`docker compose` creates it from `TRADING_HOUSE_RESEARCH_DATABASE` (default
`trading_house_research`) on a fresh cluster only; remove the volume and
re-initialise if you change it later. `init-roles.sh` reads that one variable for
the `CREATE DATABASE` *and* for every grant and `ALTER DATABASE` against it, so
there is no name in the script that can disagree with the database it creates.

**Restoring that database from a backup rewinds the ledger, and nothing detects
it.** A backup taken before a trial ran restores a chain that verifies clean,
holds fewer events, and is indistinguishable from a trial that never ran — the
hash chain proves the rows it holds were not rewritten, and it says nothing about
rows that were never restored. So restore on a schedule you can defend, and treat
`checked_events` from `research trial verify` and the age of the backup you
restored as the only two signals an operator has. Anything that can write to the
database as its owner (`pg_dump`, a restore, a replica) can remove trials from
the record without leaving a break behind; the second database gives that write
its own name and its own schedule, which is a smaller blast radius, not a
guarantee.

**No application code writes `research.trials`.** That is the legacy Phase 0.5
table, and the live public `Trial` and `deflation_trial_count` models describe
it — they are not deprecated, and nothing under `src/` writes the table. What
changed is the *role*, not the models: the ledger computes its own deflation
denominators with `trial_counters(events)`, counting from the chain, and
`research.trial_ledger_events` is what a trial is now recorded in.

The table is still migrated, and the runtime role still holds the `INSERT` on it
that `0002` granted — inherited, not granted by this phase. No test asserts that
privilege today: the memory-migration suite's append-only test connects as the
migrator and does `SET ROLE trading_house_owner` to put a row in place so the
row-level trigger has something to fire on, then rolls back. Other privileges
*are* asserted against the migration that grants them —
`audit.ledger` in `tests/integration/database/test_privileges.py`,
`marketdata.bars` and `marketdata.ingest_runs` in
`tests/integration/marketdata/test_migration.py`, and the two ledger tables in
`tests/integration/research/test_trial_ledger_store.py` — so the separation
above is stated per table, and `research.trials` is one whose runtime privileges
are read off the migration file rather than checked against it.

### Configuration

| Variable | Meaning |
|---|---|
| `TRADING_HOUSE_RESEARCH_LEDGER_DSN` | The trial ledger's connection string. **Optional** in `RuntimeSettings` — only a `research trial` command needs it, so `order submit` and `guard run` start on a host that has never run a trial. Its absence is a `configuration invalid` (exit 2) at the single boundary that cannot work without it, and so is a DSN naming the *same* database as `TRADING_HOUSE_DATABASE_DSN`. |
| `TRADING_HOUSE_EVIDENCE_ROOT` | Where sealed evidence bundles live. Content-addressed: `<root>/<first two hex>/<digest>.json`. Defaults to `.local/evidence`, which is gitignored. |

Neither is read from `.env`; export them into your shell like the rest of
`TRADING_HOUSE_*`. See `.env.example` for both.

**The two DSNs must name two different databases.** That is the whole point of the
split, and it is the one property of it that no downstream check can see: a ledger
sitting in `trading_house` passes every other test in this section, `verify`
answers `valid`, and every trial command reports success. So the comparison is
made where the two DSNs are composed, before a connection is opened — the
`dbname` is parsed out of both with psycopg's own conninfo parser (so every
spelling the driver accepts compares the same) and an equal pair is refused as
`configuration invalid`. Neither DSN appears in the refusal. A DSN that cannot be
parsed, or that omits `dbname`, is passed through rather than refused on a
comparison nobody can make: the driver rejects the first and the second is a
deployment whose ledger database is a guess.

### What is stored, and what it means

An **evidence bundle** is one attempt's complete evidence: the backtest result it
came from, the return series and cost attribution derived from that result, and
the provenance that says when and under which registration state it was
produced. It is serialised canonically — sorted keys, compact separators, no
NaN — and addressed by a domain-separated SHA-256, so the digest is **not** a
bare `sha256sum` of the file. The store is a CAS with no index: the path *is* the
index, so a copied or rsynced root is still verifiable with no repair step.

A **ledger event** is one fact about one trial, appended to a PostgreSQL hash
chain under a second, different domain separator from the audit chain
(`trading-house:trial-ledger:v1` against `trading-house:audit:v1`), so a digest
from one can never be replayed into the other.

### Append-only, and fail-closed

- **The runtime role holds no write privilege at all on the two ledger tables** —
  no `INSERT`, `UPDATE`, `DELETE` or `TRUNCATE` on
  `research.trial_ledger_events` or `research.trial_ledger_heads`, in either
  database. The chain advances only through `research.append_trial_ledger_event`,
  which is `SECURITY DEFINER` with `search_path` pinned to `pg_catalog` and
  computes the event hash server-side from the sequence and previous hash it
  holds under an advisory lock. Triggers reject row mutation and truncation, and
  the owner role is `NOLOGIN`. Scoped to those two tables on purpose: the
  research database runs the full migration history, so the same role *does*
  hold `INSERT` on `research.trials`, `memory.agent_beliefs`,
  `marketdata.bars`, `marketdata.ingest_runs`, `execution.intent_events` and
  `execution.position_events` there. No code in Phase 8A writes any of them, and
  no code in this repository writes `research.trials` at all.
- **Every trial command refuses if the ledger database is not at Alembic head**
  (exit 5), *before* the first evidence byte is written. A database that has
  never had the ledger tables cannot record the seal either, so writing first
  would leave a document behind for a command that then refuses to name it.
- **An event against an undeclared trial is refused** (exit 15). `EXECUTION_STARTED`,
  `RESULT_RECORDED`, `FAILED` and `EVIDENCE_SEALED` are admissible only against a
  trial some `preregistered` protocol declared, or an explicit legacy import.
  Containment, not a column: a protocol seals its whole candidate family inside
  one event. `start` is in that set on purpose — an execution nobody declared is
  still a draw from the search space, and admitting one would let the deflation
  denominator be widened by whoever cares to. `record` asks the same question
  *before* it writes the evidence, so a trial nobody declared leaves an empty
  evidence root rather than a sealed document no ledger row points at; the
  store's append check is still what enforces it.
- **A protocol with no candidate family is refused.** No candidates is not "one
  candidate whose values are unknown" — a trial that is not declared cannot be
  counted, and that is the denominator every later statistic divides by.
- **A lost race is told apart from a lost write.** The caller submits the head it
  chained onto; if that head has moved the database raises 40001 and the caller
  retries the *same* event. Silently appending onto a head the caller never saw
  would let it believe its event sits where it does not. An event id is an
  identity, not a receipt, so a genuine retry is idempotent rather than a
  spurious conflict.
- **An existing digest is never overwritten with different bytes.** The publish
  is an `os.link`, which is the only publish that is atomic *and* refuses to
  clobber, so the check and the publish are one step. Two writers producing the
  same bytes both succeed; two producing different documents at one digest is a
  conflict, and it raises rather than picking a winner.
- **Verification answers with a reason; being unable to verify is not an
  answer.** A detected chain break is reported as `{"valid": false, "reason":
  …}` with exit 16. A ledger that could not be *read* raises exit 16 as well, so
  a script cannot mistake "nobody could check" for "nothing is wrong".
  `checked_events` says how many rows were good before the failure — the count a
  restore-from-older-backup leaves small, with `valid: true` and no reason at all.
  The failure report goes to **stdout**, not stderr, so it can be piped into a
  reporter; the exit code, not the stream, is the authoritative signal for a
  script.
- **The chain is anchored at its genesis.** `verify` reports
  `genesis_sequence_mismatch` if the first row is not sequence 1. The genesis
  row's previous hash is the one link the verifier supplies rather than reads off
  the row before it, and the database's CHECK pins 32 zero bytes to sequence 1
  alone, so a chain whose first row was renumbered satisfies every hash check
  while claiming a genesis it never had. Sequence numbers are still free to skip
  everywhere else: a burned number is a gap, not a break.
- **Absences are recorded as absences, by the code that builds them.** The
  importer writes no dataset hash, `PARTIAL` cost attribution with spread and
  slippage left `null` rather than zero, and a `REALIZED_CLOSED_TRADES` series
  over UTC calendar days in which a day with no closed trade carries a return of
  exactly zero rather than no point at all — a calendar-day series has no way to
  leave a day out. Stated that way because the *models* are not what
  enforces it: `dataset_sha256`, `spread_cost` and `slippage_cost` are optional
  fields a caller could fill with a plausible zero, and `ReturnSeriesBasis` still
  carries `MARK_TO_MARKET` for the Phase 8 statistics that will need it. What this
  phase guarantees is the shape it writes and the fact that it records which
  shape a reader is holding.

### The Phase 7 runs, imported as legacy evidence

The three completed Phase 7 arms are not preregistrations, and importing them
does not pretend otherwise. Each becomes a `LEGACY_IMPORTED` event flagged
`legacy`, with `RegistrationState.LEGACY_UNPREGISTERED` and
`HoldoutState.CONTAMINATED` in its provenance — never a synthesised
`PREREGISTERED` event, and `registered_at` is the operator's import clock rather
than the run's own timestamps, because a date typed in after the fact cannot
order anything.

| Arm | Result digest | Status |
|---|---|---|
| `none` | `a10b0fa43fc4ddb0185b3a376caab7c9369ec49e3e347d13171d31ab989f022b` | `LEGACY_UNPREGISTERED` |
| `fixed_target` | `fe5efadef7f5ea2448957e0bec507f2133755df676ade140b05a120cfb5fbc6b` | `LEGACY_UNPREGISTERED` |
| `chandelier` | `69867de09ef3993e5aabca78b225b21bb1ef82a4aa4fbcd63a9fd52dfe2e0c54` | `LEGACY_UNPREGISTERED` |

All three lost money after costs. The import is idempotent by source result
digest — a second import adds no trial, attempt or selection candidate, and
answers with the digest *the chain recorded* rather than the one that run would
have written. The artifact's own `digest` field is re-derived from the result it
carries before the file is read at all, so a result edited after the run still
carries 64 hex characters and is still refused.

**A legacy import is a `TrialSpec`, not a `TrialProtocol`.** The ledger's
`register` seals a whole protocol and digests that, so its `spec_sha256` covers
the data, execution, cost, validation and holdout specs too; a Phase 7 artifact
has no such envelope, so its digest covers the one candidate's declaration and
nothing else. The two digests are different kinds of address and must never be
compared as if they were the same.

### Commands

Seven commands, all under `research trial`. Every one of them is read-only on the
filesystem except `record` and `import-legacy`, which write evidence.

```bash
# Seal a frozen protocol and its whole candidate family -- one event, not one per candidate.
uv run trading-house research trial register --protocol protocol.json

# Record that one execution of a declared trial began. This is the event the three
# deflation denominators are counted from, so a trial run without it counts as
# though it never ran. --spec-sha256 is the digest of the preregistered
# specification being run, as the trial declares it. --started-at is that run's
# declared start time; re-running with the same value is recognised and appends
# nothing, so it is only needed when a run crashed mid-start and it is not
# knowable whether its event landed.
uv run trading-house research trial start \
  --trial-id trial-1 --attempt-id attempt-1 --spec-sha256 1a2b3c... \
  --started-at 2026-03-01T12:00:00

# Seal one attempt's evidence and record that it was written.
uv run trading-house research trial record \
  --trial-id trial-1 --attempt-id attempt-1 --evidence bundle.json

# Import one preserved Phase 7 result. An ordinary retry is recognised by the
# source result digest and writes nothing; pass a previous run's
# --registered-at only when that run crashed between writing the bundle and
# appending the event, so the retry derives the same digest.
uv run trading-house research trial import-legacy \
  --artifact none.json --registered-at 2026-03-01T12:00:00

# Replay one trial's chain rows. A trial nobody declared is an empty list, not
# an error: the question is what the chain holds for that id. A preregistration
# is not one of those rows -- it is scoped to the protocol event and carries no
# trial id -- so "was this trial ever declared" is answered by reading the
# protocol's preregistered event and the candidate family sealed inside it.
uv run trading-house research trial show --trial-id trial-1

# The three deflation denominators, counted from the chain rather than kept.
uv run trading-house research trial count

# Re-derive every hash, then re-read every file the chain points at.
uv run trading-house research trial verify
```

- **`register`** takes a frozen `TrialProtocol` JSON document and writes one
  `preregistered` event whose id is derived from the protocol's canonical
  digest, so re-running it on the same file is a recognised retry rather than a
  second registration. `occurred_at` is the end of the declared data window, not
  a wall clock, for the same reason.
- **`start`** appends the one event the denominators move on, against a trial a
  `preregistered` protocol declared or an explicit legacy import declared. Its
  `occurred_at` is the operator's own start time, taken from `--started-at` when it
  is given and from the command's own clock when it is not. Either way it is
  declared provenance: a start has no bundle, so there is nothing to recover a
  time from, and borrowing another document's would put a time on the row that no
  artefact supports. It is not proof
  of order — the database row's `recorded_at` is the only registration-order
  authority, and nothing in the chain can observe when a backtest actually began.
  That time is part of the event's canonical bytes, so a retried `start` reuses
  the same `--started-at`, is recognised under the same event id, and appends
  nothing. A retry under a *different* value is a different body under an id the
  chain already holds, and is refused with exit 15 as it should be.
- **`record`** takes an `EvidenceBundle` JSON document. The bundle is the single
  source of identity, so `--trial-id` and `--attempt-id` are *checked against it*
  rather than trusted, and the trial's declaration is checked against the ledger
  before anything is written. Evidence is written before the events are appended:
  a failed append then leaves a document nothing points at rather than a ledger
  row whose document is missing.
- **`count`** reports three numbers, not one: audit attempts, selection
  lotteries, and effective specifications. A repeated execution raises the audit
  count without being a new lottery, and that distinction is why they are three.

### What Phase 8A does not implement

**`effective_specifications` is a count of supplied digests, not a verified
match.** It is a `len(set())` over the `spec_sha256` each started attempt carries,
and nothing in this phase compares that digest against a preregistered
declaration — `ExecutionStartedPayload` does not carry one, and a protocol seals
its whole candidate family inside a single event, so there is no per-candidate
digest in the chain to compare against. The ledger therefore *preserves* the
digest and cannot *vouch* for it, and `start`'s `--spec-sha256` is operator-
supplied for that reason. Read the third number as "how many distinct
specification digests this operator claimed", not as "how many distinct
specifications were tried".

**It imports evidence; it does not analyse it.** There is no walk-forward
analysis, no Deflated Sharpe Ratio, no Probability of Backtest Overfitting, no
combinatorially-purged cross-validation, and no promotion gate. The
`ValidationSpec` a protocol must declare records those *parameters* — fold
counts, purge and embargo hours, bootstrap replicates — and nothing in this
repository consumes them yet. `StrategyPackage.stage` exists and is
unreachable; `GATE_DECIDED` is a payload type no command emits.

The three deflation denominators are counted and readable today, which is the
part everything later divides by. Everything that divides by them lands in a
later phase, and until it does, no strategy in this repository can pass a
promotion gate because there is no promotion gate.

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
| `trading-house backtest run` | Replay one fixed EURUSD M15 strategy arm over stored bars and print the result and its digest |
| `trading-house research trial register` | Seal a frozen trial protocol and its whole candidate family as one event |
| `trading-house research trial start` | Record that one execution of a declared trial began, as the event the deflation denominators count from |
| `trading-house research trial record` | Seal one attempt's evidence bundle to its digest and record the seal |
| `trading-house research trial import-legacy` | Import a preserved Phase 7 result as `LEGACY_UNPREGISTERED` evidence, idempotently |
| `trading-house research trial show` | Replay one trial's chain rows and the evidence they reference |
| `trading-house research trial count` | Report audit attempts, selection lotteries and effective specifications |
| `trading-house research trial verify` | Re-derive the chain hashes, then re-read every sealed bundle |
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
| 2 | `configuration invalid` | Missing `TRADING_HOUSE_DATABASE_DSN`, a `research trial` command with no `TRADING_HOUSE_RESEARCH_LEDGER_DSN` or one naming the same database, unreadable or malformed constitution YAML | Export the DSN; for the ledger, point it at the second database; confirm the YAML parses and its limits are in range |
| 3 | `signature verification failed` | Signature, public key, or constitution bytes do not agree | Restore the committed trio, or re-sign offline. **Do not edit the YAML to make it load** |
| 4 | `database connection failed` | PostgreSQL unreachable, or the session refused UTC | `docker compose up -d`; check the DSN host, port and credentials |
| 5 | `migration revision mismatch` | Schema is not at the exact expected revision | Run `alembic upgrade head` as the migrator. Never migrate from the runtime process |
| 6 | `audit integrity verification failed` | The hash chain does not recompute | **Stop.** Do not append. Preserve the database and investigate; a mismatch means the ledger was altered outside the append function |
| 7 | `audit append failed` | The append transaction did not complete | Check connectivity and privileges; the ledger is unchanged because appends are transactional |
| 8 | `broker terminal unavailable` | MetaTrader 5 is not running, will not initialise, or the server-clock probe read a stale tick from a closed market | Start the terminal and log in. During the venue step of `health` this degrades to "not performed" rather than failing the gate |
| 9 | `refusing to operate a non-demo account` | The terminal reports a real or contest account | **Stop.** Log into a demo account. This never degrades to a skipped step — it is the one venue failure that fails the gate |
| 15 | `trial ledger append failed` | A trial ledger event could not be appended — a lost race past its retry budget, a duplicate event id with different content, an event against a trial no protocol declared, or an unreachable ledger database | **Do not record the trial as run.** The append is transactional, so the chain is unchanged. A retried *identical* event is safe; a new event id for the same fact is a caller bug, not a race, and a `start` re-run under a *different* `--started-at` lands here too, with the attempt already in the chain — re-run it with the first run's value |
| 16 | `trial ledger integrity verification failed` | `research trial verify` found a broken chain (`{"valid": false, "reason": …}` names which), **or** could not read the ledger to check at all | **Stop appending.** A detected break means a row was altered outside the append function — preserve the database and investigate. The unreadable case is a separate answer: nobody could check, which is not the same as nothing being wrong |
| 17 | `evidence integrity verification failed` | A sealed bundle is missing, altered, unparseable, or not the canonical bytes its digest names | **Stop.** Do not re-seal. The path or parse failure stays on the private cause for a log reader; back up the evidence root and re-derive from the ledger |
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

Never scope a run to `tests/property` alone: `test_schema_boundaries.py`'s
naive-datetime guard discovers canonical models by walking
`CanonicalModel.__subclasses__()`, which only sees a model once its module has
been imported, so `tests/property` run by itself can pass without ever
importing — and therefore never checking — a model that only `tests/unit`
imports; run `tests/unit` and `tests/property` together, as both commands
above already do by covering the whole `tests/` tree in one process.

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

LangGraph or any LLM SDK; web APIs and dashboards; and automatic migration at
startup. MetaTrader 5 is reachable from one module and serves a demo account
only — no path in this repository can reach a funded one.

Order submission, strategies, sizing, risk evaluation, market data ingest and
backtesting were all on this list and have all since shipped (Phases 1.5, 3, 4,
5 and 6). They are named here only so the list is not read as still excluding
them.

The absence is testable. `tests/acceptance/test_architecture.py` parses every
source module and fails on an import of `langgraph`, `openai`, `anthropic` or
`ccxt`, on an import of `MetaTrader5` from anywhere but
`brokers/mt5/terminal.py`, on private-key primitives outside
`constitution/signing.py`, and on any Alembic upgrade path in runtime code.
`tests/acceptance/test_phase1.py` fails if the string `order_send` appears in
any `src/` module other than `brokers/mt5/terminal.py` — and, guarding that
exemption, if it stops appearing in `terminal.py` — if `amend_protection` ever
closes a position on its own initiative, or if the gateway ever serves a
non-demo account. It no longer asserts that any adapter method refuses
unconditionally: Phases 4 and 5 filled in `submit`, `close` and
`amend_protection`, and that parametrized test was deleted rather than emptied.
