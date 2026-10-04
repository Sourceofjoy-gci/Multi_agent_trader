# Phase 10 — Portfolio-aware risk engine (design)

**Date:** 2026-10-04
**Status:** Implemented in this phase.
**Source:** `docs/reviews/2026-10-04-tech-stack-and-architecture-review.md` §3.1.

## 1. The problem

The signed constitution declares eleven portfolio-level limits. Before this phase, none of them was enforced by code that makes a decision:

| Field | Scope |
|---|---|
| `max_concurrent_positions` | book |
| `daily_loss_stop_pct` | book |
| `max_drawdown_halt_pct` | book |
| `max_gross_leverage` | book |
| `ScalpLimits.max_orders_per_minute` | book |
| `max_total_drawdown_halt_pct` | firm |
| `max_aggregate_open_risk_pct` | firm |
| `max_correlated_cluster_risk_pct` | firm |
| `max_single_instrument_risk_pct` | firm |
| `max_gross_leverage` | firm |
| `max_orders_per_minute` | firm |
| `max_consecutive_rejects` | firm |

(That is twelve rows, because `max_gross_leverage` appears at both levels.)

Each field was validated when the constitution loaded, and then read by nothing. `RiskEngine.evaluate` took one proposal plus market facts and had no view of what was already open, what had been lost today, or how fast orders were going out. `order submit` sent whatever decision file it was given. Ten decision files produced in a flat moment would have opened ten positions.

## 2. What this phase promises

1. **One pure gate, in one place.** `RiskEngine.evaluate` and `evaluate_for_execution` take a required `portfolio: PortfolioState`. Every limit in §1 is a deterministic check with its own `RejectionReason`. `checks_passed` lists every check that was ruled out, as before. There is no flag that switches the gate off: the only way to pass a check is to pass a state that clears it.
2. **The live order path re-checks before sending.** `order submit` builds a `PortfolioState` from the broker and the intent ledger, after the clean-ledger gate (I-20) and inside the submission lock. It then runs `RiskEngine.recheck_portfolio` against the decision it was handed. A refusal is exit 22, with the reasons named.
3. **Unknown is a refusal, not a zero.** The gate refuses rather than guessing in three cases:
   - an open position whose risk cannot be measured (no broker stop, or a symbol the signed binding does not name);
   - P&L the system cannot read;
   - an account equity it cannot read.

## 3. `PortfolioState`

All of it lives in `risk/portfolio.py`. It is pure data: `risk/` may still import only `core/` and `constitution/`.

| Field | Meaning |
|---|---|
| `exposures: tuple[OpenExposure, ...]` | Every open position whose risk could be measured |
| `unmeasured_positions: int` | Open positions whose risk could not be measured. Any non-zero value refuses every new order (`unmeasured_open_risk`) |
| `book_pnl: Mapping[BookId, BookPnl] \| None` | `None`, or a book missing from the mapping, refuses (`pnl_unavailable`) |
| `firm_drawdown: Decimal \| None` | `None` refuses (`pnl_unavailable`) |
| `order_stamps: tuple[OrderStamp, ...]` | Every submission the order-rate window must count |
| `consecutive_rejects: int` | `REJECTED` intents since the last `CONFIRMED` one |

`OpenExposure` has these fields:

- `book: BookId | None`. `None` means no book owns the position, such as a manual trade (magic 0) or a foreign magic number. It counts toward every firm-level limit and toward no book's.
- `instrument_id`
- `cluster_keys`
- `risk_money`: the loss from the current mark to the current stop, never negative.
- `notional_money`

`BookPnl` has three fields: `realized_today`, `unrealized`, and `drawdown` (never negative).

### 3.1 Definitions

All money is in account currency.

- **Book equity** is `firm_equity * capital_fraction`, the same definition sizing already uses. `firm_equity` is the number the caller passes to the engine. It is never re-derived inside the state.
- **Notional** of `q` lots at price `p` is `q * (p / price_increment) * value_per_price_increment`. For a 1-lot EURUSD at 1.10000 that is 110,000 USD.
- **Open risk** is the loss if the stop is hit from the current mark, floored at zero. Measuring from the mark rather than from entry avoids counting an unrealised loss twice: it is already inside `unrealized`.
- **Today's loss** is `max(0, -(realized_today + min(unrealized, 0)))`. Unrealised gains never offset a realised loss. A position carrying yesterday's profit cannot mask today's losses.
- **Day** means the UTC calendar day. It is fixed and stated, not a broker's rollover.
- **Drawdown** is peak minus current on the book's cumulative closed-trade curve, plus current unrealised, floored at zero. The peak is the closed-equity peak (starting at zero). Intrabar unrealised highs are not tracked.

### 3.2 Correlation clusters

There is no signed cluster table. Re-signing the constitution needs the offline key, and cluster membership is a classification, not a limit. So clusters come from the contract, deterministically:

- **fx and metal** instruments: one key per currency leg, `ccy:EUR` and `ccy:USD` for EURUSD, `ccy:XAU` and `ccy:USD` for XAUUSD. This is the spec's "USD risk" cluster (§7.1).
- **every other asset class:** one key per class, for example `class:equity_cfd` (the spec's "index beta").

The cluster check runs once per key the candidate carries. Risk is summed regardless of direction: long EURUSD and long USDJPY both add to `ccy:USD`. That is conservative on purpose. Netting would credit a hedge that can come apart exactly when it matters.

## 4. The gates

Each limit is checked so that the candidate cannot be the order that breaches it. A limit holds at equality (`<=`), the same convention as I-19.

| Reason | Check |
|---|---|
| `unmeasured_open_risk` | `unmeasured_positions == 0` |
| `pnl_unavailable` | P&L known for the candidate's book, and firm drawdown known |
| `consecutive_rejects_exceeded` | `consecutive_rejects < firm.max_consecutive_rejects` |
| `order_rate_exceeded` | Stamps in `(now - 60s, now]` `< firm.max_orders_per_minute`; for a scalp book, that book's own stamps `< limits.max_orders_per_minute` too |
| `max_concurrent_positions` | Open exposures owned by the book `< max_concurrent_positions` |
| `daily_loss_stop` | `today's loss + book open risk + candidate risk <= daily_loss_stop_pct% of book equity` |
| `book_drawdown_halt` | `book drawdown + book open risk + candidate risk <= max_drawdown_halt_pct% of book equity` |
| `firm_drawdown_halt` | `firm drawdown + firm open risk + candidate risk <= max_total_drawdown_halt_pct% of firm equity` |
| `aggregate_open_risk` | `firm open risk + candidate risk <= max_aggregate_open_risk_pct% of firm equity` |
| `single_instrument_risk` | `open risk on the instrument (all books) + candidate risk <= max_single_instrument_risk_pct%` |
| `correlated_cluster_risk` | For each candidate key: `open risk sharing the key + candidate risk <= max_correlated_cluster_risk_pct%` |
| `book_gross_leverage` | `(book notional + candidate notional) / book equity <= book max_gross_leverage` |
| `firm_gross_leverage` | `(firm notional + candidate notional) / firm equity <= firm max_gross_leverage` |

Why the daily and drawdown halts include open risk: the constitution's own validator already requires `max_concurrent_positions * risk_per_trade_pct <= daily_loss_stop_pct`, so that a correlated cluster is "arrested by the stop, not breaching it". Comparing only realised loss to the stop would let three fresh positions take a book that has already lost 2% through a 2.5% stop. Counting worst-case open risk is what makes each halt a ceiling rather than a trigger that fires after the damage is done.

The first six checks need only the state, so they join the existing independent-gate batch. The rest need the candidate's risk and notional, so they run after sizing, on the volume actually approved. A breach **rejects**; it does not resize down to the remaining headroom. Resizing to fit is a sizing policy and a later decision.

## 5. Where each state comes from

### 5.1 Backtester: an explicit flat portfolio

`Backtester` passes `PortfolioState.flat(constitution.books)`: no exposures, zero P&L, zero drawdown, no stamps, no rejects. This is a decision, not an omission:

- The simulator takes decisions only when flat (D-7), so concurrency, aggregate, instrument, cluster and leverage are already empty in fact.
- **Halts would truncate the evidence, and truncation flatters a loser.** A drawdown halt "requires human unlock", and there is no human in a replay. A strategy that lost 25,000 would stop at the first breach and report a smaller loss than its rule produces. The Phase 7 chandelier arm is exactly this case. Research measures the rule; the halts are an operational overlay on it.
- Every result digest, run id and sealed bundle produced so far stays byte-identical.

The gap this leaves is named, not hidden. Gate 7 rejects a firm-equity drawdown of 10% or more. A book halt binds earlier: `fx_swing`'s 10% of a 0.45 slice is 4.5% of firm equity. Making gate 7 book-relative is a new validation policy and therefore a new trial; it belongs to whichever phase next revises `research/validation/policy.py`.

### 5.2 Live: `ops/portfolio.py`

`build_live_portfolio` composes:

- **Positions** (`adapter.positions_now()`): the book comes from the signed binding's magic ranges. The instrument comes from the binding's server-symbol map. The contract comes from `describe_instrument`. The mark is the broker's own `price_current`. A position with no stop, or on an unbound symbol, is unmeasured.
- **P&L:**
  - realised P&L per book from `deals_since(2000-01-01)`, where each deal's money is profit + commission + swap + fee;
  - today's slice is the deals at or after UTC midnight;
  - unrealised is each position's profit + swap;
  - firm drawdown uses the same curve over every system-owned deal.

  If any read returns `None` ("could not see"), the field is `None` and the gate refuses.
- **Order stamps** (`IntentLedger.submissions_since(now - 60s)`): `SUBMITTING` rows, with their book.
- **Rejects** (`IntentLedger.consecutive_rejects()`): `REJECTED` rows after the last `CONFIRMED` row.
- **Firm equity** (`adapter.account_equity()`): `None` refuses.

`recheck_portfolio` is used because `order submit` receives a decision, not a proposal. It needs a risk and a notional for the decision as it would execute now. Risk is the larger of the decision's own `risk_money` and the loss from the current quote (ask for a buy, bid for a sell) to the decision's stop. A quote that drifted toward the stop cannot lower the risk the decision was sized for. Notional is quantity at the same quote.

### 5.3 Broker surface added

These fields and methods are needed, and nothing else:

- `Mt5Deal`: `profit`, `commission`, `swap` and `fee`. MT5 reports these as separate deal fields.
- `Mt5Position`: `price_current`, `profit` and `swap`.
- `TerminalPort.account_equity()`.
- `DealRecord.net_money`, plus `PositionRecord.current_price` and `unrealized_money`.
- `Mt5BrokerAdapter.account_equity()`.

## 6. What this phase does not do

- **No latching.** A halt holds while its arithmetic says so. A book with no open positions can only recover drawdown by trading, which the halt forbids, so in practice it latches. A position still open can still pull it back under the line. A latched, human-unlocked halt is SAFE_MODE's job (review §3.2).
- **No swing overnight or weekend limits** (`max_overnight_positions`, `max_weekend_exposure_pct`, `gap_risk_multiple`). These need a calendar of when the position will be held, which this phase does not model. They stay declared and unenforced, and are listed here so that nobody reads the table in §4 as complete.
- **No resize-to-fit** (§4).
- **No deposit or withdrawal accounting.** Drawdown is built from system-owned deals only. Balance operations and manual trades' P&L are outside it. A manual trade's *open risk* is inside every firm limit.
- **The automated runner is not built.** `order submit` is the only live caller today. The strategy runner (review §3.3) will call `evaluate_for_execution` with the same live builder.

## 7. Tests

- **Unit:** one test per reason, both sides of each boundary, plus the unknown-state refusals and the flat state passing everything.
- **Property:** for any generated portfolio and any approved decision:
  - book open risk plus today's loss stays within the daily stop;
  - firm open risk stays within the aggregate limit;
  - every cluster stays within its limit.
- **Backtester:** every existing known-answer and digest test runs unchanged.
- **Live builder:** fake adapter and fake ledger. Unmeasured, unknown-P&L and unknown-equity cases refuse; book attribution by magic range; today's slice at UTC midnight.
- **CLI:** `order submit` refuses with exit 22 and names the reasons, before any intent is written.
