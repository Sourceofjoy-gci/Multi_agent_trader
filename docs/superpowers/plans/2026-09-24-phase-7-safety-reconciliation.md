# Phase 7 Safety Reconciliation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close four review findings that can make Phase 7 evidence financially misleading before Tasks 9–11 continue.

**Architecture:** Keep the fixes at their existing trust boundaries: exit-policy consistency in the replay loop, proposal completeness in the canonical schema, session completeness in `FeatureEngine`, and slippage sign validity in `CostModel`. Add no migration, configuration switch, compatibility default, or alternate execution path.

**Tech Stack:** Python 3.12, Pydantic v2, Decimal, pytest, Hypothesis, uv, Ruff, mypy.

## Global Constraints

- Python 3.12. Pydantic v2 strict, frozen, `extra="forbid"`. All money and prices are `Decimal`; never float.
- `UV_SYSTEM_CERTS=1` prefixes every `uv` command. `uv run mypy` takes no path arguments.
- No new dependency, migration, schema table, CLI option, or portfolio/state abstraction.
- No comments are added to production code unless an existing contract cannot be made clear through names and types.
- No commits unless the user explicitly authorizes one. Leave each task's focused diff in the worktree and report it.
- Follow TDD: capture RED, make the minimum change, capture GREEN, then mutate the protected rule and confirm the intended test fails.
- Work only in `C:\Users\sourc\Downloads\Multi-Agent\.worktrees\phase-7-session-momentum` on `feature/phase-7-session-momentum`.
- Preserve the existing uncommitted Task 8 review work. Do not restore the whole engine file and lose the valid safety checks.

## File Structure

| File | Responsibility after this plan |
|---|---|
| `src/trading_house/research/backtest/result.py` | Names the typed exit-policy refusal. |
| `src/trading_house/research/backtest/engine.py` | Refuses mismatched or impossible exit policies; trails only after the current bar's exits. |
| `src/trading_house/core/schemas.py` | Requires every proposal to state its swap component. |
| `src/trading_house/features/engine.py` | Rejects partial session windows. |
| `src/trading_house/research/backtest/costs.py` | Requires nonnegative slippage points. |
| `tests/unit/research/backtest/test_engine.py` | Pins the two exit-policy refusals and accepted next-bar trail ordering. |
| `tests/unit/core/test_schemas_market.py` | Pins explicit swap declaration at the canonical boundary. |
| `tests/unit/features/test_engine.py` | Pins complete-session behavior and interior-gap refusal. |
| `tests/unit/research/backtest/test_costs.py` | Pins nonnegative slippage. |
| Existing proposal builders | Supply `expected_swap_cost_bps` explicitly. |

---

### Task 1: Reconcile the exit-policy safety patch

**Files:**
- Modify: `src/trading_house/research/backtest/result.py`
- Modify: `src/trading_house/research/backtest/engine.py`
- Test: `tests/unit/research/backtest/test_engine.py`

**Interfaces:**
- Produces: `RefusalKind.EXIT_POLICY = "exit_policy"`.
- Preserves: exhaustive `ExitPolicy` decomposition, fixed-target/proposal agreement, non-positive Chandelier refusal, and next-bar trailing.
- Removes: unapproved MFE lifecycle state and gating.

- [ ] **Step 1: Add the two failing safety tests**

```python
def test_a_fixed_target_arm_refuses_a_proposal_without_the_declared_target() -> None:
    with pytest.raises(BacktestRefused) as caught:
        _run(
            bars=_ramp(40),
            strategy=ToyStrategy(
                every_n=1000,
                target_r_multiple=None,
                policy=FixedTargetPolicy(kind="fixed_target", r_multiple=Decimal("1")),
            ),
        )

    assert caught.value.kind is RefusalKind.EXIT_POLICY


def test_a_non_positive_chandelier_level_refuses_the_run() -> None:
    with pytest.raises(BacktestRefused) as caught:
        trail_candidate(
            side=Side.BUY,
            current_stop=Decimal("1.09900"),
            bar=_bar(high=Decimal("1.10500"), close=Decimal("1.10000")),
            atr=Decimal("0.00010"),
            contract=_contract(min_stop_distance=Decimal("2")),
            policy=ChandelierPolicy(
                kind="chandelier", atr_multiple=Decimal(3), min_step_points=Decimal(1)
            ),
        )

    assert caught.value.kind is RefusalKind.EXIT_POLICY
```

- [ ] **Step 2: Run RED**

Run:

```powershell
$env:UV_SYSTEM_CERTS='1'; uv run pytest tests/unit/research/backtest/test_engine.py -q --no-cov -k "chandelier_run or fixed_target_arm_refuses or non_positive"
```

Expected: the accepted chandelier ordering test fails while the MFE gate is present; the new tests cannot resolve `RefusalKind.EXIT_POLICY`. Mypy independently reports the missing enum member at the two refusal sites.

- [ ] **Step 3: Implement the minimum approved behavior**

Add this enum member:

```python
EXIT_POLICY = "exit_policy"
```

Retain the existing exhaustive `_exit_arms` match, fixed-target/proposal equality refusal, and non-positive Chandelier refusal. Remove `_Position.observe`, `mfe`, `may_trail`, `_TRAIL_TRIGGER_R`, and the `position.may_trail()` loop gate. A trail derived after the current bar remains open updates `position.stop` for the next bar.

- [ ] **Step 4: Run GREEN and static checks**

Run:

```powershell
$env:UV_SYSTEM_CERTS='1'; uv run pytest tests/unit/research/backtest tests/property/test_trailing.py -q --no-cov
$env:UV_SYSTEM_CERTS='1'; uv run ruff check src/trading_house/research/backtest tests/unit/research/backtest
$env:UV_SYSTEM_CERTS='1'; uv run mypy
```

Expected: all pass, including `test_a_chandelier_run_stops_out_at_the_level_the_previous_bar_set`.

- [ ] **Step 5: Mutate each refusal**

Temporarily remove the fixed-target equality check and confirm only the mismatch test fails. Restore it. Temporarily replace the non-positive Chandelier refusal with `return None` and confirm only the non-positive test fails. Restore it.

- [ ] **Step 6: Preserve the diff**

Do not commit. Record the focused test commands and output in the task report.

---

### Task 2: Require an explicit swap declaration

**Files:**
- Modify: `src/trading_house/core/schemas.py`
- Modify: `src/trading_house/ops/backtest.py`
- Modify: `tests/property/test_schema_boundaries.py`
- Modify: `tests/unit/core/test_schemas_market.py`
- Modify: `tests/unit/research/backtest/conftest.py`
- Modify: `tests/unit/risk/conftest.py`

**Interfaces:**
- Changes: `TradeProposal.expected_swap_cost_bps` becomes required.
- Preserves: zero is a valid explicit declaration; negative values remain forbidden.

- [ ] **Step 1: Write the failing schema-boundary test**

```python
def test_trade_proposal_requires_an_explicit_swap_declaration(
    valid_proposal: dict[str, object],
) -> None:
    payload = {
        key: value
        for key, value in valid_proposal.items()
        if key != "expected_swap_cost_bps"
    }

    with pytest.raises(ValidationError, match="expected_swap_cost_bps"):
        TradeProposal(**payload)
```

Add `expected_swap_cost_bps=0.0` to the valid fixture after the test is written.

- [ ] **Step 2: Run RED**

Run:

```powershell
$env:UV_SYSTEM_CERTS='1'; uv run pytest tests/unit/core/test_schemas_market.py -q --no-cov -k explicit_swap
```

Expected: FAIL because omission currently succeeds through the schema default.

- [ ] **Step 3: Remove the schema default and update every builder**

Change the field to:

```python
expected_swap_cost_bps: NonNegativeFiniteFloat
```

Supply `0.0` explicitly in:

- `ToyStrategy._proposal` in `src/trading_house/ops/backtest.py`;
- `ToyStrategy._proposal` in `tests/unit/research/backtest/conftest.py`;
- `_proposal` in `tests/unit/risk/conftest.py`;
- `valid_proposal` in `tests/unit/core/test_schemas_market.py`;
- `BUILDERS[TradeProposal]` in `tests/property/test_schema_boundaries.py`.

Use `0.0` only where the fixture intentionally declares no swap. Preserve explicit nonzero values already used by risk tests.

- [ ] **Step 4: Run GREEN**

Run:

```powershell
$env:UV_SYSTEM_CERTS='1'; uv run pytest tests/unit/core/test_schemas_market.py tests/unit/risk tests/unit/research/backtest tests/property/test_schema_boundaries.py -q --no-cov
$env:UV_SYSTEM_CERTS='1'; uv run mypy
```

Expected: all pass.

- [ ] **Step 5: Mutate the requirement**

Temporarily restore the `= 0.0` default. Run the explicit-swap test and confirm it fails. Restore the required field.

- [ ] **Step 6: Preserve the diff**

Do not commit. Record the builder list and focused test output.

---

### Task 3: Reject partial session windows

**Files:**
- Modify: `src/trading_house/features/engine.py`
- Test: `tests/unit/features/test_engine.py`

**Interfaces:**
- Produces: private `FeatureEngine._window_is_complete(bars, start, through, timeframe) -> bool`.
- Changes: `prior_session_return` returns `None` for any incomplete preceding window; `session_open_price` and `bars_since_session_open` raise `InsufficientHistoryError` for any incomplete current window.

- [ ] **Step 1: Make existing positive fixtures genuinely complete**

Add a test helper that emits every M15 bar from a half-open start through the last expected open:

```python
def _m15_window(
    start: datetime,
    end: datetime,
    *,
    first_close: Decimal,
    last_close: Decimal,
) -> tuple[Bar, ...]:
    step = duration(Timeframe.M15)
    count = int((end - start) / step)
    return tuple(
        _m15_bar(
            start + index * step,
            first_close if index in (0, count - 1) else last_close,
        )
        for index in range(count)
    )
```

Use it for Asian windows in `_asian_then_london`, `_asian_only`, and `_flat_asian_then_london`. For the flat case pass equal first and last closes. Use it for the full London window in the session-closing-bar test. Interior values need not model a return path; the prior-return contract reads only the first and last closes.

- [ ] **Step 2: Add failing interior-gap tests**

```python
def test_prior_session_return_refuses_an_interior_gap() -> None:
    complete = _m15_window(
        datetime(2026, 9, 21, 0, 0, tzinfo=UTC),
        datetime(2026, 9, 21, 7, 0, tzinfo=UTC),
        first_close=Decimal("1.10000"),
        last_close=Decimal("1.10110"),
    )
    bars = (*complete[:1], *complete[2:])
    engine = FeatureEngine(
        FakeBarReader((*bars, _m15_bar(datetime(2026, 9, 21, 7, 0, tzinfo=UTC), Decimal("1.10120"))))
    )

    assert engine.prior_session_return(
        "fx.eurusd", Timeframe.M15, as_of=datetime(2026, 9, 21, 7, 0, tzinfo=UTC)
    ) is None


def test_current_session_methods_refuse_an_interior_gap() -> None:
    bars = (
        _m15_bar(datetime(2026, 9, 21, 7, 0, tzinfo=UTC), Decimal("1.10120")),
        _m15_bar(datetime(2026, 9, 21, 7, 30, tzinfo=UTC), Decimal("1.10130")),
    )
    engine = FeatureEngine(FakeBarReader(bars))
    as_of = datetime(2026, 9, 21, 7, 45, tzinfo=UTC)

    with pytest.raises(InsufficientHistoryError):
        engine.session_open_price("fx.eurusd", Timeframe.M15, as_of=as_of)
    with pytest.raises(InsufficientHistoryError):
        engine.bars_since_session_open("fx.eurusd", Timeframe.M15, as_of=as_of)
```

- [ ] **Step 3: Run RED**

Run:

```powershell
$env:UV_SYSTEM_CERTS='1'; uv run pytest tests/unit/features/test_engine.py -q --no-cov -k "interior_gap or prior_session_return or session_open_price or bars_since_session_open"
```

Expected: the new interior-gap tests fail because non-empty partial windows currently return plausible values.

- [ ] **Step 4: Implement one exact-window predicate**

```python
def _window_is_complete(
    bars: Sequence[Bar],
    start: datetime,
    through: datetime,
    timeframe: Timeframe,
) -> bool:
    step = duration(timeframe)
    expected = start
    for bar in bars:
        if bar.event_time != expected:
            return False
        expected += step
    return expected == through + step
```

For `prior_session_return`, call it with `through=end - duration(timeframe)` and return `None` when false. For `_current_session_bars`, call it with `through=reference` and raise `InsufficientHistoryError` when false. Reuse it for both current-session methods through their shared helper.

- [ ] **Step 5: Run GREEN and mutation**

Run:

```powershell
$env:UV_SYSTEM_CERTS='1'; uv run pytest tests/unit/features/test_engine.py -q --no-cov
$env:UV_SYSTEM_CERTS='1'; uv run ruff check src/trading_house/features/engine.py tests/unit/features/test_engine.py
$env:UV_SYSTEM_CERTS='1'; uv run mypy
```

Expected: all pass. Temporarily make `_window_is_complete` return true after the empty check; confirm the three interior-gap assertions fail. Restore it.

- [ ] **Step 6: Preserve the diff**

Do not commit. Record the exact completeness predicate and focused test output.

---

### Task 4: Reject negative slippage

**Files:**
- Modify: `src/trading_house/research/backtest/costs.py`
- Test: `tests/unit/research/backtest/test_costs.py`

**Interfaces:**
- Changes: `CostModel.slippage_points_per_side` requires `Decimal >= 0`.

- [ ] **Step 1: Write the failing test**

```python
def test_slippage_points_per_side_must_be_nonnegative() -> None:
    with pytest.raises(ValidationError):
        _model(slippage_points_per_side=Decimal("-0.1"))
```

- [ ] **Step 2: Run RED**

Run:

```powershell
$env:UV_SYSTEM_CERTS='1'; uv run pytest tests/unit/research/backtest/test_costs.py -q --no-cov -k nonnegative
```

Expected: FAIL because a negative declaration currently constructs.

- [ ] **Step 3: Add the field constraint**

```python
slippage_points_per_side: Decimal = Field(ge=0)
```

Do not change signed swap rates or the already-positive stress multiplier.

- [ ] **Step 4: Run GREEN and mutation**

Run:

```powershell
$env:UV_SYSTEM_CERTS='1'; uv run pytest tests/unit/research/backtest/test_costs.py tests/unit/research/backtest/test_fills.py -q --no-cov
$env:UV_SYSTEM_CERTS='1'; uv run ruff check src/trading_house/research/backtest/costs.py tests/unit/research/backtest/test_costs.py
$env:UV_SYSTEM_CERTS='1'; uv run mypy
```

Expected: all pass. Temporarily remove `ge=0`; confirm only the new boundary test fails. Restore it.

- [ ] **Step 5: Preserve the diff**

Do not commit. Record the focused test output.

---

## Final verification for this sub-plan

Run after all four task reviews are clean:

```powershell
$env:UV_SYSTEM_CERTS='1'; uv run pytest tests/unit tests/acceptance tests/property -q --no-cov
$env:UV_SYSTEM_CERTS='1'; uv run ruff format --check .
$env:UV_SYSTEM_CERTS='1'; uv run ruff check .
$env:UV_SYSTEM_CERTS='1'; uv run mypy
```

Expected: all pass. Integration tests remain part of original Task 11's final gate.

## Self-Review

- **Spec coverage:** explicit swap, complete session windows, nonnegative slippage, and the approved Task 8 safety subset each map to one task.
- **Scope:** no migration, new option, alternate path, or deferred abstraction is introduced.
- **Type consistency:** the required swap remains `NonNegativeFiniteFloat`; session refusal remains `InsufficientHistoryError`; exit refusal remains `BacktestRefused(RefusalKind.EXIT_POLICY)`.
- **Placeholder scan:** every task names exact files, interfaces, tests, commands, and expected outcomes.
