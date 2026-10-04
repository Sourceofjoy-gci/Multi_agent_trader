# Phase 11 — Safe mode, kill switches and alerting (design)

**Date:** 2026-10-04
**Status:** Implemented in this phase.
**Source:** `docs/reviews/2026-10-04-tech-stack-and-architecture-review.md` §3.2, plus the latching gap Phase 10 left (its design, §6).

## 1. The problem

Before this phase, safe mode existed only as `RecoveryAction.ENTER_SAFE_MODE`, a value nothing acted on. Three gaps followed from that:

- **I-4 could not hold.** The invariant reads "a protective stop, or the system is in SAFE_MODE", and there was no SAFE_MODE to be in.
- **No kill switch existed** at any of the four levels in master spec 13.1.
- **Nobody was told about anything.** An operator learned of a guard escalation by reading the audit ledger.

Phase 10 added two more:

- **Its drawdown halts did not latch.** The constitution says they "require human unlock".
- **Its reject-streak gate could never clear.** The streak only ends at a `CONFIRMED` intent, and while the streak refuses every order, no intent can be confirmed.

## 2. What this phase promises

1. **A halt is a row, and only a person removes it.** Every halt is entered by one row in `execution.control_events` and cleared by a second row naming its `halt_id`.
   - The table is append-only by grant and by trigger (migration 0009), like every other ledger here.
   - `(halt_id, action)` is unique, so a halt is entered once and cleared once.
   - Nothing in the system clears a halt on its own. The condition that entered it going away is not the same as somebody having looked.
2. **Three kinds, one gate.**
   - `SAFE_MODE` is firm-wide and entered by the system or by an operator.
   - `KILL` is an operator's switch on a strategy, an instrument, a book, or the firm.
   - `DRAWDOWN_HALT` latches on a book or the firm.

   `order submit` runs `require_not_halted` inside the submission lock, before the terminal is reached. A covering halt in force is exit 23, naming each halt by id and kind.
3. **Entering is idempotent per switch.** Asking for the same kind on the same thing while it is in force returns the existing halt, writes nothing, and alerts no one. The check and the insert share a transaction-scoped advisory lock, so concurrent requests still produce one halt; a test proves it with eight threads.
4. **A halt is in force before anyone is told, whatever happens to the telling.** Effects run in this order:
   1. The halt row commits.
   2. A `control.halt_entered` audit row is appended.
   3. The alert is sent.

   A failed alert is audited as `control.alert_undelivered` and never raised. With no channel configured, every halt records that nobody was told.
5. **The guard keeps guarding.** A guard escalation enters safe mode, but the guard is not gated by it and never was (D-6). Safe mode stops new orders; it never stops stops.

## 3. Triggers

| Trigger | Halt | Where |
|---|---|---|
| Any position-guard escalation: a stop restore failed twice, an orphan with no stop, an unreadable terminal, a crashed cycle | `SAFE_MODE` | `SafeModeEscalator` around the guard's `LedgerEscalator` |
| A venue refusal whose recovery is `ENTER_SAFE_MODE`. That is the AUTHORITY class in `core/venue.py`: trading disabled (including retcode 10027), account disabled, insufficient funds, or unclassified | `SAFE_MODE`, reason `venue_refused:<reason>` | `order submit`, after the send |
| `consecutive_rejects >= max_consecutive_rejects` | `SAFE_MODE`, reason `consecutive_rejects` | `order submit` and `control check` |
| A book's drawdown `>= max_drawdown_halt_pct` of its slice | `DRAWDOWN_HALT` on that book | same |
| The firm's drawdown `>= max_total_drawdown_halt_pct` | `DRAWDOWN_HALT` on the firm | same |
| An operator | `SAFE_MODE` or `KILL` | `control safe-mode`, `control kill` |

The drawdown and streak triggers live in `risk/halts.py` as a pure function of the Phase 10 `PortfolioState`. Unlike the Phase 10 gates, they compare what has already happened, not what one more order would add. A book one trade from its halt is refused that trade; a book at its halt is halted. Unknown P&L latches nothing, because the gate already refuses it, and a halt latched on a number nobody read would need a person to clear it for no reason.

Every guard escalation enters safe mode, including an unreadable terminal. The loop's own comments call an unreadable terminal an ordinary overnight state, so this costs an operator a clear after such a night. That cost is accepted: spec 13.2 lists the terminal being unreadable as a safe-mode trigger by name, and while the guard cannot read positions it cannot confirm that any stop is in place.

## 4. Clearing restarts the counts

Clearing a halt has to mean something.

- **A drawdown halt.** Its curve restarts from zero at the moment it was cleared. Otherwise the same drawdown would re-latch on the next read, and the book could never trade again.
- **Safe mode.** Clearing it restarts the reject streak: the ledger query counts `REJECTED` rows after the clear as well as after the last `CONFIRMED`.

`Baselines` carries these times from the control ledger into `build_live_portfolio`. Today's realised P&L does not restart, because a person clearing a halt does not change what was lost today.

## 5. Kill switches and flatten

`control kill --scope firm|book|instrument|strategy [--target T]`. The target must be something the system knows:

| Scope | Target must be in |
|---|---|
| Book | The signed constitution |
| Instrument | The signed venue binding |
| Strategy | The strategy registry |

A kill on a typo stops nothing and would report that it did. Operators are named. `system` is reserved, so a hand-entered halt cannot pass for an automatic one, nor a hand-cleared one look like nobody cleared it.

`control flatten` closes every position with a system magic number, and only while a firm `KILL` is in force. It is split from the kill itself, so stopping new orders (cheap and reversible) never closes positions by accident. A manual trade is left alone. Each close is one `control.position_flattened` audit row. A refused close is reported, not retried.

## 6. Alerting

`ops/alerts.py` posts to one webhook, configured by two settings:

| Setting | Values |
|---|---|
| `TRADING_HOUSE_ALERT_WEBHOOK_URL` | The webhook URL |
| `TRADING_HOUSE_ALERT_WEBHOOK_FORMAT` | `slack` (`{"text"}`), `discord` (`{"content"}`, cut to 2,000 characters), `ntfy` (plain text with an urgent priority for a halt), or `json` (the default) |

The URL is the channel's credential. It is a `SecretStr`, must be https, and appears in no output, error, or audit row; a delivery failure's cause is dropped for the same reason.

There are no retries. An alert goes out when a halt is entered (`critical`) and when one is cleared (`info`), never once per cycle.

## 7. Commands

| Command | Does |
|---|---|
| `control status` | Lists halts in force and the alert channel. Ungated and read-only |
| `control kill` | Enters a `KILL` |
| `control safe-mode` | Enters `SAFE_MODE` by hand |
| `control clear --halt-id` | The only way any halt ends. Exit 24 if it is not in force |
| `control check` | Reads the live account and latches anything it has earned. For a scheduler, so a book that crosses its halt between orders is halted, and a person told, when it happens |
| `control flatten` | As in §5 |

## 8. What this phase does not do

- **No automatic flatten.** Flattening is always an operator's second act.
- **No market-data triggers.** Tick age, spread multiple and clock drift remain per-order gates in the risk engine, not latched triggers: a stale tick refuses its order without halting the firm.
- **No slippage-breach trigger and no forbidden-tool-call trigger.** The first needs TCA and the second needs agents. Neither exists yet.
- **Only one alert channel**, and no escalation ladder or acknowledgement loop. The acknowledgement is `control clear`.
- **`health` does not fail readiness on a halt.** It is a startup check, and a halted system is ready, just not trading.
