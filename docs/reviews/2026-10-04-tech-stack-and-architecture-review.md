# Technology Stack and Architecture Review

**Date:** 2026-10-04
**Scope:** The whole repository at `8572731` (Phase 9). It covers the stack, the architecture, the agent layer, and what stands between this codebase and a system that makes money after costs.
**Method:** I read the master spec, both research reports, the README, and every package under `src/`. I checked each claim below against the code, not only the docs. I ran `uv run pytest -m "not integration" --no-cov`: 2,599 passed, 9 skipped.

---

## 1. Bottom line

The codebase is very well built on safety, reproducibility, and statistical honesty. The trial ledger, the hash-chained audit, the signed constitution, the DSR/PBO/CPCV gates, and point-in-time reads are all stronger than what most professional shops run. **None of this is the bottleneck to profit.** The bottlenecks are:

1. **It has no edge, and it has very little capacity to search for one.** One strategy has been tested on real data, Session Momentum, and it lost money in all three arms. The second, Vol Breakout, has never been run. About 10,400 source lines go to research validation and ops. About 1,000 go to strategies and features. The machine is built to *reject* hypotheses rigorously, but it cannot *generate and screen* them at a useful rate.
2. **The risk engine has no portfolio view.** Most limits in the signed constitution are declared and validated at load, but **no code enforces them** at decision time. That covers the daily loss stop, the drawdown halt, max concurrent positions, aggregate, cluster and instrument risk, orders per minute, and consecutive rejects (§3.1). It is safe today only because nothing trades automatically.
3. **There is no autonomous trading loop.** `order submit` takes a hand-supplied decision JSON. Nothing runs a strategy against live bars, so there is no paper or shadow mode. That means no live evidence can ever be collected, and live evidence is the only thing that should ever justify capital.
4. **Promotion is structurally impossible.** Gate 9 (capacity) is `UNAVAILABLE` for every candidate, and no holdout has been collected. Even a genuine edge would come out `REJECTED`.
5. **The agent layer does not exist.** `agents/` holds a 158-line provider protocol. CI bans every LLM SDK from `src/`. This matches the spec's roadmap ("do not start at the agent phase"). But the highest-value use of agents here is the research plane, which is exactly where point 1 needs help.

No technology can guarantee profit, and the repo's own spec says so correctly (§1.2: real-world base rates are 70–97% of retail traders losing). What the right technology *can* do is three things. It can raise the number of honest, cost-aware hypotheses tested per week. It can make sure the ones that survive are sized and stopped by a risk engine that actually sees the portfolio. And it can make sure capital follows live evidence rather than backtests. The recommendations below are ordered by that logic.

---

## 2. What is already good (keep it)

| Area | Why it matters for profit |
|---|---|
| Signed, load-once risk constitution (Ed25519) | No agent or operator can widen a limit at runtime (I-2) |
| Intent ledger written before send, reconcile instead of resend (I-6, I-20) | Prevents the double-position failure MT5 is notorious for |
| Position guard with restore-or-escalate (I-21) | Protects against the "stop vanished" tail loss |
| Point-in-time reads on `availability_time` (I-17) and fixed feature windows (I-18) | Removes the most common source of fake backtest edge |
| Risk engine shared by the backtester and the live path | A measured edge belongs to the strategy, not to a second copy of the sizing code |
| Preregistered trial ledger, DSR/PBO/CPCV, cost stress at 1.5x and 2x, one-time holdout | This is what stops you deploying an overfit strategy |
| Tooling: `uv` with lock check, ruff, mypy strict, Hypothesis, testcontainers, 95% coverage gate | A modern, fast, reproducible Python toolchain |

These should stay as they are. Every recommendation below **feeds into** these gates rather than around them.

---

## 3. Findings and recommendations

Priorities: **P0** blocks safe paper or live trading. **P1** is the main lever on profitability. **P2** is the agentic layer. **P3** is engineering hygiene.

### 3.1 [P0] Give the risk engine a portfolio view and enforce the whole constitution

**Finding.** `grep` over `src/` finds the following fields only in `constitution/models.py`, never in a decision path:

- `daily_loss_stop_pct`
- `max_drawdown_halt_pct`
- `max_concurrent_positions`
- `max_aggregate_open_risk_pct`
- `max_correlated_cluster_risk_pct`
- `max_single_instrument_risk_pct`
- `max_orders_per_minute`
- `max_consecutive_rejects`

`RiskEngine.evaluate()` takes one proposal and market facts, and no open-position or P&L state. The README admits this for concurrency (Phase 6), but the gap is wider than concurrency.

**Recommendation.**

- Add a `PortfolioState` port: open positions with their risk-to-stop, realised P&L today per book, the equity high-water mark per book and firm-wide, recent order timestamps, and the reject streak.
- Build it from the intent and position ledgers, which already exist, and pass it into both `evaluate` and `evaluate_for_execution`.
  - **Live:** the port reads Postgres.
  - **Backtest:** a simulated implementation, so backtests also honour the halts. This matters, because a strategy that only looks good while ignoring its own drawdown halt is not the strategy that will run.
- Enforce each limit as a deterministic check with its own `RejectionReason`. Add property tests in the style of `tests/property/test_risk.py`, for example: "no sequence of approved decisions exceeds aggregate open risk".
- Add correlation clusters (USD-bloc, JPY-crosses, metals, index beta) as signed config. Today `max_correlated_cluster_risk_pct` has nothing to sum over.

### 3.2 [P0] Build SAFE_MODE, kill switches and alerting

**Finding.** `RecoveryAction.ENTER_SAFE_MODE` exists only as an enum value. The README says plainly: "There is no alerting subsystem and no safe-mode state machine." Invariant I-4 ("…or the system is in SAFE_MODE") therefore cannot hold. An escalation is visible only to someone who runs `guard status`.

**Recommendation.**

- Add a persisted `SafeModeState`. Its transitions should be audit-ledger events. Entering is automatic; **exiting needs a human acknowledgement**. Wire in every trigger from spec §13.2 that already has data behind it: tick age, spread multiple, clock drift, reconciliation mismatch, guard escalation, and retcodes 10027/10031.
- Add the four kill levels from spec §13.1: strategy, instrument, book and firm. Use one CLI command each, plus a single file-or-DB flag that the hot loop checks every cycle.
- Push alerts to a channel a human actually sees: Telegram, ntfy or PagerDuty, behind a small `Alerter` port. Send at most one message per incident, never repeats.

### 3.3 [P0] Add a live strategy runner with paper and shadow modes

**Finding.** Nothing turns a closed live bar into a `FeatureSnapshot`, then a `Strategy.evaluate`, then `RiskEngine.evaluate_for_execution`, then `OrderManager.submit`. Spec roadmap phases 7 and 8 (paper house, then shadow) cannot start.

**Recommendation.**

- Add a `strategy run` daemon. It reuses `Backtester._snapshot`'s logic over `data update`'s stream of closed bars, so backtest and live share one code path for snapshots, just as they already share one for risk.
- It needs three modes:
  - `shadow`: log the would-be order plus the obtainable price.
  - `paper`: send orders to the demo account.
  - `live`: locked until a `LIVE` package exists.
- Add **TCA**: record modelled versus realised slippage per fill. That is the input that `slippage_breach_sigma` (already in the constitution) needs, and the evidence that should update the cost model.
- Write it as a single asyncio event loop with one injected clock, not a framework. The spec's "no agent framework in the hot path" is right.

### 3.4 [P0] Unblock promotion: declare a capacity model and collect a holdout

**Finding.** Gate 9 is `UNAVAILABLE` for every candidate, so `RESEARCH_PASSED` is unreachable by construction. No holdout window has been collected and locked.

**Recommendation.**

- **Capacity.** Ship a deliberately simple, conservative capacity model that a protocol can declare. For example, max lots = min(broker `volume_max`, k × median bar tick volume at the entry hour), with k declared up front. At retail size on EURUSD, capacity will not bind. The point is to make gate 9 *decidable*, not to make it pass.
- **Holdout.** Pick the holdout window now, before any further H1 research, and lock its digest with `research dataset digest`. Every month that goes by without one is a month of data that research may contaminate.

### 3.5 [P1] Fix the alpha pipeline: more hypotheses, cheaper screening, honest counting

**Finding.**

- The backtester is single-instrument, single-timeframe, one-position-at-a-time and bar-only.
- It builds Pydantic and `Decimal` snapshots per bar, and the Bollinger block recomputes 125 bandwidth windows on every bar (`features/engine.py:238` says so).
- That is correct as the evidence engine, but far too slow and narrow as a search engine.
- Each strategy takes a full design, plan and review cycle (about one phase) to reach its first real run.

**Recommendation.** Use a two-tier research stack:

- **Tier 1, the screener (new).**
  - Vectorised Polars/NumPy, or DuckDB, over Parquet exports of the bar store.
  - Cost-aware but approximate.
  - Multi-instrument and portfolio-level.
  - Thousands of variants per minute.
  - **Every variant screened is appended to the trial ledger as a counted trial.** This keeps the DSR denominator honest. Without it, a fast screener is just a faster way to overfit.
- **Tier 2, the existing event-driven engine.** It stays the only source of sealed evidence, run only on preregistered survivors.
- Extend Tier 2 to **multiple instruments and positions** behind the portfolio-aware risk engine from §3.1. The strategy families with the strongest published, retail-feasible evidence are portfolio strategies, not single-pair signals:
  - time-series momentum and trend across FX majors, metals and index CFDs (D1/H4);
  - FX carry, where MT5 swap *is* the carry;
  - volatility-targeted sizing.
  A diversified book of modest edges is how a retail system realistically compounds.
- Replace hardcoded strategy priors (`win_probability=0.45`, `expected_return_bps=20.0`) with **calibrated** estimates from Tier 2 evidence. The risk engine's `min_expected_edge_after_cost_bps` gate currently checks a number that the strategy asserts about itself.
- Consider **meta-labelling** (a classifier that filters a primary rule's signals) as the first ML component. It fits the existing calibration fields and keeps the rule as the auditable core.

### 3.6 [P1] Data: independent, deeper, DST-correct

**Finding.**

- All history comes from one demo broker (FBS). M1 depth is about 3 months and M5 about 16 months.
- The spec's "independent feed comparison" SAFE_MODE trigger has no second feed.
- Sessions are fixed in UTC on purpose, to keep tzdata out of the digest. As a result, Session Momentum's "London open" (07:00 UTC) is an hour early from late October to late March. The strategy traded pre-open liquidity for about half of every year. This doesn't make the dead hypothesis worth reviving under the same id, but any future session strategy will carry the same flaw.

**Recommendation.**

- Ingest a second, deeper source, for example Dukascopy tick and bar history (free, about 20 years of FX tick data). Uses:
  - cross-checking the broker's bars (the quality layer already grades bars);
  - unlocking M1/M5 research history;
  - eventually tick-resolution fills, which would let `MIN_HORIZON_BARS` be lowered.
- Make sessions DST-aware **and** reproducible. Pin the `tzdata` PyPI package in `uv.lock` and use `zoneinfo` against that pinned package, not the OS. The lock file already pins everything else that goes into the digest, so this needs no new rule, only one more pinned input.
- Optionally move bars to TimescaleDB, which is still Postgres and keeps the existing roles, triggers and migrations. Compression and time-bucketed queries help once tick data arrives.
- Revisit the broker choice per spec §17.2. A raw-spread or ECN account with an explicit commission usually beats a standard account's built-in spread for systematic strategies, and makes `commission_per_lot_per_side` a real number.

### 3.7 [P2] The agent layer: put agents where the bottleneck is

The spec's permission model is sound, and the existing `Plane`, `SandboxHandle`, `AgentRun` and `AgentBelief` models already encode it: agents never touch the hot path, never hold broker credentials, and their outputs are beliefs, not facts. The gap is that nothing runs. Recommended design:

**Where agents earn money here (in order):**

1. **Strategy research agent ("foundry").** It reads the spec catalogue, prior trial outcomes and the literature, and proposes a `StrategySpec` plus the code plus a `TrialProtocol`. It runs Tier 1 screens in a sandbox, and opens a PR with the preregistration for a human to approve. Every candidate it screens is a counted trial. It is scored on the rate of its proposals that later pass gates, not on backtest Sharpe.
2. **Red-team and validation agent.** It reviews each candidate and PR for look-ahead, survivorship, cost omissions and hidden leverage. This automates what the phase reviews ("F1–F9") already do by hand. It may only *block*, never approve.
3. **Event and news extractor.** It turns economic-calendar and headline text into schema-bound events. Their deterministic use is an **event blackout window** in the risk engine (spec §7.1, "event blackout window"). That is a direct loss-avoidance feature that needs no edge claim.
4. **Post-trade analyst.** It produces daily attribution from TCA, fills and features into `AgentBelief`s. It writes no facts.
5. **Ops summariser.** A read-only digest of audit-ledger escalations and SAFE_MODE events.

Leave regime classification deterministic (vol quantiles or an HMM over features). An LLM adds latency and nondeterminism there, and no evidence.

**Recommended technology (current as of this review):**

| Need | Recommendation |
|---|---|
| Reasoning, research, red-team | Claude Opus 5.5 (`claude-opus-5-5`) with adaptive thinking and `effort: high` |
| High-volume extraction (news, calendar) | Claude Haiku 4.5 (`claude-haiku-4-5`), or Sonnet 5.5 (`claude-sonnet-5-5`) where judgment is needed |
| Schema-bound output | Structured outputs (`client.messages.parse(...)` / `output_config.format`) validated straight into the existing Pydantic `CanonicalModel`s. Malformed output is rejected, never coerced (spec Phase 4 `test_agent_output_schema`) |
| Tool access | Expose the read-only surfaces as an **MCP server** with no write tools: data coverage, trial ledger `show`/`count`, `research dataset digest`, Tier 1 screener. The spec's `FORBIDDEN_FOR_ALL_LLM_ROLES` becomes "the tool does not exist", which is stronger than a deny-list |
| Agent harness | Claude Agent SDK, or the Anthropic API Tool Runner, in a **separate package/process** (e.g. `agents_runtime/`) so the CI ban on LLM imports in `src/trading_house` stays meaningful. Or Managed Agents with scheduled deployments for the nightly foundry, which gives a hosted sandbox with no route to the broker by construction |
| Overnight bulk work | Message Batches API (asynchronous, cheaper) for screening reviews and news backfills |
| Governance | Log every run as an `AgentRun` (already modelled: prompt and transcript hashes, tokens, cost) in the audit chain. Enforce `RunBudget`. Treat a `refusal` stop reason as a failed run, not empty output |

**Agent evaluation (spec §5.1), built from day one:**

- pairwise disagreement logging;
- outcome-based scoring against the trial ledger;
- periodic ablation: an agent that does not improve out-of-sample results is removed.

The `test_llm_blackout` (I-7) and `test_forbidden_tool_call` acceptance tests should land in the same phase.

### 3.8 [P3] Engineering and platform

| Item | Recommendation |
|---|---|
| Python pinned to `>=3.12,<3.13` | The pin exists for the `MetaTrader5` wheel. Isolate the gateway (`brokers/mt5/terminal.py` is already the only importer) behind a small gRPC or ZeroMQ service (spec topology B). That lets the rest of the stack move to a newer CPython, and decouples Linux research from the Windows terminal |
| `cli.py` is 2,866 lines | Split into `cli/{data,order,guard,backtest,research,package}.py` sub-apps. The behaviour and JSON contracts stay unchanged |
| README is 2,718 lines of phase history | Move phase narratives to `docs/phases/`. Keep the README as an operator manual (setup, commands, exit codes) |
| Observability | OpenTelemetry metrics, plus Prometheus and Grafana for the §13.4 scorecard: P&L, slippage versus model, retcode histogram, guard cycle latency, SAFE_MODE state, agent cost |
| Ledger rollback via backup restore (README admits it is undetectable) | Periodically anchor the audit and trial-ledger head hashes outside the database: a signed git tag, an object-lock bucket, or an RFC 3161 timestamp. A restore that rewinds past an anchor then becomes detectable |
| Backups | Scheduled `pgBackRest` or WAL archiving for both databases, with restore drills |
| CI | Add Dependabot or Renovate (actions are on `checkout@v4`/`setup-uv@v5`), `pip-audit`, and CodeQL. Add `pytest-xdist` to parallelise the 3,000+ tests |
| Process | Cap per-phase review overhead for **research** work. Infrastructure for the hot path deserves the current rigour; a Tier 1 screen does not need a design, plan and review cycle |

---

## 4. Proposed sequencing

| Step | Delivers | Exit criterion |
|---|---|---|
| A | §3.1 portfolio-aware risk engine and §3.2 SAFE_MODE, kill switches and alerts | Every constitution limit has an enforcing check and a property test; the I-4 path exists |
| B | §3.4 capacity model and locked holdout; run Vol Breakout through the Phase 8 sequence as already planned | Vol Breakout gets an honest verdict |
| C | §3.6 second data source and DST-pinned sessions; §3.5 Tier 1 screener with ledger counting | Hundreds of counted screens per week; survivors preregistered |
| D | §3.5 multi-instrument Tier 2 and a diversified trend/carry candidate | A portfolio candidate through the nine gates |
| E | §3.3 strategy runner in `shadow`, then `paper`, with TCA | 4 weeks clean (spec Phase 6 acceptance); slippage within the modelled distribution |
| F | §3.7 research-plane agents (foundry, red-team, event blackout) via MCP and structured outputs | Agent-originated candidates pass gates at a measurable rate; blackout and forbidden-tool tests pass |
| G | Canary with real capital (spec roadmap phase 9) | Only from a `LIVE` package with human capital authorisation |

Steps A and C can run in parallel. Steps B and E are where the first real evidence about profitability will come from. The agent layer (F) multiplies a pipeline that works; it does not replace one.

---

## 5. Things not to do

- Do not let any agent, or the faster screener, sidestep the trial ledger. Uncounted trials are how backtests lie.
- Do not tune Session Momentum under its old id. The DST finding belongs to a *new* preregistered hypothesis.
- Do not add an agent framework to the hot path, and do not let a model's confidence reach sizing (I-1, I-3).
- Do not raise constitution limits to "make it profitable". Size follows live evidence (spec §1.3).
