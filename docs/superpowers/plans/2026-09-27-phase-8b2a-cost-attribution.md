# Phase 8B2a — Cost Correctness and Per-Trade Attribution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the backtest cost model express §6.4's stress honestly, and seal a per-trade decomposition of what a run paid in spread and slippage.

**Architecture:** Three engine changes, none of them touching `BacktestResult`. The stress multiplier reaches the *observed* spread where the spread is observed, in the fill functions; the swap-credit rule becomes piecewise in the sign of the selected rate; and the fill model reports each leg's raw price and the two components it charged, so the engine can decompose an existing number rather than restate it. The decomposition rides a sidecar parallel to `result.trades` and is sealed in the bundle, where `CostSummary` stops being an independently-asserted total and becomes a checked aggregate of the detail.

**Tech Stack:** Python 3.12, Pydantic v2 strict/frozen models, stdlib `decimal`/`dataclasses`, pytest, Hypothesis, Ruff, strict mypy. No new dependency.

## Global Constraints

- Target Python `>=3.12,<3.13`. `CanonicalModel` semantics: `strict=True`, `frozen=True`, `extra="forbid"`.
- `Decimal` for money, prices and rates. Every exact factor multiplies first and the single division runs last, on an already-exact numerator — the shape `costs.py:105-120` documents at length, and the reason two runs over identical inputs cannot differ in trailing zeros and so cannot differ in digest.
- Every canonical timestamp is timezone-aware UTC.
- Pydantic validators raise `ValueError`; plain functions raise the typed error.
- **No field is added to `BacktestResult`, `SimulatedTrade` or `CostModel`.** The first two would move four pinned digest constants and make the sealed v1 legacy bundles unreadable; the third is forbidden by `tests/acceptance/test_phase6.py:54`, `:195-201` and `:204-216`.
- No new dependency, migration, ledger event type, CLI option, or command.
- Every new field on a sealed document is **defaulted and excluded when absent**, so a document that omits it keeps byte-identical canonical bytes. This is the `exclude_if` rule 8B1 established, and `_CANONICAL_BUNDLE_SHA256` in `tests/property/test_trial_evidence.py:198` is its regression witness.
- Use absolute imports. `BACKTEST_ALLOWED` admits `trading_house.research.backtest` but **not** `trading_house.research`.
- Run the focused test first, then the quality gates named in each task.

## File Map

### Create

- `src/trading_house/research/backtest/costs_attribution.py` — `TradeCostAttribution` and `CostAttribution`, kept out of `costs.py` because that module owns the *declared* costs and this owns what a *run* was charged. Small, so it is its own file rather than a section of either neighbour.
- `tests/unit/research/backtest/test_costs_attribution.py` — the decomposition and its reconciliation.
- `tests/acceptance/test_phase8b2a.py` — the 8B2a acceptance gate.

### Modify

- `src/trading_house/research/backtest/fills.py` — `Fill` reports its raw price and the two components it charged; `entry_fill` scales the observed spread by the multiplier.
- `src/trading_house/research/backtest/costs.py` — `_stressed_rate` implements §6.4's piecewise rule; the `CostModel` docstring's "stressing a positive carry increases profit" paragraph is replaced.
- `src/trading_house/research/backtest/engine.py` — `_closing_fill` inherits the spread scaling; `_trade` assembles the attribution; `_gross_pnl` delegates its conversion to a named `_price_to_money`; `BacktestOutcome` gains `attribution` and the reconciliation.
- `src/trading_house/research/evidence.py` — `EvidenceBundle.cost_attribution` plus its coupling and aggregate checks.
- `src/trading_house/ops/backtest.py` — `mark_to_market_bundle` populates the attribution and reports `COMPLETE`; its docstring's "costs is PARTIAL and never COMPLETE" paragraph is replaced.
- `src/trading_house/research/__init__.py` — re-export the two new names.
- `tests/unit/research/backtest/test_costs.py` — the swap-credit rule's four cases.
- `tests/unit/research/backtest/test_fills.py` — the spread scaling and the per-leg components.
- `tests/unit/research/backtest/test_engine.py` — the known-answer proof extended to the decomposition.
- `tests/integration/research/test_backtest_evidence.py` — a stressed run end to end.
- `README.md` — the complete attribution, the stress semantics, and the shared-`run_id` limit.

### Explicitly unchanged

- `src/trading_house/research/backtest/result.py` — no field, no digest movement.
- `src/trading_house/research/legacy_import.py` — builds no attribution, so §6.3's "must not invent a numeric residual" holds by construction.
- `src/trading_house/research/backtest/mark.py` — the equity series is untouched.
- `migrations/`, `pyproject.toml`.

---

### Task 1: Make the cost model express §6.4's stress honestly

**Files:**
- Modify: `src/trading_house/research/backtest/fills.py:50-69`
- Modify: `src/trading_house/research/backtest/costs.py:28-166`
- Test: `tests/unit/research/backtest/test_fills.py`
- Test: `tests/unit/research/backtest/test_costs.py`

**Interfaces:**
- Consumes: `CostModel.stress_multiplier`, `slippage_price_offset`, `swap_cost`, `bar.spread`, `contract.point_size`.
- Produces: `entry_fill` whose `Fill.price` crosses a multiplier-scaled half-spread; `_stressed_rate(rate, multiplier)` in `costs.py`.
- No signature changes outside `entry_fill`'s behaviour.

- [ ] **Step 1: Write the failing tests**

In `tests/unit/research/backtest/test_costs.py`, add the four cases of §6.4's swap rule. Read the file's `_model` and `_contract` helpers first and use them.

```python
def test_a_charge_becomes_m_times_more_negative() -> None:
    """A negative rate is a cost, so stress makes it worse. The branch the
    code already implemented, kept so the two cases are separately pinned."""

    model = _model(swap_long_points_per_day=Decimal("-1"), stress_multiplier=Decimal("2"))
    contract = _contract(point_size=Decimal("0.00001"), value_per_price_increment=Decimal("1"))
    held = {
        "lots": Decimal(1),
        "contract": contract,
        "opened_at": datetime(2026, 9, 21, 9, 0, tzinfo=UTC),  # Monday
        "closed_at": datetime(2026, 9, 22, 9, 0, tzinfo=UTC),  # Tuesday, one crossing
    }

    assert swap_cost(model=model, side=Side.BUY, **held) == Decimal("-2")


def test_a_credit_never_grows_under_stress() -> None:
    """The defect this replaces. A positive rate is money the broker pays, and
    the old unconditional ``rate * m`` handed a positive-carry strategy MORE
    profit at 2x than at 1x -- so it cleared the section 12 gate more easily
    stressed, which is the opposite of a stress."""

    model = _model(swap_short_points_per_day=Decimal("1"), stress_multiplier=Decimal("2"))
    contract = _contract(point_size=Decimal("0.00001"), value_per_price_increment=Decimal("1"))
    held = {
        "lots": Decimal(1),
        "contract": contract,
        "opened_at": datetime(2026, 9, 21, 9, 0, tzinfo=UTC),
        "closed_at": datetime(2026, 9, 22, 9, 0, tzinfo=UTC),
    }

    assert swap_cost(model=model, side=Side.SELL, **held) == Decimal(0)


@pytest.mark.parametrize("rate", [Decimal("-1"), Decimal("1")], ids=["charge", "credit"])
def test_the_stressed_rate_matches_hand_derived_constants(rate: Decimal) -> None:
    """Section 6.4: "At m = 1, every component exactly matches baseline."

    Both signs, because a rule that only reproduced the baseline for charges
    would be a redefinition of the baseline dressed as a stress.
    """

    stressed = _model(swap_long_points_per_day=rate, swap_short_points_per_day=rate, stress_multiplier=Decimal(1))
    plain = _model(swap_long_points_per_day=rate, swap_short_points_per_day=rate)
    contract = _contract(point_size=Decimal("0.00001"), value_per_price_increment=Decimal("1"))
    held = {
        "lots": Decimal(1),
        "contract": contract,
        "opened_at": datetime(2026, 9, 21, 9, 0, tzinfo=UTC),
        "closed_at": datetime(2026, 9, 22, 9, 0, tzinfo=UTC),
    }

    assert swap_cost(model=stressed, side=Side.BUY, **held) == swap_cost(model=plain, side=Side.BUY, **held)
    assert swap_cost(model=stressed, side=Side.SELL, **held) == swap_cost(model=plain, side=Side.SELL, **held)


def test_a_credit_beyond_double_stress_becomes_a_charge() -> None:
    """``m > 2`` is total, not refused. The field's only bound is ``gt=0``, and
    "what if costs are worse than 2x" is a legitimate question; answering it by
    refusing would be less honest than answering it."""

    model = _model(swap_short_points_per_day=Decimal("1"), stress_multiplier=Decimal("3"))
    contract = _contract(point_size=Decimal("0.00001"), value_per_price_increment=Decimal("1"))
    held = {
        "lots": Decimal(1),
        "contract": contract,
        "opened_at": datetime(2026, 9, 21, 9, 0, tzinfo=UTC),
        "closed_at": datetime(2026, 9, 22, 9, 0, tzinfo=UTC),
    }

    assert swap_cost(model=model, side=Side.SELL, **held) < 0
```

In `tests/unit/research/backtest/test_fills.py`, add the spread-scaling cases. Read the file's existing helpers first.

```python
def test_an_entry_crosses_a_multiplier_scaled_half_spread() -> None:
    """Spread is observed, never declared, so the multiplier reaches it where
    it is observed. A stress the run does not pay is not a stress."""

    bar = _bar(high=Decimal("1.10010"), spread=10)
    contract = _contract(point_size=Decimal("0.00001"))
    model = _cost_model()

    nominal = entry_fill(bar=bar, side=Side.BUY, contract=contract, model=model)
    stressed = entry_fill(
        bar=bar, side=Side.BUY, contract=contract, model=model.model_copy(update={"stress_multiplier": Decimal("1.5")})
    )

    assert nominal.price - bar.open == Decimal("0.00005")
    assert stressed.price - bar.open == Decimal("0.000075")


def test_a_stop_exit_crosses_no_spread_at_any_multiplier() -> None:
    """The asymmetry is the model, not an oversight to smooth over. A stop or
    target exit triggers off a raw bar price and pays only slippage, and §6.3
    attributes what is charged rather than what would be conventional."""

    bar = _bar(high=Decimal("1.10010"), low=Decimal("1.09900"), spread=10)
    contract = _contract(point_size=Decimal("0.00001"))
    model = _cost_model(slippage_points_per_side=Decimal(0)).model_copy(
        update={"stress_multiplier": Decimal("2")}
    )

    closed = resolve_exit(
        bar=bar, side=Side.BUY, stop=Decimal("1.09950"), target=None, contract=contract, model=model
    )

    assert closed is not None
    assert closed.fill.price == Decimal("1.09950")
```

- [ ] **Step 2: Run the tests and verify they fail**

Run: `uv run pytest tests/unit/research/backtest/test_costs.py tests/unit/research/backtest/test_fills.py -q --no-cov`

Expected: the credit case fails (`Decimal("2")` against `Decimal(0)`), and the entry-scaling case fails because the half-spread is unscaled.

- [ ] **Step 3: Scale the observed spread at fill time**

In `src/trading_house/research/backtest/fills.py`, change `entry_fill`'s half-spread:

```python
    # The multiplier belongs here rather than in ``CostModel``, because spread is
    # observed from the bar and never declared: there is no spread field to
    # scale, and ``tests/acceptance/test_phase6.py`` forbids adding one. At
    # ``m = 1`` the multiply is ``Decimal(x) * 1`` and changes nothing, so every
    # result digest stays exactly where it is. Multiplication first and the
    # halving last, so the only division is by two and it terminates.
    half_spread = Decimal(bar.spread) * model.stress_multiplier * contract.point_size / _HALF
```

`_closing_fill` in `engine.py:681-689` calls `entry_fill` and inherits this with no change.

Do **not** touch `resolve_exit`'s spread behaviour. It applies no spread at all, at
any multiplier, and that is the model's real behaviour. Task 2 widens `Fill` with
the per-leg components and updates `resolve_exit`'s three constructions then; that
is not this task's work.

- [ ] **Step 4: Implement the swap-credit rule**

In `src/trading_house/research/backtest/costs.py`, add `_TWO` beside `_ROUND_TRIP_SIDES` and add the helper:

```python
_TWO: Final[Decimal] = Decimal(2)


def _stressed_rate(rate: Decimal, multiplier: Decimal) -> Decimal:
    """Section 6.4's adverse multiplier, which is piecewise in the rate's sign.

    A charge becomes ``m`` times more negative. A credit is reduced by
    ``(m - 1) * abs(rate)``, so stress can never *increase* carry -- which is
    the defect this replaces. The previous unconditional ``rate * m`` handed a
    positive-carry strategy more profit at 2x than at 1x, and this module's own
    docstring had to record that as an accepted consequence rather than fix it.

    At ``m = 1`` both branches return the rate unchanged, which is §6.4's "at
    m = 1 every component exactly matches baseline" and is what makes this a
    stress rather than a redefinition of the baseline. Below 1 a credit grows,
    which is the legitimate "what if costs are lower than assumed" probe
    ``CostModel`` already invites; at 2 it is zero; above 2 it becomes a charge.
    """

    if rate < 0:
        return rate * multiplier
    return rate * (_TWO - multiplier)
```

In `swap_cost`, replace the unconditional multiplier in the returned product:

```python
    return (
        _stressed_rate(rate, model.stress_multiplier)
        * Decimal(day_count)
        * lots
        * contract.point_size
        * contract.value_per_price_increment
        / contract.price_increment
    )
```

The multiplication order and the single trailing division are unchanged, so
`test_the_stress_multiplier_is_applied_before_the_only_division` (`test_costs.py:262`)
keeps its hand-derived `-1` and keeps proving the rounding property.

- [ ] **Step 5: Replace the `CostModel` docstring paragraph that is now false**

`costs.py:46-51` currently documents stressing a positive carry as accepted. Replace that paragraph with the rule, where it lives, and the reason the consequence is no longer accepted. Keep the surrounding text about `stress_multiplier` being the only default and being bounded above zero — both still true.

- [ ] **Step 6: Run the cost and fill tests and the gates**

Run:
```powershell
uv run pytest tests/unit/research/backtest/ -q --no-cov
uv run pytest tests/acceptance/test_phase6.py -q --no-cov
uv run ruff format src/trading_house/research/backtest/fills.py src/trading_house/research/backtest/costs.py tests/unit/research/backtest/test_costs.py tests/unit/research/backtest/test_fills.py
uv run ruff check .
uv run mypy
```

Expected: all pass, `test_costs.py:93` and `:262` and `:304` unchanged and green, ruff and mypy clean.

- [ ] **Step 7: Confirm nothing moved**

Run: `uv run pytest tests/property/test_trial_evidence.py -q --no-cov` and `uv run pytest tests/acceptance/test_phase8a.py -q --no-cov`

Expected: green. The three pinned Phase 7 result digests and `_CANONICAL_BUNDLE_SHA256` are unmoved, because every change is a no-op at `m = 1` and no test stresses a credit.

- [ ] **Step 8: Commit the cost model**

```powershell
git add src/trading_house/research/backtest/fills.py src/trading_house/research/backtest/costs.py tests/unit/research/backtest/test_costs.py tests/unit/research/backtest/test_fills.py
git commit -m "fix: stress the observed spread and stop a positive carry growing under stress"
```

---

### Task 2: Capture the per-leg decomposition and carry it on the outcome

**Files:**
- Create: `src/trading_house/research/backtest/costs_attribution.py`
- Modify: `src/trading_house/research/backtest/fills.py:38-104`
- Modify: `src/trading_house/research/backtest/engine.py:691-746` and the `BacktestOutcome` construction
- Modify: `src/trading_house/research/__init__.py`
- Test: `tests/unit/research/backtest/test_costs_attribution.py`
- Test: `tests/unit/research/backtest/test_engine.py`

**Interfaces:**
- Consumes: everything Task 1 produced, plus `BacktestOutcome`, `SimulatedTrade`, `_gross_pnl`.
- Produces: `Fill.raw_price`, `Fill.spread_charged`, `Fill.slippage_charged`; `TradeCostAttribution`; `CostAttribution`; `BacktestOutcome.attribution`; `Backtester._price_to_money`.

- [ ] **Step 1: Extend the known-answer test, and write the failing attribution tests**

`tests/unit/research/backtest/test_engine.py`'s known-answer proof pins `gross_pnl == Decimal("3.37")` from hand arithmetic. Extend it so the decomposition reconstructs that same 3.37, and add the two legs' asymmetry:

```python
    # Phase 8B2a: the decomposition reconstructs the number above exactly. A
    # fill-model regression fails a hand-computed value, not a shape.
    attribution = outcome.attribution
    assert len(attribution.trades) == len(outcome.result.trades)
    for entry, trade in zip(attribution.trades, outcome.result.trades, strict=True):
        assert entry.proposal_id == trade.proposal_id
        assert entry.post_fill_gross == trade.gross_pnl
        assert entry.market_pnl - entry.spread_cost - entry.slippage_cost == trade.gross_pnl
    # Both trades in this fixture are TIME exits, so each crosses a second
    # half-spread from its own exit bar.
    assert all(entry.spread_cost > 0 for entry in attribution.trades)
    assert all(entry.slippage_cost == 0 for entry in attribution.trades)
```

Create `tests/unit/research/backtest/test_costs_attribution.py` with the failure cases:

```python
def test_a_decomposition_that_does_not_reconstruct_its_own_total_is_refused() -> None:
    with pytest.raises(ValidationError, match="market PnL"):
        CostAttribution(
            trades=(
                TradeCostAttribution(
                    proposal_id="p-1",
                    market_pnl=Decimal("10"),
                    spread_cost=Decimal("2"),
                    slippage_cost=Decimal("1"),
                    post_fill_gross=Decimal("8"),
                ),
            )
        )


def test_an_empty_attribution_is_fine_and_says_nothing() -> None:
    """A run with no trades has no decomposition, and that is not a defect: the
    totals are all zero and both sides of the equality hold vacuously."""

    assert CostAttribution(trades=()).trades == ()
```

- [ ] **Step 2: Run the tests and verify they fail**

Run: `uv run pytest tests/unit/research/backtest/test_costs_attribution.py tests/unit/research/backtest/test_engine.py -q --no-cov`

Expected: import failure — `research.backtest.costs_attribution` does not exist.

- [ ] **Step 3: Create the attribution module**

Create `src/trading_house/research/backtest/costs_attribution.py`:

```python
"""What one trade was actually charged, split from the number that reported it.

Phase 8B2a. ``SimulatedTrade.gross_pnl`` is gross of commission and swap but
already contains spread and slippage, because the fill model charges both into
the two prices and discards the components. This module carries that split
beside the trade rather than inside it: a field on ``SimulatedTrade`` would move
``BacktestResult.digest()`` for every run, and with it four pinned digest
constants naming Phase 7 artifacts that exist on no machine and can never be
re-derived.

The equality checked here is not arithmetic added for its own sake. It is the
claim that a reported number was not merely restated but genuinely decomposed:
``market_pnl - spread_cost - slippage_cost`` must equal the ``gross_pnl`` the
result already carries, exactly, for every trade.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Self

from pydantic import model_validator

from trading_house.core.values import CanonicalModel, NonEmptyStr


class TradeCostAttribution(CanonicalModel):
    """One trade's costs, in money, split three ways.

    ``market_pnl`` is the raw move and does not scale with any stress: the
    market's move is a fact about the run. ``spread_cost`` and
    ``slippage_cost`` are what the fills actually charged, so they do.

    ``post_fill_gross`` is stored rather than derived because §6.3 names it and
    because a stored value which must equal its own derivation is a *checked*
    duplicate rather than a silent one -- the same pattern ``BacktestOutcome``
    uses to bind a series to a result.
    """

    proposal_id: NonEmptyStr
    market_pnl: Decimal
    spread_cost: Decimal
    slippage_cost: Decimal
    post_fill_gross: Decimal


class CostAttribution(CanonicalModel):
    """Every trade's split, in result order.

    Ordered and parallel to ``result.trades``, matched by ``proposal_id`` at
    every index by ``BacktestOutcome`` -- a tuple that can be reordered, padded
    or paired with another run's trades would otherwise still be internally
    consistent.
    """

    trades: tuple[TradeCostAttribution, ...]

    @model_validator(mode="after")
    def each_split_reconstructs_its_own_total(self) -> Self:
        for split in self.trades:
            rebuilt = split.market_pnl - split.spread_cost - split.slippage_cost
            if rebuilt != split.post_fill_gross:
                raise ValueError(
                    "a trade's market PnL less spread and slippage must equal its post-fill gross"
                )
        return self
```

- [ ] **Step 4: Make each fill report what it charged**

In `src/trading_house/research/backtest/fills.py`, extend `Fill`:

```python
@dataclass(frozen=True, slots=True)
class Fill:
    price: Decimal
    at: datetime
    raw_price: Decimal
    """The price before any spread or slippage: this leg's own ``bar.open`` for
    an entry or a time exit, and the trigger price for a stop or target."""

    spread_charged: Decimal
    """The half-spread this leg crossed, in price terms and never negative. Zero
    for a stop or target exit, which crosses no spread at all."""

    slippage_charged: Decimal
    """The absolute slippage offset this leg paid, in price terms."""
```

`entry_fill` returns all four; the spread is `half_spread` and the slippage is
`abs(offset)`, so both are costs rather than signed price deltas:

```python
    return Fill(
        price=raw_price + offset,
        at=bar.event_time,
        raw_price=bar.open,
        spread_charged=half_spread,
        slippage_charged=abs(offset),
    )
```

All **four** `Exit` constructions in `resolve_exit` — a STOP and a TARGET under each
side — gain the same three arguments, with `spread_charged=Decimal(0)` and
`slippage_charged=abs(offset)`. Count them: `fills.py` builds four, not three.

- [ ] **Step 5: Assemble the attribution in the engine and bind it to the trades**

In `engine.py`, name the price-to-money conversion so the attribution and
`gross_pnl` cannot drift onto different roundings, and have `_gross_pnl` use it:

```python
    def _price_to_money(self, price_delta: Decimal, lots: Decimal) -> Decimal:
        """A price distance in account currency, for one quantity.

        The single division by ``price_increment`` runs last, on an
        already-exact numerator -- the shape ``swap_cost`` uses and the reason
        two runs over identical inputs cannot differ only in trailing zeros and
        so cannot differ in digest.
        """

        return (
            price_delta * lots * self._contract.value_per_price_increment / self._contract.price_increment
        )

    def _gross_pnl(
        self, *, side: Side, entry_price: Decimal, exit_price: Decimal, lots: Decimal
    ) -> Decimal:
        move = exit_price - entry_price if side is Side.BUY else entry_price - exit_price
        return self._price_to_money(move, lots)
```

Keep `_gross_pnl`'s docstring but correct its closing sentence: the breakdown is
no longer partial, and say where the full split now lives.

In `_trade`, build the split alongside the existing totals:

```python
        market_pnl = self._gross_pnl(
            side=signal.side,
            entry_price=position.entry.raw_price,
            exit_price=closed.fill.raw_price,
            lots=signal.lots,
        )
        spread_cost = self._price_to_money(
            position.entry.spread_charged + closed.fill.spread_charged, signal.lots
        )
        slippage_cost = self._price_to_money(
            position.entry.slippage_charged + closed.fill.slippage_charged, signal.lots
        )
```

and return it. `_trade` currently returns a `SimulatedTrade`; change it to return
**both**, as a `tuple[SimulatedTrade, TradeCostAttribution]`, and thread an
`attributions` list alongside `trades` in `run`, so the two are appended in one
place and cannot fall out of step:

```python
        return (
            SimulatedTrade(
                proposal_id=signal.proposal_id,
                side=signal.side,
                lots=signal.lots,
                entry_price=position.entry.price,
                entry_at=position.entry.at,
                exit_price=closed.fill.price,
                exit_at=closed.fill.at,
                exit_kind=closed.kind,
                gross_pnl=gross_pnl,
                commission=commission,
                swap=swap,
                net_pnl=gross_pnl - commission + swap,
            ),
            TradeCostAttribution(
                proposal_id=signal.proposal_id,
                market_pnl=market_pnl,
                spread_cost=spread_cost,
                slippage_cost=slippage_cost,
                post_fill_gross=market_pnl - spread_cost - slippage_cost,
            ),
        )
```

`post_fill_gross` is built from the components rather than reusing `gross_pnl`,
because the whole point is that the two are computed independently and then
compared. `BacktestOutcome` is built with both tuples:

```python
        return BacktestOutcome(
            result=result,
            equity=EquitySeries(firm_equity=request.firm_equity, observations=tuple(observations)),
            attribution=CostAttribution(trades=tuple(attributions)),
        )
```

Add the field and its reconciliation to the existing `BacktestOutcome` model in
`engine.py`, so the split is checked against the trades rather than trusted:

```python
    attribution: CostAttribution

    @model_validator(mode="after")
    def the_attribution_is_the_result_it_decomposes(self) -> Self:
        """The split is checked against the trades rather than trusted.

        A tuple can be internally consistent and still describe another run: a
        reordered one, a padded one, or one whose ``post_fill_gross`` no longer
        matches the ``gross_pnl`` the result reports. All three are refused, in
        the same words ``EvidenceBundle`` will use in Task 3, so a disagreement
        reads the same whether it was caught at construction or at read time.
        """

        if len(self.attribution.trades) != len(self.result.trades):
            raise ValueError("the attribution must cover every trade exactly once")
        for split, trade in zip(self.attribution.trades, self.result.trades, strict=True):
            if split.proposal_id != trade.proposal_id:
                raise ValueError("the attribution must be in result order")
            if split.post_fill_gross != trade.gross_pnl:
                raise ValueError("a split's post-fill gross must equal its trade's gross PnL")
        return self
```

Add one test per refusal to `test_costs_attribution.py`: a tuple of the wrong
length, a reordered pair, and one whose `post_fill_gross` was changed. The third
is the one that matters — it is the check that would catch a fill-model
regression, so it must fail when the decomposition is falsified rather than when
the model is merely absent.

- [ ] **Step 6: Run the tests and the gates**

Run:
```powershell
uv run pytest tests/unit/research/backtest/ -q --no-cov
uv run pytest tests/integration/research/test_backtest_determinism.py -q --no-cov
uv run ruff format src/trading_house/research/backtest/ tests/unit/research/backtest/
uv run ruff check .
uv run mypy
```

Expected: all pass, including the determinism suite, which is the witness that
the decomposition is as reproducible as the result it decomposes.

- [ ] **Step 7: Commit the decomposition**

```powershell
git add src/trading_house/research/backtest/costs_attribution.py src/trading_house/research/backtest/fills.py src/trading_house/research/backtest/engine.py src/trading_house/research/__init__.py tests/unit/research/backtest/test_costs_attribution.py tests/unit/research/backtest/test_engine.py
git commit -m "feat: decompose every trade's spread and slippage from its reported gross"
```

---

### Task 3: Seal the attribution and make CostSummary a checked aggregate

**Files:**
- Modify: `src/trading_house/research/evidence.py`
- Modify: `src/trading_house/ops/backtest.py`
- Test: `tests/integration/research/test_backtest_evidence.py`
- Test: `tests/acceptance/test_phase8b2a.py`
- Modify: `README.md`

**Interfaces:**
- Consumes: `CostAttribution` (Task 2), `CostSummary`, `CostAttributionStatus`, the `mark_to_market` field's `exclude_if` pattern.
- Produces: `EvidenceBundle.cost_attribution`.

- [ ] **Step 1: Write the failing coupling and aggregate tests**

Create `tests/acceptance/test_phase8b2a.py`. It reuses 8B1's acceptance helpers
verbatim rather than re-creating them — read `tests/acceptance/test_phase8b1.py`
first and import from it: `_outcome(bars, *, last_bar=None)`,
`_run_command(...)` (which returns the command's payload dict),
`_bundle_of(payload)` (which parses that payload's `"bundle"` over the JSON
path), `_session_ramp`, and `_sealed_legacy_bundle(tmp_path, arm)`, which
returns a real `(EvidenceStore, EvidenceBundle)` pair produced by the real
importer. Every case below goes through a real command or a real importer; none
of them builds a bundle by hand.

```python
def test_a_complete_cost_summary_must_carry_the_attribution_it_aggregates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bundle = _bundle_of(_run_command(monkeypatch, tmp_path, marked=True))

    assert bundle.costs.status is CostAttributionStatus.COMPLETE
    with pytest.raises(ValidationError, match="attribution"):
        bundle.model_copy(update={"cost_attribution": None})


def test_a_partial_summary_must_not_carry_one(tmp_path: Path) -> None:
    """The other direction, through the real importer.

    A v1 legacy bundle is ``PARTIAL`` and carries no attribution. Attaching one
    would claim a completeness the summary explicitly denies, so the bundle
    must refuse rather than seal a document whose two halves disagree.
    """

    _store, legacy = _sealed_legacy_bundle(tmp_path)

    assert legacy.costs.status is CostAttributionStatus.PARTIAL
    assert legacy.cost_attribution is None
    with pytest.raises(ValidationError, match="attribution"):
        legacy.model_copy(update={"cost_attribution": _prospective_attribution()})


def test_an_attribution_that_disagrees_with_its_trades_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bundle = _bundle_of(_run_command(monkeypatch, tmp_path, marked=True))
    assert bundle.cost_attribution is not None
    trades = bundle.cost_attribution.trades
    if not trades:
        pytest.skip("this fixture produced no trades; the run-level checks below still cover it")
    tampered = trades[0].model_copy(update={"post_fill_gross": Decimal("999")})

    with pytest.raises(ValidationError, match="post-fill gross"):
        bundle.model_copy(
            update={
                "cost_attribution": bundle.cost_attribution.model_copy(
                    update={"trades": (tampered, *trades[1:])}
                )
            }
        )


def test_a_summary_that_disagrees_with_its_attribution_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The aggregate is checked, not trusted. ``CostSummary``'s own validator
    already refuses a COMPLETE with a ``None``; this is the other direction --
    a COMPLETE whose four fields are all present and whose spread total is a
    number the trades behind it do not produce."""

    bundle = _bundle_of(_run_command(monkeypatch, tmp_path, marked=True))

    with pytest.raises(ValidationError, match="spread cost"):
        bundle.model_copy(
            update={"costs": bundle.costs.model_copy(update={"spread_cost": Decimal("12345")})}
        )
```

Where `_prospective_attribution()` is a small local helper returning
`CostAttribution(trades=(TradeCostAttribution(proposal_id="p-1", market_pnl=Decimal(0),
spread_cost=Decimal(0), slippage_cost=Decimal(0), post_fill_gross=Decimal(0)),))`
— a well-formed attribution is all the "must not carry one" case needs, since
the coupling rule is checked before any total.


- [ ] **Step 2: Run and verify failure**

Run: `uv run pytest tests/acceptance/test_phase8b2a.py -q --no-cov`

Expected: failures — `EvidenceBundle` has no `cost_attribution`.

- [ ] **Step 3: Add the field and its checks**

In `src/trading_house/research/evidence.py`, add the field beside `mark_to_market`, reusing the same `_is_absent` predicate and the same rationale for the exclusion:

```python
    cost_attribution: CostAttribution | None = Field(default=None, exclude_if=_is_absent)
```

Add two validators. The first is the coupling, worded as 8B1's basis coupling:

```python
    @model_validator(mode="after")
    def the_attribution_is_present_exactly_where_the_summary_claims_one(self) -> Self:
        """A COMPLETE summary and a per-trade attribution are the same claim.

        ``PARTIAL`` with two ``None`` components is the honest record of a run
        that could not separate spread from slippage -- which is every Phase 7
        artifact, whose bar store was deleted. A COMPLETE summary with no
        attribution behind it asserts a breakdown it does not carry, and an
        attribution beside a PARTIAL summary claims a completeness the summary
        denies.
        """

        complete = self.costs.status is CostAttributionStatus.COMPLETE
        if complete and self.cost_attribution is None:
            raise ValueError("a complete cost summary must carry the attribution it aggregates")
        if not complete and self.cost_attribution is not None:
            raise ValueError("only a complete cost summary may carry a per-trade attribution")
        return self
```

The second checks the aggregate against the detail and the result, in the same
wording as the outcome so a disagreement reads the same wherever it is caught:

```python
    @model_validator(mode="after")
    def the_summary_is_the_attribution_it_aggregates(self) -> Self:
        # Commission and swap are sums over result.trades, which every bundle
        # carries whole -- legacy included -- so they are checkable
        # unconditionally and sit ABOVE the early return. Below it they stay
        # asserted rather than derived for precisely the Phase 7 artifacts that
        # cannot be regenerated; that ordering was a real defect, not a style
        # choice, and the split is the per-trade attribution, which does not
        # exist without one.
        if self.costs.commission != sum((t.commission for t in self.result.trades), Decimal(0)):
            raise ValueError("the summary's commission must equal the sum of the trades'")
        if self.costs.swap != sum((t.swap for t in self.result.trades), Decimal(0)):
            raise ValueError("the summary's swap must equal the sum of the trades'")
        if self.cost_attribution is None:
            return self
        # The three per-trade checks are NOT restated here. Task 2 left a
        # predicate, ``attribution_disagreement(attribution, result)``, in
        # ``research/backtest/costs_attribution.py`` which returns the refusal
        # reason or ``None``, and ``BacktestOutcome`` calls it. This bundle must
        # call the same one: the two surfaces exist to catch the same defect in
        # the same place, and duplicated string literals drift the moment one is
        # edited. ``evidence.py`` may import from ``research.backtest`` --
        # it already does for ``BacktestResult`` and for the series -- so the
        # import is available; the constraint runs the other way, and a module
        # inside ``research/backtest/`` may not import from ``research/``.
        disagreement = attribution_disagreement(self.cost_attribution, self.result)
        if disagreement is not None:
            raise ValueError(disagreement)
        if sum((s.spread_cost for s in self.cost_attribution.trades), Decimal(0)) != self.costs.spread_cost:
            raise ValueError("the summary's spread cost must equal the sum of the splits'")
        if (
            sum((s.slippage_cost for s in self.cost_attribution.trades), Decimal(0))
            != self.costs.slippage_cost
        ):
            raise ValueError("the summary's slippage cost must equal the sum of the splits'")
        return self
```

- [ ] **Step 4: Populate it and report COMPLETE**

In `src/trading_house/ops/backtest.py`'s `mark_to_market_bundle`, replace the
`costs=CostSummary(...)` block with:

```python
        cost_attribution=outcome.attribution,
        costs=CostSummary(
            status=CostAttributionStatus.COMPLETE,
            commission=sum((trade.commission for trade in result.trades), Decimal(0)),
            swap=sum((trade.swap for trade in result.trades), Decimal(0)),
            spread_cost=sum((s.spread_cost for s in outcome.attribution.trades), Decimal(0)),
            slippage_cost=sum((s.slippage_cost for s in outcome.attribution.trades), Decimal(0)),
        ),
```

Rewrite the docstring's "``costs`` is PARTIAL and never COMPLETE" paragraph: it
is now false. State that the attribution is sealed whole for the same reason
`mark_to_market` is — the aggregate is a function of the detail, and a total
whose inputs are not retained cannot be re-derived or re-audited.

- [ ] **Step 5: Add the integration coverage**

In `tests/integration/research/test_backtest_evidence.py`, add a case that a
prospective bundle now reports `COMPLETE` with all four components present and
an attribution covering every trade, and a case that a run at
`--stress-multiplier 1.5` seals an attribution whose `spread_cost` is larger
than the same run at 1.0 while `market_pnl` is unchanged. The second is the
integration-level proof of C-4: the stress is paid, and the market's move is not
distorted by it.

- [ ] **Step 6: Update the README**

Add, in the Phase 8B1 section or a new 8B2a one:

- a prospective bundle carries a **complete** per-trade attribution —
  `market_pnl`, `spread_cost`, `slippage_cost`, `post_fill_gross` — and
  `CostSummary` is the checked aggregate of it, so the four fields on the
  summary cannot disagree with the trades behind them;
- the stress semantics: a charge becomes `m` times more negative, a credit is
  reduced so stress can never increase carry, the observed spread is scaled on
  the legs that cross spread, and at `m = 1` every component exactly matches the
  baseline;
- spread is crossed on entry and on a time exit but **not** on a stop or target
  exit, and that asymmetry is the model rather than an oversight;
- the attribution explains *where the cost went*; it does not make the cost
  model correct. The spread is a mid-price half-spread the bar store observed,
  not a depth-aware fill, and MT5 does not provide the depth data a better model
  would need;
- the known limit: a run at `1.5x` and the same run at `1.0x` share a `run_id`,
  because `_run_id` omits the cost model and the fix is closed — the legacy
  importer refuses any artifact whose digest is not its own `result.digest()`, so
  changing the identity would make every Phase 7 artifact fail its own import.
  The scenario identity rides the sealed attribution instead.

- [ ] **Step 7: Run the acceptance, property, and architecture checks**

Run:
```powershell
uv run pytest tests/acceptance/ tests/property/ tests/integration/research/ -q --no-cov
uv run ruff format --check .
uv run ruff check .
uv run mypy
```

Expected: all pass. `tests/property/test_trial_evidence.py`'s
`_CANONICAL_BUNDLE_SHA256` must be **unmoved** — that bundle is `PARTIAL` and
omits both new fields, which is exactly the property the exclusion buys.

- [ ] **Step 8: Commit the sealing**

```powershell
git add src/trading_house/research/evidence.py src/trading_house/ops/backtest.py tests/acceptance/test_phase8b2a.py tests/integration/research/test_backtest_evidence.py README.md
git commit -m "feat: seal the per-trade cost attribution and check the summary against it"
```

---

### Task 4: Verify the whole slice

**Files:** No source files are created.

- [ ] **Step 1: Run the complete non-container suite**

```powershell
$env:UV_SYSTEM_CERTS = "1"
uv run pytest -m "not integration" --no-cov
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv lock --check
git diff --check
git status --short
```

Expected: all green, tree clean.

- [ ] **Step 2: Run the complete container-backed suite**

```powershell
$env:UV_SYSTEM_CERTS = "1"
uv run pytest
```

Expected: green at or above the 95% coverage floor. If Docker is unavailable,
stop and report that the integration gate was not run; do not claim 8B2a complete.

- [ ] **Step 3: Drive the real operator flow at two multipliers**

With the local research database migrated and both DSNs exported, and a bar
store holding bars (provision it as the operator step — the Phase 7 store was
deleted):

```powershell
$env:TRADING_HOUSE_DATABASE_DSN = "postgresql://trading_house_runtime:development-only-runtime-password@127.0.0.1:5432/trading_house"
$env:TRADING_HOUSE_RESEARCH_LEDGER_DSN = "postgresql://trading_house_runtime:development-only-runtime-password@127.0.0.1:5432/trading_house_research"
$env:TRADING_HOUSE_EVIDENCE_ROOT = ".local/evidence"
```

Run `backtest run --mark-to-market` twice over identical inputs, once at
`--stress-multiplier 1` and once at `1.5`, with a fresh attempt each. Then
`research trial record` and `verify`.

Expected, and assert rather than assume: both bundles report `costs.status`
`complete`; each carries an attribution covering every trade; the 1.5x
`spread_cost` is strictly larger than the 1.0x one; the two bundles'
`market_pnl` for the same trade are **equal**, because the market's move is not
a stressed quantity; and `verify` reports `valid: true`.

If the run produces no trades, the attribution is empty and none of that is
checkable — say so in the report rather than claiming the checks passed.

- [ ] **Step 4: Check the final repository state**

```powershell
git status --short
git diff --check
```

Expected: only intended source, test, and documentation files; no evidence
content, DSN, contract, or seed script staged.

---

## Plan Self-Review Checklist

- [x] Every 8B2a design decision has a task: the cost model (§3.1-3.2) in Task 1, per-leg capture and the outcome binding (§3.3, §4) in Task 2, sealing and the aggregate (§4.1) in Task 3.
- [x] No field is added to `BacktestResult`, `SimulatedTrade` or `CostModel`; the four pinned digests and the v1 bundles are unmoved, and Task 1 Step 7 and Task 3 Step 7 both assert it.
- [x] The swap-credit rule is covered in four cases including `m = 1` in both signs and `m > 2`, and the two existing stressed tests are left to prove nothing regressed.
- [x] The spread asymmetry is pinned on both sides, because it is the model's real behaviour and the likeliest thing for a later reader to "fix".
- [x] The decomposition is checked against the existing `gross_pnl` rather than restating it, per trade, so a fill-model regression fails a number.
- [x] Every new sealed field is defaulted and excluded, with `_CANONICAL_BUNDLE_SHA256` as the regression witness.
- [x] The three options the code closes are recorded in the design and none of them is quietly worked around.
- [x] The shared-`run_id` limit is documented with its reason rather than fixed, and Task 4's operator step checks the stress where it can be checked.
- [x] No step contains a placeholder; every code step shows the code and every test step shows the test.
