# Phase 8B2b — Cost Scenario Orchestration and the Declared-Grid Report Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run the cost grid a protocol preregisters, seal all three scenarios, and derive a report that refuses any candidate whose sealed scenarios are not the declared ones.

**Architecture:** No new sealed artifact — the three bundles are the evidence, already sealed and verified. A new pure module derives the grid from the protocol **as it sits in the chain**, not from a file, and holds the five fail-closed checks plus the report model. A second module adds the two commands: an orchestrator that starts, simulates and seals all three levels, and a standalone read. `backtest run` and the orchestrator call one simulation function; `research trial record` and the orchestrator call one seal function.

**Tech Stack:** Python 3.12, Pydantic v2 strict/frozen models, Typer, `Decimal`, pytest, Ruff, strict mypy. No new dependency, no migration, no new ledger event type.

## Global Constraints

- Target Python `>=3.12,<3.13`. `CanonicalModel` semantics: `strict=True`, `frozen=True`, `extra="forbid"`.
- `Decimal` throughout. Never print a "total cost": `swap` is signed, so a summed total means something only given a sign convention.
- Every canonical timestamp is timezone-aware UTC.
- Pydantic validators raise `ValueError`; plain functions raise the typed error.
- **No new sealed artifact, no new field on any model, no new event type, no migration, no dependency, no database privilege.** The three bundles are the evidence.
- **No verdict, threshold, survival rule, or promotion claim** may appear in the report, its keys, or its documentation. It reports; 8D judges, with thresholds fixed in advance.
- The declared grid is read from the **`PREREGISTERED` event in the chain**, never from a file the operator names. A report that validated a hand-supplied protocol would be checking the operator's copy rather than the record.
- Use absolute imports. `ops/` is outside `research/`, so it may import from both; the reverse is not true.
- Run the focused test first, then the quality gates named in each task.

## File Map

### Create

- `src/trading_house/ops/scenarios.py` — `declared_grid`, `scenario_report`, `ScenarioTotals`, `ScenarioDegradation`, `ScenarioReport`, and `registered_protocol` (recovers a `TrialProtocol` from the chain's `PREREGISTERED` event, via `replay()` and **not** `events_for`).
- `tests/unit/ops/test_scenarios.py` — the six checks, each with a failing case.
- `tests/acceptance/test_phase8b2b.py` — the 8B2b acceptance gate.

### Modify

- `src/trading_house/core/errors.py` — `ExitCode.SCENARIO_EVIDENCE = 19` and `ScenarioEvidenceError`.
- `src/trading_house/ops/backtest.py` — `simulate(...)`, the body of `backtest run`'s `operation()` with the stress multiplier as a parameter.
- `src/trading_house/ops/ledger.py` — `seal_bundle(...)`, shared by `research trial record` and the orchestrator.
- `src/trading_house/cli.py` — `EXIT_CODES` entry; `backtest run` delegates to `simulate`; `record` delegates to `seal_bundle`; the two new commands.
- `tests/unit/test_cli.py` — the new command group's contract and the new exit code.
- `tests/integration/research/test_scenarios.py` — real-PostgreSQL round trips for both commands.
- `README.md` — the two commands, the six checks, and what the report does not establish.

### Explicitly unchanged

- `src/trading_house/research/**` — no field, no model, no event, no digest movement.
- `src/trading_house/research/backtest/result.py` — untouched, so the four pinned digests stay put.
- `migrations/`, `pyproject.toml`.

---

### Task 1: The declared grid, the six checks, and the report

**Files:**
- Create: `src/trading_house/ops/scenarios.py`
- Create: `tests/unit/ops/test_scenarios.py`
- Modify: `src/trading_house/core/errors.py`

**Interfaces:**
- Consumes: `TrialProtocol`, `CostSpec`, `CostModel`, `EvidenceBundle`, `ReturnSeriesBasis`, `CostAttributionStatus`, `EquityEvidenceError` for its message register.
- Produces:
  - `registered_protocol(events: Sequence[LedgerRecord], trial_id: str) -> TrialProtocol`
  - `declared_grid(protocol: TrialProtocol) -> tuple[Decimal, ...]`
  - `scenario_report(*, trial_id: str, protocol: TrialProtocol, sealed: Sequence[tuple[str, EvidenceBundle]]) -> ScenarioReport`
  - `ScenarioTotals`, `ScenarioDegradation`, `ScenarioReport`
  - `ExitCode.SCENARIO_EVIDENCE = 19`, `ScenarioEvidenceError`

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/ops/test_scenarios.py`. Every case is built from **real** models — a real
`TrialProtocol`, real `EvidenceBundle`s — not from loose fixtures, because the report's whole value
is that it reads the same documents a reader would. Read
`src/trading_house/ops/scenarios.py`'s consumers for the shapes, and reuse the protocol builder
`tests/acceptance/test_phase8b1.py` uses if you want a realistic one; a small hand-built
`TrialProtocol` with the exact `costs` and `data` is enough and clearer.

The checks, each with a refusing case. **Every tamper below is rebuilt through a constructor or a
JSON round trip, never `model_copy(update=...)`** — a tampered document built by bypassing
validation could be asserting on something the engine would refuse to seal, and the test would
prove nothing.

```python
def test_a_grid_is_read_from_the_protocol_and_never_hard_coded() -> None:
    protocol = _protocol()
    assert declared_grid(protocol) == (Decimal("1"), Decimal("1.5"), Decimal("2"))


def test_a_registration_that_names_no_such_trial_is_refused() -> None:
    # The grid's authority is the sealed registration, so a trial the chain does
    # not name has no grid -- which is what makes an unregistered candidate
    # unreportable rather than merely empty.
    with pytest.raises(ScenarioEvidenceError):
        registered_protocol([_preregistered_event(_protocol())], "trial-nobody-declared")


def test_a_grid_is_recovered_from_a_registration_several_candidates_share() -> None:
    # A registration seals the whole family in one event whose trial_id column is
    # null, so this is the case events_for(trial_id) cannot serve -- and getting
    # it wrong reports a registered candidate as unregistered.
    protocol = _protocol(candidate_ids=("trial-1", "trial-2"))
    recovered = registered_protocol([_preregistered_event(protocol)], "trial-2")

    assert recovered.protocol_id == protocol.protocol_id
    assert [c.trial_id for c in recovered.candidates] == ["trial-1", "trial-2"]


def test_an_incomplete_grid_is_refused() -> None:
    protocol = _protocol()
    sealed = _sealed(protocol, multipliers=(Decimal("1"), Decimal("1.5")))

    with pytest.raises(ScenarioEvidenceError):
        scenario_report(trial_id=_trial_id(protocol), protocol=protocol, sealed=sealed)


def test_a_summary_that_is_not_complete_is_refused() -> None:
    # PARTIAL carries None for the two terms 8B2a added, so the report cannot
    # name four separable costs and must refuse rather than read them as zero.
    protocol = _protocol()
    sealed = _sealed(protocol, summary_status=CostAttributionStatus.PARTIAL)

    with pytest.raises(ScenarioEvidenceError):
        scenario_report(trial_id=_trial_id(protocol), protocol=protocol, sealed=sealed)


def test_a_baseline_that_is_not_the_declared_one_is_refused() -> None:
    protocol = _protocol()
    sealed = _sealed(
        protocol, baseline_overrides={"commission_per_lot_per_side": Decimal("3.50")}
    )

    with pytest.raises(ScenarioEvidenceError):
        scenario_report(trial_id=_trial_id(protocol), protocol=protocol, sealed=sealed)


def test_a_stressed_scenario_that_changed_anything_but_the_multiplier_is_refused() -> None:
    protocol = _protocol()
    sealed = _sealed(
        protocol, stressed_overrides={"slippage_points_per_side": Decimal("0.9")}
    )

    with pytest.raises(ScenarioEvidenceError):
        scenario_report(trial_id=_trial_id(protocol), protocol=protocol, sealed=sealed)


def test_a_scenario_from_another_trial_is_refused() -> None:
    protocol = _protocol()
    sealed = _sealed(protocol, foreign_trial_at=Decimal("1"))

    with pytest.raises(ScenarioEvidenceError):
        scenario_report(trial_id=_trial_id(protocol), protocol=protocol, sealed=sealed)


def test_a_window_the_protocol_did_not_declare_is_refused() -> None:
    protocol = _protocol()
    sealed = _sealed(protocol, window_end=datetime(2025, 6, 1, tzinfo=UTC))

    with pytest.raises(ScenarioEvidenceError):
        scenario_report(trial_id=_trial_id(protocol), protocol=protocol, sealed=sealed)
```

`_sealed(protocol, **overrides)` is the one builder, and the reason these tests can be one fact each.
It runs the real engine and the real attribution per multiplier, pairs each bundle with a digest the
way the chain would, and rebuilds whichever bundle an override names **through
`EvidenceBundle.model_validate(bundle.model_dump_json())` with the change applied to the payload
first** — so the tampered document is one the model accepts and the refusal is the report's, not
Pydantic's.

Every keyword is a field a test above actually uses: `multipliers`, `summary_status`,
`baseline_overrides`, `stressed_overrides`, `foreign_trial_at`, `window_end`. Build it once, at the
top of the file, and let it be long — it is a test helper, and the alternative is eight
near-identical fixture functions. A keyword nobody calls is a keyword to delete.

Plus the report's own contract, which is where a "total cost" would sneak in:

```python
def test_the_report_names_the_grid_it_expected_before_what_it_found() -> None:
    protocol = _protocol()
    report = scenario_report(trial_id=_trial_id(protocol), protocol=protocol, sealed=_sealed(protocol))

    assert report.declared_multipliers == (Decimal("1"), Decimal("1.5"), Decimal("2"))
    assert [s.multiplier for s in report.scenarios] == [Decimal("1"), Decimal("1.5"), Decimal("2")]
    assert not hasattr(report, "total_cost")
    assert not any("total" in key for key in report.model_dump(mode="json"))


def test_swap_is_reported_signed_and_the_degradation_is_a_difference_in_net_pnl() -> None:
    protocol = _protocol()
    report = scenario_report(trial_id=_trial_id(protocol), protocol=protocol, sealed=_sealed(protocol))
    baseline, stressed = report.scenarios[0], report.scenarios[1]

    assert baseline.net_pnl - stressed.net_pnl == report.degradations[0].net_pnl_delta
    # Signed: a charge is negative, a credit positive. Nothing sums the four
    # terms into one figure, which would bury a credit inside a plausible-looking
    # total. The field set is pinned rather than any single value, so a later
    # `total_delta` fails this test rather than laundering the four into one.
    assert stressed.swap < 0
    assert set(ScenarioDegradation.model_fields) == {
        "multiplier",
        "net_pnl_delta",
        "market_pnl_delta",
        "spread_cost_delta",
        "slippage_cost_delta",
        "commission_delta",
        "swap_delta",
    }
```

- [ ] **Step 2: Run and verify failure**

Run: `uv run pytest tests/unit/ops/test_scenarios.py -q --no-cov`

Expected: import failure — `trading_house.ops.scenarios` does not exist.

- [ ] **Step 3: Add the error and its exit code**

In `src/trading_house/core/errors.py`, add `SCENARIO_EVIDENCE = 19` to the `ExitCode` enum after
`EVIDENCE_INTEGRITY = 18`, and:

```python
class ScenarioEvidenceError(TradingHouseError):
    """Raised when a candidate's sealed scenarios are not the ones it declared.

    Five distinct ways that happens — a level missing, a baseline that is not the
    declared one, a stressed level that changed something other than the
    multiplier, a scenario belonging to another candidate, a window the protocol
    did not declare — and one remedy for all of them: this candidate's cost grid
    is not the grid that was preregistered, so nothing downstream may read it as
    one. Distinct from ``EvidenceIntegrityError``, which says a document is
    missing, altered, or not the canonical bytes its digest names; here every
    document verifies and the *set* is wrong.

    The specifics ride on the private cause so an operator can be told which of
    the five they hit, while the public message stays as uninformative as every
    other code here.
    """

    public_message = "sealed scenarios do not match the declared cost grid"
```

The private cause is a `ValueError` carrying the full explanation. Raise it as
`raise ScenarioEvidenceError() from ValueError(detail)`.

- [ ] **Step 4: Create `ops/scenarios.py`**

```python
""The declared cost grid, the six checks a candidate's scenarios must pass,
and the report they produce.

Phase 8B2b. Umbrella 6.4 requires every preregistered candidate to be rerun at
1.0x, 1.5x and 2.0x costs, and 8B2a sealed the per-trade attribution each run
produces. This module is the read over those three sealed bundles.

The grid is taken from the protocol **as the chain holds it**, recovered from the
``PREREGISTERED`` event, rather than from a file the operator names. A report
that validated a hand-supplied protocol would be checking the operator's copy
rather than the record, and the whole point of preregistration is that the
declared grid is the one that was fixed before any result existed.

Nothing here judges. The six checks below are all about whether the evidence
*is* what it claims to be, and all of them fail closed. Whether a candidate
survives its grid is 8D's question, with thresholds fixed in advance; answering
it here would fix a threshold after seeing how the numbers came out.
"""
```

Then the model and the functions:

```python
class ScenarioTotals(CanonicalModel):
    """One scenario's sealed figures, read from its own bundle and never
    recomputed from anything else.

    ``swap`` is signed and is reported signed. A charge is negative and a credit
    positive, and the four cost terms do not all move the same way under stress
    on a carry-earning candidate, so a single "total cost" would hide a credit
    inside a positive-looking number. The relation is
    ``net = market - spread - slippage - commission + swap``.
    """

    multiplier: Decimal
    attempt_id: NonEmptyStr
    evidence_sha256: NonEmptyStr
    """The digest the chain's ``EVIDENCE_SEALED`` event names for this
    scenario's document -- which sealed file this row is talking about. Taken
    from the chain rather than recomputed, because the store is what holds that
    name and re-deriving it would answer a different question."""
    source_result_sha256: NonEmptyStr
    """The digest of the result the bundle carries inline. Distinct from the
    field above and reported beside it: one names the document, the other names
    the run inside it, and a reader verifying the report needs both to get back
    from a number here to the bytes that produced it."""
    trades: NonNegativeInt
    market_pnl: Decimal
    spread_cost: Decimal
    slippage_cost: Decimal
    commission: Decimal
    swap: Decimal
    net_pnl: Decimal


class ScenarioDegradation(CanonicalModel):
    """How one stressed level differs from the baseline, term by term.

    Deltas, not levels, and never a ratio: at 2.0x a candidate's expectancy may
    cross zero, and a percentage of a number that changed sign says nothing.
    """

    multiplier: Decimal
    net_pnl_delta: Decimal
    market_pnl_delta: Decimal
    spread_cost_delta: Decimal
    slippage_cost_delta: Decimal
    commission_delta: Decimal
    swap_delta: Decimal


class ScenarioReport(CanonicalModel):
    """What the declared grid asked for, and what the sealed evidence says.

    ``declared_multipliers`` is first in the field order and in every rendering,
    so a reader meets the preregistration before the outcome.
    """

    trial_id: NonEmptyStr
    spec_sha256: NonEmptyStr
    declared_multipliers: tuple[Decimal, ...]
    scenarios: tuple[ScenarioTotals, ...]
    degradations: tuple[ScenarioDegradation, ...]
```

```python
def declared_grid(protocol: TrialProtocol) -> tuple[Decimal, ...]:
    """The multipliers this protocol preregistered, ascending, baseline first.

    Read from the protocol rather than written down: ``CostSpec`` already holds
    the validator that pins the stressed levels to exactly ``{1.5, 2}``, and a
    constant here would be a second statement of a rule that has one home.
    """

    return tuple(sorted({Decimal(1), *protocol.costs.stress_multipliers}))
```

`CostSpec.stress_grid_is_exact` means the union is belt-and-braces rather than the rule -- the
stressed levels are always `{1.5, 2}`. It is written as a union anyway because "there is always a
baseline at 1" is *this* function's claim, and `CostSpec`'s validator says nothing about whether a
baseline exists.


def registered_protocol(events: Sequence[LedgerEvent], trial_id: str) -> TrialProtocol:
    """The protocol this trial was registered against, out of the chain.

    Recovered from the ``PREREGISTERED`` event's payload rather than from
    anything the caller supplies, so the grid the report checks is the grid that
    was sealed. A trial that was never registered has no such event, and there is
    no honest answer for it.

    Takes the whole chain and not ``ledger.events_for(trial_id)``, which is the
    trap here. A protocol seals its entire candidate family inside one event, so
    the ``PREREGISTERED`` row's ``trial_id`` column is null and
    ``_TRIAL_EVENTS_SQL`` -- ``WHERE trial_id = %s`` -- never returns it. A
    candidate would be reported as unregistered while the ledger's own
    ``declares_trial`` says the opposite. ``replay()`` is the read that carries
    the registration, and it is what ``counters()`` already uses for the same
    reason.
    """

    for event in events:
        if not isinstance(event.payload, PreregisteredPayload):
            continue
        protocol = event.payload.protocol
        if any(candidate.trial_id == trial_id for candidate in protocol.candidates):
            return protocol
    raise ScenarioEvidenceError() from ValueError(f"trial {trial_id} has no preregistered protocol")
```

`scenario_report` is the body's substance. Its order is the order the refusals must fire in:

```python
def scenario_report(
    *, trial_id: str, protocol: TrialProtocol, sealed: Sequence[tuple[str, EvidenceBundle]]
) -> ScenarioReport:
    """Check a candidate's sealed scenarios against its declared grid, and report.

    ``sealed`` pairs each bundle with the evidence digest the chain's
    ``EVIDENCE_SEALED`` event names for it. The pairing is the caller's because
    the chain is where the digest lives, and taking the documents alone would
    leave the report unable to say which sealed file any of its numbers came
    from.

    Six checks, in this order, so an incomplete candidate is refused for the
    first reason that applies rather than for a later one it also happens to
    break:

    1. **Completeness** — every declared multiplier present exactly once.
    2. **Attribution** — every scenario carries a ``COMPLETE`` ``CostSummary``
       and a ``cost_attribution``, because the four separable terms and
       ``market_pnl`` are read from them and a ``PARTIAL`` summary has two of
       them as ``None``.
    3. **Baseline fidelity** — the ``1.0`` scenario's ``cost_model`` equals the
       protocol's ``CostSpec.baseline`` exactly.
    4. **Scenario fidelity** — each stressed scenario differs from the baseline
       in ``stress_multiplier`` and nothing else.
    5. **Window fidelity** — every scenario's ``result.start``/``end`` equals the
       window ``protocol.data`` declared.
    6. **Identity** — one candidate: same ``trial_id``, ``spec_sha256``,
       ``strategy_id``/``strategy_version``, ``bars_seen``, and the same ordered
       ``proposal_id``s.

    Check 6 pins the trade *sequence* and says nothing about prices, because the
    prices must differ: scaling the spread is the stress, and 8B2a measured
    404.4 -> 606.6 of it across the same twelve trades. A report that pinned
    either the prices or the sequence as equal would be wrong in one direction
    or the other.
    """

    grid = declared_grid(protocol)
    by_multiplier: dict[Decimal, list[tuple[str, EvidenceBundle]]] = {}
    for evidence_sha256, bundle in sealed:
        by_multiplier.setdefault(bundle.result.cost_model.stress_multiplier, []).append(
            (evidence_sha256, bundle)
        )

    _refuse_completeness(grid, by_multiplier, trial_id)
    pairs = [by_multiplier[m][0] for m in grid]
    bundles = [bundle for _, bundle in pairs]
    _refuse_attribution(bundles)
    _refuse_baseline_fidelity(protocol, bundles)
    _refuse_scenario_fidelity(protocol, bundles)
    _refuse_window_fidelity(protocol, bundles)
    _refuse_identity(trial_id, bundles)

    scenarios = tuple(
        _totals(m, bundle, evidence_sha256)
        for m, (evidence_sha256, bundle) in zip(grid, pairs, strict=True)
    )
    baseline = scenarios[0]
    degradations = tuple(
        ScenarioDegradation(
            multiplier=s.multiplier,
            net_pnl_delta=baseline.net_pnl - s.net_pnl,
            market_pnl_delta=s.market_pnl - baseline.market_pnl,
            spread_cost_delta=s.spread_cost - baseline.spread_cost,
            slippage_cost_delta=s.slippage_cost - baseline.slippage_cost,
            commission_delta=s.commission - baseline.commission,
            swap_delta=s.swap - baseline.swap,
        )
        for s in scenarios[1:]
    )
    return ScenarioReport(
        trial_id=trial_id,
        # Taken from the first bundle because ``_refuse_identity`` has already
        # established that all three agree; a report that read it from the
        # protocol instead would be asserting a cross-check the chain does not
        # make. The candidate's ``TrialSpec`` has no digest in the ledger -- the
        # ``spec_sha256`` column is the operator's declared value, unvouched by
        # 8A -- so this reports what the evidence says, not what was intended.
        spec_sha256=bundles[0].spec_sha256,
        declared_multipliers=grid,
        scenarios=scenarios,
        degradations=degradations,
    )
```

Each `_refuse_*` is a small function that raises `ScenarioEvidenceError() from ValueError(detail)`
naming both what it found and what it expected — a refusal an operator cannot act on is one they
will route around. The six are:

| Check | Compares | Refuses when |
| --- | --- | --- |
| completeness | `{m for m in by_multiplier}` against `grid` | a declared level is missing, or one appears twice |
| attribution | `b.costs.status`, `b.cost_attribution` | a summary is not `COMPLETE`, or the split is absent |
| baseline fidelity | `ordered[0].result.cost_model` == `protocol.costs.baseline` | any term differs |
| scenario fidelity | each `cost_model` against `baseline` with `stress_multiplier` replaced | any other field differs |
| window fidelity | `b.result.start`/`end` against `protocol.data.start`/`end` | either differs |
| identity | `trial_id`, `spec_sha256`, `strategy_id`, `strategy_version`, `bars_seen`, ordered `proposal_id`s | any differs across the set, or a bundle names another trial |

`_refuse_scenario_fidelity` is written as a copy-then-replace rather than as four field
comparisons, because "the only difference is the multiplier" is the property and a hand-written
list of the five other fields would be a second statement of `CostModel`'s shape:

```python
def _refuse_scenario_fidelity(protocol: TrialProtocol, bundles: Sequence[EvidenceBundle]) -> None:
    """Every stressed scenario is the baseline at its own multiplier, and nothing else.

    Written as a copy-then-replace rather than as a list of the five fields that
    must match, because "the only difference is the multiplier" is the property
    and a hand-written field list is a second statement of ``CostModel``'s
    shape -- one that would keep passing, and quietly, if a sixth term were ever
    added. A new term is then compared here for free.
    """

    baseline = protocol.costs.baseline
    for bundle in bundles[1:]:
        multiplier = bundle.result.cost_model.stress_multiplier
        expected = baseline.model_copy(update={"stress_multiplier": multiplier})
        if bundle.result.cost_model != expected:
            raise ScenarioEvidenceError() from ValueError(
                f"scenario {multiplier} declares {bundle.result.cost_model}; "
                f"the protocol's baseline at that multiplier is {expected}"
            )
```

`model_copy(update=...)` is the right tool *here* and the wrong one in the tests below: it bypasses
validation, which is what this comparison wants -- the question is whether the bundle's declared
costs match the protocol's, and a copy that re-validated would raise instead of comparing. The
tests build their tampered bundles through a constructor or a JSON round trip for the opposite
reason, so a test cannot accidentally assert on a document the engine would refuse to seal.

`_totals` reads only fields the bundle already sealed and checked:

```python
def _totals(multiplier: Decimal, bundle: EvidenceBundle, evidence_sha256: str) -> ScenarioTotals:
    """One scenario's own sealed figures, read rather than recomputed.

    ``commission`` and ``swap`` come from the ``CostSummary``; so do
    ``spread_cost`` and ``slippage_cost``, which are ``None`` on a ``PARTIAL``
    summary and are only non-``None`` because ``_refuse_attribution`` has
    already required ``COMPLETE``. ``market_pnl`` has no result-level field at
    all -- it exists only on ``TradeCostAttribution`` -- so it is summed over
    the split, whose per-trade ``post_fill_gross`` check and whose parallelism
    to ``result.trades`` are the bundle's own validator's work, not this
    function's.

    Nothing is recomputed from anything. Every figure here is a field the bundle
    sealed and its own validators checked, or a digest the chain already holds;
    a report that re-derived them would be a second implementation of the
    engine, and a disagreement between the two would be unresolvable.
    """

    costs = bundle.costs
    attribution = bundle.cost_attribution
    assert costs.spread_cost is not None and costs.slippage_cost is not None
    assert attribution is not None
    return ScenarioTotals(
        multiplier=multiplier,
        attempt_id=bundle.attempt_id,
        evidence_sha256=evidence_sha256,
        source_result_sha256=bundle.source_result_sha256,
        trades=len(bundle.result.trades),
        market_pnl=sum((t.market_pnl for t in attribution.trades), Decimal(0)),
        spread_cost=costs.spread_cost,
        slippage_cost=costs.slippage_cost,
        commission=costs.commission,
        swap=costs.swap,
        net_pnl=bundle.result.net_pnl,
    )
```

The two `assert`s are unreachable by construction, and that is their whole job: they narrow
`Decimal | None` and `CostAttribution | None` for mypy where the refusal above has already
established the invariant. They are not the check -- `_refuse_attribution` is, and it raises a typed
error an operator can act on rather than an `AssertionError` they cannot. If either assert ever
fires, `_refuse_attribution` has a hole and that is a bug in the check, not in the bundle.

- [ ] **Step 5: Run the tests and the gates**

Run:
```powershell
uv run pytest tests/unit/ops/test_scenarios.py -q --no-cov
uv run pytest tests/unit/ -q --no-cov
uv run ruff format src/trading_house/ops/scenarios.py tests/unit/ops/test_scenarios.py
uv run ruff check .
uv run mypy
```

Expected: all pass. `tests/unit/test_cli.py` will fail on the new unmapped error type until
Task 3 adds the `EXIT_CODES` entry — that is expected at this point and is Task 3's line.

- [ ] **Step 6: Commit the checks**

```powershell
git add src/trading_house/ops/scenarios.py src/trading_house/core/errors.py tests/unit/ops/test_scenarios.py
git commit -m "feat: check a candidate's sealed scenarios against its declared cost grid"
```

---

### Task 2: Factor the simulation and the seal so the two commands share them

**Files:**
- Modify: `src/trading_house/ops/backtest.py`
- Modify: `src/trading_house/ops/ledger.py`
- Modify: `src/trading_house/cli.py`

**Interfaces:**
- Consumes: everything Task 1 produced (unused here, but this is the refactor Task 3 builds on).
- Produces: `simulate(...)` in `ops/backtest.py`; `seal_bundle(...)` in `ops/ledger.py`.

This task changes no behaviour. It is separated from Task 3 because its whole risk is "did the
refactor change `backtest run`?", and that question is cleanly answerable against the command's
existing tests in isolation.

- [ ] **Step 1: Write the equivalence test first**

The S-4 guarantee, stated as a test before the refactor: `backtest run`'s own output must be
unchanged by the factoring. In `tests/integration/research/test_backtest_evidence.py` there is
already a case pinning the non-flag payload's exact key set and that its `digest` equals the
same run's bundle `source_result_sha256`. Note its `result.digest()` before touching anything:

```powershell
uv run pytest tests/integration/research/test_backtest_evidence.py -q --no-cov
```

Record the digest the existing test asserts. After the refactor it must be the same number, not a
re-derived one.

- [ ] **Step 2: Extract `simulate`**

Move the body of `backtest_run`'s `operation()` into `src/trading_house/ops/backtest.py` as:

```python
def simulate(
    request: BacktestRequest,
    *,
    bars: BarReader,
    contract: InstrumentContract,
    constitution: LoadedConstitution,
) -> BacktestOutcome:
    """One run, with the cost stress already carried by ``request``.

    ``BacktestRequest.cost_model.stress_multiplier`` is the only thing that
    distinguishes a baseline run from a stressed one, so the scenario level is
    not a parameter of this function. It is inside the request, which is where
    the rest of the run's declared costs live and where ``digest()`` can see it.

    ``backtest run`` and ``research trial scenarios`` both call this, which is
    the point: two entry points that computed a run their own way would produce
    two different results for one declared input, and the comparison between
    them would be comparing two simulators rather than two cost levels.

    The body is ``build_backtester(...).run(request)`` and nothing else --
    ``build_backtester`` is already here, and already the single construction
    point for the engine. This function exists so that the *call* is shared, not
    so the engine is re-wrapped.
    """

    return build_backtester(
        bars=bars, contract=contract, constitution=constitution
    ).run(request)
```

`bars` is typed `BarReader`, not `BarStore`: `build_backtester` declares a `BarReader` and
`ops/backtest.py` imports it from `trading_house.features.engine`. Naming the wider type is what
lets the CLI pass a `PostgresBarStore` without an import it does not otherwise need.

`backtest_run`'s `operation()` keeps building the `CostModel` (the multiplier is the operator's
option) and the `BacktestRequest`, then calls `simulate(request, bars=_bar_store(),
contract=instrument_contract, constitution=loaded_constitution)`. Nothing else in that function
moves, and `LoadedConstitution` is already imported by this module.

- [ ] **Step 3: Extract `seal_bundle`**

Move the evidence write and the two appends out of `research_trial_record` into
`src/trading_house/ops/ledger.py`:

```python
def seal_bundle(
    bundle: EvidenceBundle,
    *,
    ledger: TrialLedger,
    store: EvidenceStore,
) -> str:
    """Write one bundle and append the two events that reference it.

    Returns the evidence digest, which is the address the bundle now has.

    ``research trial record`` and ``research trial scenarios`` both call this.
    An orchestrator that sealed by a second route would be able to produce a
    bundle the chain does not point at, or an event whose digest is not the one
    on disk -- and neither would be noticed, because both sides would look
    complete on their own.

    The order is write-then-append, so a failed append leaves a document nothing
    references rather than a row whose document is missing. The unreferenced side
    of that pair is the recoverable one.
    """

    stored = store.write(bundle)
    ledger.append(result_recorded_event(bundle))
    ledger.append(evidence_sealed_event(bundle, stored.sha256))
    return stored.sha256
```

`research_trial_record` keeps loading the bundle from disk, keeps the
`trial_id`/`attempt_id` consistency check against the bundle, and then calls `seal_bundle`.

- [ ] **Step 4: Prove `backtest run` is unchanged**

Run:
```powershell
uv run pytest tests/integration/research/ -q --no-cov
uv run pytest tests/unit/ -q --no-cov
uv run ruff format --check .
uv run ruff check .
uv run mypy
```

Expected: green, and the digest recorded in Step 1 still asserted. That test is the whole point of
this task — if it needed updating, the refactor changed a command rather than moving a body.

- [ ] **Step 5: Commit the refactor**

```powershell
git add src/trading_house/ops/backtest.py src/trading_house/ops/ledger.py src/trading_house/cli.py
git commit -m "refactor: give the backtest and the trial commands one simulation and one seal"
```

---

### Task 3: Add the orchestrator and the standalone read

**Files:**
- Modify: `src/trading_house/cli.py`
- Test: `tests/unit/test_cli.py`
- Test: `tests/integration/research/test_scenarios.py`

**Interfaces:**
- Consumes: `simulate`, `seal_bundle` (Task 2), `scenario_report`, `registered_protocol`, `declared_grid` (Task 1), the existing `_trial_ledger()`, `_evidence_store()`, `mark_to_market_bundle`.
- Produces: `research trial scenarios`, `research trial scenario-report`, and the `EXIT_CODES` entry.

- [ ] **Step 1: Write the failing CLI and integration tests**

In `tests/unit/test_cli.py`, add the exit-code case next to
`test_equity_evidence_error_maps_to_its_own_exit_code`, following its shape — the mapping assertion,
then the literal pinned, because that file's convention is that a code an operator's script branches
on is pinned as a number and not merely as a member:

```python
def test_scenario_evidence_error_maps_to_its_own_exit_code() -> None:
    """The 19 a wrong cost grid earns, pinned as a number.

    Distinct from 17 because every document verified: ``EvidenceIntegrityError``
    says a bundle is missing, altered, or not the canonical bytes its digest
    names, and a candidate whose three scenarios are all intact but are not the
    three its registration declared is a different operator problem with a
    different remedy.
    """

    assert cli.EXIT_CODES[ScenarioEvidenceError] is cli.ExitCode.SCENARIO_EVIDENCE
    assert int(cli.ExitCode.SCENARIO_EVIDENCE) == 19
```

`test_every_typed_error_has_a_stable_exit_code` asserts `_concrete_error_types() <= set(cli.EXIT_CODES)`
and will fail from Task 1 onward, until this mapping lands. That is expected, and it is the check
that catches a new error type nobody gave an exit code.

Create `tests/integration/research/test_scenarios.py`. Reuse the module-level `seeded` fixture and
the `_args`/`_run` shape from `test_backtest_evidence.py` by importing them
(`from tests.integration.research.test_backtest_evidence import _args, _run, seeded`) — the
alternative is a third copy of the fourteen-option invocation that will drift from the command's
signature, and the S-4 test below is meaningless unless both sides type the *same* options. Reuse
`research_env` and `isolated_research_ledger` from that directory's `conftest.py`; do not redefine
either, which the file's own header explains.

Cover:

1. `research trial scenarios` on a registered protocol produces three attempts named
   `<prefix>-1.0`, `<prefix>-1.5`, `<prefix>-2.0`, seals three bundles, and reports all three.
2. The orchestrator's `1.0` scenario and a hand-run `backtest run --mark-to-market
   --stress-multiplier 1` produce the same `source_result_sha256`. This is the S-4 guarantee at the
   level an operator experiences, and it is the one test in this file that must not be weakened:
   assert the digests are equal, not merely that both runs succeeded.
3. Re-running the same prefix is idempotent: no new attempts, no new evidence, same three digests.
   This works only because every timestamp is a required option rather than a clock read — say so
   in the test, because it is the reason the command has twelve required options.
4. `research trial scenario-report` on a healthy candidate reports the declared grid and all three.
5. A candidate with one scenario removed — delete one sealed bundle from the evidence root before
   the read — is refused with exit 19. The store's `read` raises `EvidenceIntegrityError` on a
   missing document *before* the report sees a set to be incomplete, so this asserts exit **17** and
   the reason it is not 19: a document that is not there is an integrity failure, and a set that is
   missing a level is a different one. Both are refusals; the codes must not blur.
6. A **present** but wrong set is the 19 case: seal all three, then over-seal a fourth attempt at
   `1.0` for a different trial so `events_for` returns a document naming another candidate. Refused
   on identity at 19.
7. `research trial count` reports three more `audit_attempts`, **no** more
   `selection_lotteries`, and **no** more `effective_specifications`. This is the counters
   consequence and it is the test that would catch an orchestrator that minted a trial id per
   multiplier.
8. Every refusal names what it found and what it expected — assert on `result.exception.__cause__`
   text, since the public message is deliberately uninformative.

- [ ] **Step 2: Run and verify failure**

Run: `uv run pytest tests/unit/test_cli.py tests/integration/research/test_scenarios.py -q --no-cov`

Expected: failures — the commands and the exit code do not exist.

- [ ] **Step 3: Add the exit-code mapping**

In `cli.py`'s `EXIT_CODES`, after the `EquityEvidenceError` line:

```python
    ScenarioEvidenceError: ExitCode.SCENARIO_EVIDENCE,
```

- [ ] **Step 4: Add the orchestrator**

```python
@trial_app.command("scenarios")
def research_trial_scenarios(
    protocol: Annotated[Path, typer.Option("--protocol", help="Frozen TrialProtocol JSON, the grid source.")],
    trial_id: Annotated[str, typer.Option("--trial-id")],
    attempt_prefix: Annotated[str, typer.Option("--attempt-prefix", help="Attempt ids are this prefix plus the multiplier.")],
    started_at: Annotated[datetime, typer.Option("--started-at")],
    occurred_at: Annotated[datetime, typer.Option("--occurred-at")],
    registered_at: Annotated[datetime, typer.Option("--registered-at")],
    agent_run_id: Annotated[str, typer.Option("--agent-run-id")],
    strategy: Annotated[str, typer.Option("--strategy")],
    exit_policy: Annotated[ExitPolicyName, typer.Option("--exit-policy")],
    start: Annotated[datetime, typer.Option("--start")],
    end: Annotated[datetime, typer.Option("--end")],
    firm_equity: Annotated[str, typer.Option("--firm-equity")],
    contract: Annotated[Path, typer.Option("--contract")],
    atr_period: Annotated[int, typer.Option("--atr-period", min=1)],
    spread_window: Annotated[int, typer.Option("--spread-window", min=1)],
    commission_per_lot_per_side: Annotated[str, typer.Option("--commission-per-lot-per-side")],
    slippage_points_per_side: Annotated[str, typer.Option("--slippage-points-per-side")],
    swap_long_points_per_day: Annotated[str, typer.Option("--swap-long-points-per-day")],
    swap_short_points_per_day: Annotated[str, typer.Option("--swap-short-points-per-day")],
    triple_swap_weekday: Annotated[int, typer.Option("--triple-swap-weekday")],
    defective_bar_tolerance: Annotated[str, typer.Option("--defective-bar-tolerance")] = "0",
) -> None:
    """Run, seal and compare the cost grid this protocol preregistered.

    Every level is one attempt, because §5.6 counts a distinct ``attempt_id``
    as an audit attempt and a distinct ``trial_id`` as a selection lottery: one
    candidate examined at three cost levels is three attempts and **one**
    selection, and the selection count is what DSR divides by.

    The grid comes from ``--protocol`` here and from the chain in
    ``scenario-report``. The two must agree, and ``scenario-report`` re-derives
    it from the sealed registration, so a protocol file edited after the fact
    cannot widen what the report will accept.

    Every level shares one ``--spec-sha256``-free specification digest, computed
    from the protocol's own candidate rather than typed three times. Three
    hand-typed digests is exactly how the machine that verified 8B1 and 8B2a
    ended up with ``trial-1`` counting two effective specifications: the ledger
    faithfully recorded what it was told, and nothing checked it. Here there is
    nothing to mistype.
    """

    def operation() -> dict[str, JsonValue]:
        parsed = _load_json_model(protocol, TrialProtocol)
        # No ``candidate_for``: ``TrialProtocol`` has no such method, and its
        # candidates are a tuple of ``TrialSpec`` reached by comprehension. An
        # unknown ``--trial-id`` is a ``ConfigurationError``, not a bare
        # ``StopIteration`` the catch-all would answer with a correlation id.
        spec_sha256 = canonical_sha256(_candidate(parsed, trial_id))
        settings = _settings()
        ledger = _trial_ledger()
        store = _evidence_store()
        constitution = load_constitution(
            settings.constitution_path,
            settings.constitution_signature_path,
            settings.constitution_public_key_path,
        )
        instrument_contract = _instrument_contract(contract)
        scenarios: list[JsonValue] = []
        for multiplier in declared_grid(parsed):
            attempt_id = f"{attempt_prefix}-{multiplier}"
            ledger.append(
                execution_started_event(trial_id, attempt_id, spec_sha256, _as_utc(started_at))
            )
            request = _backtest_request(
                strategy=strategy,
                exit_policy=_exit_policy(exit_policy),
                start=start,
                end=end,
                firm_equity=_decimal(firm_equity),
                cost_model=CostModel(
                    commission_per_lot_per_side=_decimal(commission_per_lot_per_side),
                    slippage_points_per_side=_decimal(slippage_points_per_side),
                    swap_long_points_per_day=_decimal(swap_long_points_per_day),
                    swap_short_points_per_day=_decimal(swap_short_points_per_day),
                    triple_swap_weekday=triple_swap_weekday,
                    stress_multiplier=multiplier,
                ),
                atr_period=atr_period,
                spread_window=spread_window,
                defective_bar_tolerance=_unit_interval_decimal(defective_bar_tolerance),
            )
            outcome = simulate(
                request,
                bars=_bar_store(),
                contract=instrument_contract,
                constitution=constitution,
            )
            bundle = mark_to_market_bundle(
                outcome,
                trial_id=trial_id,
                attempt_id=attempt_id,
                spec_sha256=spec_sha256,
                agent_run_id=agent_run_id,
                occurred_at=_as_utc(occurred_at),
                registered_at=_as_utc(registered_at),
            )
            digest = seal_bundle(bundle, ledger=ledger, store=store)
            scenarios.append(
                {
                    "multiplier": str(multiplier),
                    "attempt_id": attempt_id,
                    "evidence_sha256": digest,
                }
            )
        return {
            "trial_id": trial_id,
            "scenarios": scenarios,
            "report": cast(JsonValue, json.loads(_scenario_report_for(trial_id, ledger, store).model_dump_json())),
        }

    _run(operation)
```

**Three extractions, all required and all in this task** because this task is what creates the
second caller. Each is lifted whole out of `backtest run`'s `operation()`, and `backtest run` is
rewritten to call them, so neither command has its own copy of a thing that must not differ:

```python
def _candidate(protocol: TrialProtocol, trial_id: str) -> TrialSpec:
    """The one candidate a trial names, or a refusal naming what was asked for.

    ``StopIteration`` would be the natural failure of a bare ``next(...)`` and
    the wrong one here: it is not an error the catch-all may report as an
    internal fault, and the remedy is the operator passing the ``--trial-id``
    the protocol actually declares.
    """

    for candidate in protocol.candidates:
        if candidate.trial_id == trial_id:
            return candidate
    raise ConfigurationError()


def _instrument_contract(contract: Path) -> InstrumentContract:
    """The contract file, decoded and checked against the one instrument.

    The ``try``/``except`` and the ``_BACKTEST_INSTRUMENT`` comparison move out
    of ``backtest run`` unchanged, comment and all. ``--contract`` has no
    producer in this repo, so a hand-written trailing comma is the likeliest
    mistake either command will see and both must answer it at exit 2.
    """

    try:
        instrument_contract = InstrumentContract.model_validate(
            json.loads(contract.read_text(encoding="utf-8")), strict=False
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ConfigurationError() from error
    if instrument_contract.instrument_id != _BACKTEST_INSTRUMENT:
        raise ConfigurationError()
    return instrument_contract


def _backtest_request(
    *,
    strategy: str,
    exit_policy: ExitPolicyName,
    start: datetime,
    end: datetime,
    firm_equity: Decimal,
    cost_model: CostModel,
    atr_period: int,
    spread_window: int,
    defective_bar_tolerance: Decimal,
) -> BacktestRequest:
    """The run's declared inputs, with every cost already in ``cost_model``.

    Takes a built ``CostModel`` rather than the six cost options, because the
    orchestrator's only difference from ``backtest run`` is where the multiplier
    came from -- its grid rather than a typed ``--stress-multiplier``. Taking the
    model makes that the single difference and keeps the rest of the request
    construction out of the loop.

    The ``try``/``except ValueError`` moves out of ``backtest run`` unchanged:
    ``BacktestRequest`` validates ``firm_equity`` in ``__post_init__`` and
    raises a bare ``ValueError``, so without it a mistyped equity escapes as
    "unexpected failure".
    """

    try:
        return BacktestRequest(
            strategy=build_strategy(strategy, exit_policy=_exit_policy(exit_policy)),
            instrument_id=_BACKTEST_INSTRUMENT,
            timeframe=_BACKTEST_TIMEFRAME,
            start=_as_utc(start),
            end=_as_utc(end),
            firm_equity=firm_equity,
            cost_model=cost_model,
            atr_period=atr_period,
            spread_window=spread_window,
            defective_bar_tolerance=defective_bar_tolerance,
        )
    except ValueError as error:
        raise ConfigurationError() from error
```

Note what the orchestrator does **not** take: no `--mark-to-market`, no `--trial-id`/`--attempt-id`/
`--spec-sha256` pair, and no `--stress-multiplier`. All four belong to the hand-run path. An
orchestrator that accepted them would be accepting three ways to get the same three runs, and the
one that widens the grid is the one an operator would reach for by mistake.

- [ ] **Step 5: Add the standalone read**

```python
@trial_app.command("scenario-report")
def research_trial_scenario_report(
    trial_id: Annotated[str, typer.Option("--trial-id")],
) -> None:
    """Check one candidate's sealed scenarios against its declared grid, and report.

    The grid is read from the ``PREREGISTERED`` event in the chain, not from a
    file, so what is checked is what was sealed before any result existed.

    Reports; does not judge. There is no survival verdict here and no
    threshold: whether a candidate's degradation is acceptable is 8D's
    promotion gate, with its thresholds fixed in advance.
    """

    def operation() -> dict[str, JsonValue]:
        report = _scenario_report_for(trial_id, _trial_ledger(), _evidence_store())
        return cast(JsonValue, json.loads(report.model_dump_json()))

    payload = _execute(operation)
    _emit(payload)
```

```python
def _scenario_report_for(
    trial_id: str, ledger: PostgresTrialLedger, store: EvidenceStore
) -> ScenarioReport:
    """One candidate's report, from the chain and the evidence store alone.

    Two different reads, and the difference is the whole point. The bundles come
    from ``events_for(trial_id)``, which is right for them: an
    ``EVIDENCE_SEALED`` row does carry the trial's id, so one trial's sealed
    documents are exactly the rows that query returns. The protocol does not:
    a ``PREREGISTERED`` row's ``trial_id`` is null, because one event seals a
    whole candidate family, so ``_TRIAL_EVENTS_SQL`` -- ``WHERE trial_id = %s``
    -- never returns it. ``replay()`` is the read that carries the registration.

    Both are used here rather than choosing one, because getting it wrong fails
    quietly in the direction that matters: from ``events_for`` alone every
    registered candidate would be reported as never registered, and the operator
    would be told to go preregister a trial they already preregistered.

    Each digest is read through ``store.read``, which re-serialises what it
    decoded and refuses any document whose bytes are not today's canonical
    encoding. A report is therefore a read of bytes that were verified on the way
    in, not of objects a caller assembled.
    """

    sealed: list[tuple[str, EvidenceBundle]] = []
    for record in ledger.events_for(trial_id):
        if record.event_type is not LedgerEventType.EVIDENCE_SEALED:
            continue
        evidence_sha256 = EvidenceSealedPayload.model_validate(record.event_json).evidence_sha256
        sealed.append((evidence_sha256, store.read(evidence_sha256)))
    return scenario_report(
        trial_id=trial_id,
        protocol=registered_protocol(ledger.replay(), trial_id),
        sealed=sealed,
    )
```

`record.event_json` is the chain's stored payload — a `dict[str, JsonValue]` on the row — and
`EvidenceSealedPayload` validates it, so the digest is read through the model rather than out of a
dict: a malformed row is a `ValidationError` from the ledger's own schema rather than a `KeyError`
from here.

A missing document fails earlier and differently. `store.read` raises `EvidenceIntegrityError` on a
digest with no file, so a candidate whose grid is incomplete *because a bundle was deleted* exits
**17**, not 19 — and that is correct. A document that is not there is an integrity failure with a
different remedy (restore it from backup) than a set that is missing a declared level (run the
level). Task 3's integration tests pin both codes separately, because a 19 that also covered a
deleted file would send an operator to run a scenario they should be restoring.

This helper is the *only* path either command uses to build a report, so the orchestrator's own
output and a hand-built candidate's are checked by identical code. That matters for the S-4 case:
the orchestrator returns the report it just produced, and if that report came from a different code
path than the standalone read's, the equality assertion would be comparing two implementations.

- [ ] **Step 6: Run the CLI and integration tests**

Run:
```powershell
uv run pytest tests/unit/test_cli.py -q --no-cov
uv run pytest tests/integration/research/ -q --no-cov
uv run ruff format --check .
uv run ruff check .
uv run mypy
```

Expected: green, including the S-4 digest-equality case.

- [ ] **Step 7: Commit the commands**

```powershell
git add src/trading_house/cli.py tests/unit/test_cli.py tests/integration/research/test_scenarios.py
git commit -m "feat: run and report a candidate's declared cost grid"
```

---

### Task 4: Close the acceptance and documentation gates

**Files:**
- Create: `tests/acceptance/test_phase8b2b.py`
- Modify: `README.md`
- Test: `tests/acceptance/test_architecture.py` (a new `ops/` import set may need an allowlist entry)

**Interfaces:**
- Consumes: everything Tasks 1-3 produced.
- Produces: the 8B2b acceptance gate and the operator documentation.

- [ ] **Step 1: Write the acceptance gate**

Create `tests/acceptance/test_phase8b2b.py`, importing the real `seeded` fixture and `_args`/`_run`
from `test_backtest_evidence.py` the same way Task 3 does:

- the declared grid is `1.0, 1.5, 2.0` for the real protocol the fixture registers, and is read
  from the chain;
- a real three-scenario candidate passes all six checks and reports all three;
- each of the six checks has a refusing case through a **real sealed bundle** in a real evidence
  root, not a hand-built model;
- the report contains no key whose name contains `total`, `verdict`, `surviv`, `threshold`, or
  `promote` — asserted over `ScenarioReport.model_dump(mode="json")` recursively, keys and values;
- the degradation equals the difference between two sealed bundles' `net_pnl`, and the two bundles'
  own sealed figures are what the report prints (a reader must be able to re-add them);
- the orchestrator's 1.0x and a hand-run 1.0x agree on `source_result_sha256`;
- `research trial count` moved `audit_attempts` by three and the other two counters by nothing;
- the four pinned Phase 7 result digests and `_CANONICAL_BUNDLE_SHA256` are unmoved;
- the report's own `declared_multipliers` equals the grid **recovered from the chain**, not from the
  protocol file passed to `scenarios` — the case where those two disagree is the reason the
  orchestrator is not merely convenient, and it deserves a test rather than a paragraph;
- no verdict, threshold, survival, or promotion word appears in the two commands' `--help` text.

The `--help` check is the gate worth having: the framework's premise is that no slice fixes a
threshold after seeing results, and a test that greps the operator-facing text for a verdict is the
only thing that keeps 8B2b from quietly becoming one. Read the help through Typer's
`runner.invoke(cli.app, ["research", "trial", "scenarios", "--help"])` and check `result.stdout`.

The help check has a false-positive risk worth naming while writing it: `scenario-report`'s own
docstring says "does not judge", and a naive substring search for "judge" or "verdict" would then
fail on the sentence that is *promising* not to judge. Match on the words that would constitute a
verdict — `surviv`, `threshold`, `promote`, `recommend`, `approve`, `reject` — and not on
`judge` or `verdict` themselves, or write the docstring to avoid the vocabulary. Decide which and
leave a comment saying which; a gate that must be relaxed the first time somebody rewords a
docstring is not a gate.

- [ ] **Step 2: Check the architecture guard**

Run: `uv run pytest tests/acceptance/test_architecture.py -q --no-cov`

`ops/scenarios.py` imports from both `research/` and `research/backtest/`. `ops/` is outside the
`research/` subtree, so the existing allowlists should already permit it. If the guard fails, the
import set is wrong — do not widen an allowlist to make it pass.

- [ ] **Step 3: Update the README**

Add, in the Phase 8B2 section or a new 8B2b one:

- the two commands, with one example invocation each, in the operator command table;
- the six checks, and that each fails closed with what it found and what it expected;
- the counters consequence: three attempts, one selection lottery, and why that matters for DSR;
- that the report is **evidence-checked and performance-reported**, and states no verdict — a
  `net_pnl` of +350.48 at 1.0x and −20.22 at 1.5x is a fact about two runs, and whether it
  refutes a candidate is the promotion gate's question with thresholds fixed in advance;
- that the four cost terms are reported separately and never summed, because `swap` is signed;
- the shared-`run_id` limit and the unvouched `spec_sha256`, and that the orchestrator is what
  guarantees the three levels share one declared specification;
- that orchestrating three runs does **not** establish the trade sequence is cost-invariant — it is
  on today's engine, and 8B3's compounding path makes sizing cost-sensitive;
- that a deleted sealed bundle exits 17 and a wrong-but-present set exits 19, and why the two
  remedies differ — restore the document versus run the missing level.

- [ ] **Step 4: Run the acceptance, property, and architecture checks**

Run:
```powershell
uv run pytest tests/acceptance/ tests/property/ -q --no-cov
uv run ruff format --check .
uv run ruff check .
uv run mypy
```

Expected: green.

- [ ] **Step 5: Commit the gates**

```powershell
git add tests/acceptance/test_phase8b2b.py README.md
git commit -m "test: close the phase 8b2b acceptance and documentation gates"
```

---

### Task 5: Verify the slice

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

- [ ] **Step 2: Run the complete container-backed suite**

```powershell
$env:UV_SYSTEM_CERTS = "1"
uv run pytest
```

Expected: green at or above the 95% coverage floor. If Docker is unavailable, stop and report that
the integration gate was not run; do not claim 8B2b complete.

- [ ] **Step 3: Drive the real three-scenario flow**

With both databases migrated, a bar store holding bars, and the DSNs exported:

```powershell
research trial register --protocol protocol.json
research trial scenarios --protocol protocol.json --trial-id trial-1 `
    --attempt-prefix grid-1 --started-at 2026-09-27T12:00:00 `
    --occurred-at 2026-09-27T12:30:00 --registered-at 2026-09-27T12:00:00 `
    --agent-run-id run-1 --strategy session_momentum_eurusd --exit-policy none `
    --start 2024-01-01T00:00:00 --end 2024-06-01T00:00:00 --firm-equity 100000 `
    --contract eurusd-contract.json --atr-period 2 --spread-window 10 `
    --commission-per-lot-per-side 3.50 --slippage-points-per-side 0 `
    --swap-long-points-per-day -0.80 --swap-short-points-per-day 0.30 `
    --triple-swap-weekday 2 --defective-bar-tolerance 0
research trial scenario-report --trial-id trial-1
research trial count
research trial verify
```

The command is invoked as `research trial scenarios`, not `research trial scenario-report ...` for
the second — two separate invocations, and the report is worth running on its own rather than
trusting the orchestrator's own copy, since the standalone read is the one an operator or a later
gate will use.

Then repeat the `scenarios` command with identical arguments — same prefix, same timestamps — and
confirm it is idempotent: three attempts still, three digests unchanged, and the chain still
verifies. This is worth doing by hand rather than only in a test, because it is the property that
makes the twelve required options tolerable.

Read the three commands' JSON and check each of these rather than assuming them:

- the report names the declared grid first, and all three scenarios;
- each level's `spread_cost` is **strictly larger** than the baseline's;
- each level's `market_pnl` is **equal** to the baseline's — the market's move is not a stressed
  quantity, and a difference here would mean the stress leaked into the signal, which is the one
  thing a cost grid must not do;
- `net_pnl` decreases across the grid;
- all three scenarios share one `source_result_sha256` prefix and one `run_id` — the inherited
  8B2a defect, now confirmed rather than assumed;
- the report's own arithmetic holds: `baseline.net_pnl - stressed.net_pnl == degradations[0].net_pnl_delta`;
- `research trial count` moved `audit_attempts` by 3 and `selection_lotteries` by 0;
- `research trial verify` still reports `valid: true` over the whole chain.

The `market_pnl` equality is the assertion most worth making and the one most likely to need
qualifying. It holds on this engine because sizing does not depend on cost. If a run shows it
*differing*, do not record the report as a pass — the identity check pins the trade sequence and
`net_pnl` is the only field allowed to move, so a `market_pnl` that moved means the six checks have
a hole, and that is a finding about this slice rather than a limitation to document.

- [ ] **Step 4: Check the final repository state**

```powershell
git status --short
git diff --check
```

Expected: only intended source, test, and documentation files.

---

## Plan Self-Review Checklist

- [x] Every 8B2b design decision has a task: the grid and checks (Task 1), the shared simulation and seal (Task 2), the two commands (Task 3), the gates (Task 4).
- [x] The grid is read from the chain, not from a file, and the orchestrator's copy is re-derived from the sealed registration by the same report path.
- [x] Each of the six checks has a refusing case, and the identity check pins the trade sequence rather than prices.
- [x] No new sealed artifact, model field, event type, migration, dependency, or privilege.
- [x] No verdict, threshold, or promotion claim; Task 4's gate greps the report and the `--help` text for one, and the words it matches are named so the gate is not silently weakened.
- [x] The S-4 guarantee — orchestrator's 1.0x equals a hand-run 1.0x — is a test, and Task 2's refactor is gated on `backtest run`'s own digest not moving.
- [x] The counters consequence is stated, and the unvouched `spec_sha256` is named as the reason the orchestrator is load-bearing rather than convenient.
- [x] Nothing is summed into a total cost, and `swap` is reported signed.
- [x] A missing document (17) and a wrong-but-present set (19) are separate codes with separate tests, because their remedies differ.
- [x] Every helper and signature the plan names was checked against the source before being written down: `simulate` takes `BarReader` because `build_backtester` does; `registered_protocol` takes `LedgerEvent` and reads `replay()` because `events_for` cannot return a `PREREGISTERED` row; `_backtest_request` exists only because this plan extracts it; `canonical_sha256(TrialSpec)` is computed because no `candidate_for` method exists.
- [x] No step contains a placeholder; every code step shows the code and every test step shows the test.
- [x] Tampered documents in tests are rebuilt through a constructor, never `model_copy(update=...)`, so no test asserts on a document the engine would refuse.
