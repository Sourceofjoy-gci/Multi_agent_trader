# Phase 12 — The capacity model and an unseen holdout (design)

**Date:** 2026-10-04
**Status:** Implemented in this phase.
**Source:** `docs/reviews/2026-10-04-tech-stack-and-architecture-review.md` §3.4, and the first two
items of README "Phase 8 — what blocks promotion".

## 1. The problem

Two of the three things that blocked every promotion were inside this repository:

- **Gate 9 could never be measured.** 8B3 typed the capacity diagnostic as permanently
  `UNAVAILABLE`: no protocol could declare a volume-to-lots model, and no run recorded the market
  volume at its fills. Every decision was `REJECTED` on gate 9 alone.
- **"Unseen" meant only "not opened".** The holdout lifecycle (8D2) guaranteed a holdout opens
  once. It did not check that nothing had read the window before it was locked:
  - a protocol could lock a holdout inside its own research window;
  - another trial's research runs over the same bars did not count;
  - and there was no way to lock a window before its data existed, which is the only kind of
    holdout the umbrella spec says EURUSD can still have (no unseen span remains inside the
    inspected history).

The third blocker, human authorizations and signatures, stays outside the repository by design.

## 2. What this phase promises

1. **Capacity is a declared assumption, measured.** A protocol may declare a `CapacitySpec` before
   any result exists. A run under it seals each fill's bar volume beside its trades. Gate 9
   compares the measurement to the declared limits and passes or fails.
2. **No declaration, no number.** Without a `CapacitySpec`, or for evidence sealed without a
   liquidity record, capacity stays `unavailable` and says which is missing. Tick volume alone is
   never presented as capital capacity.
3. **A holdout is unseen only if no sealed run read it.** It derives `CONTAMINATED` when:
   - its window reaches into its own protocol's research window; or
   - any sealed run on the same instrument, by any trial, read a bar inside it, other than the
     opening runs of the trial (or the trials sharing its holdout) itself.
4. **A prospective holdout is locked before its data exists.** It declares a window and no hash.
   It derives `CONTAMINATED` if the window starts before the ledger recorded the lock. It is
   opened with a digest computed over the stored bars, and both opened levels must carry that
   same digest.
5. **An operator can ask first.** `research trial holdout-check --protocol` applies the same rules
   to an unregistered protocol and names every reason the window would not be unseen.
6. **Old bytes stay old bytes.** Every new field is excluded from the canonical form when absent:
   a protocol, bundle or report sealed before this phase re-reads to its own digest.

## 3. The capacity model

`CapacitySpec`, on `TrialProtocol.capacity`:

| Field | Meaning |
|---|---|
| `model` | `tick_volume_participation_v1`, the only model; a new one is a new literal |
| `lots_per_tick` | The declared conversion from MT5 tick volume (a count of price updates) to lots |
| `target_equity` | The capital the claim is made at |
| `max_participation` | The largest share of a bar's lots one fill may take, in (0, 1] |
| `impact_points_at_full_participation` | `k` in the square-root impact model, in points |
| `max_impact_fraction_of_edge` | The largest share of the net edge impact may consume, in (0, 1] |

The run records `Liquidity` on the bundle: for each trade, in the trades' order, the entry and
exit bar's `tick_volume` and the contract's money per point per lot
(`value_per_price_increment × point_size / price_increment`). A fill is stamped at its bar's
`event_time`, so the bar is found by that instant among the bars the replay read; a fill whose bar
is not among them is a `CoverageError`, never a guess. The bundle refuses a liquidity record whose
proposal ids are not exactly its trades'.

Measurement (`validation/capacity.py`), with `scale = target_equity / firm_equity` since
constant-notional lots scale linearly with equity:

- `participation = lots × scale / (tick_volume × lots_per_tick)` per fill;
- `impact = k × √participation × money_per_point × lots × scale` per fill;
- `net_edge = Σ net_pnl × scale`;
- `impact_fraction_of_edge = Σ impact / net_edge`, only when the edge is positive.

A fill on a zero-volume bar is counted, never divided by.

Gate 9 fails, naming every failing clause, on:

- any zero-volume fill;
- `max_participation` above the declared limit;
- an edge that is not positive (there is nothing to spend on impact);
- an impact fraction above the declared limit.

Otherwise it passes, reporting participation as its value.

**Why square-root impact.** It is the standard empirical shape (Almgren et al., Tóth et al.).
Its one constant is the protocol's to declare, not this code's to fit. A linear model would
understate small fills and overstate large ones, and fitting `k` from the run itself would let
the evidence choose its own cost.

## 4. The unseen rule

`derive_holdout` now takes every sealed bundle's window (`SealedWindow`: instrument, timeframe,
start, end, and whether it was an opening run). It also takes the time the ledger recorded the
trial's first registration (`recorded_at`, the database's clock, not a declared time). Both are
read by `ops/decide.py`; `promotion.py` stays pure and imports no clock.

Windows are inclusive bar open times, as `replay_window_bars` reads them. A research window ending
on the holdout's first bar has read it; one ending a bar earlier has not.

The order of checks is unchanged, with two additions:

1. a holdout that cannot exist (not defined) stays `NOT_DEFINED`;
2. the legacy and spent rules from 8D2;
3. **new:** `_seen_before_lock`, covering the research window overlap and a prospective start
   before the recorded lock;
4. the sharing rule from 8D2;
5. **new:** `_read_by_another_run`, covering any sealed run on the instrument inside the window.

A sealed digest with no known window is refused (`PromotionRefusedError`), not skipped.

**Consequence for EURUSD.** Phase 7's legacy runs read 2022-09-16 to 2026-09-25. Any retrospective
EURUSD holdout overlapping that span is now contaminated, which matches the umbrella spec's own
finding. A real holdout must lie before 2022-09, or be prospective.

## 5. The prospective holdout

`HoldoutSpec.collection` is `retrospective` (the default, and not written when it is the default)
or `prospective`.

- **Registering.** A prospective holdout must be `LOCKED`, declare a window with start before end,
  and declare no `dataset_sha256`, since a hash of data that does not exist yet is not one.
- **Identity.** Trials share a prospective holdout when they declare the same window, instrument
  and timeframe. A retrospective holdout is still keyed by its hash.
- **Opening.** `open-holdout` skips the declared-hash comparison and seals each level with the
  digest the run computed (Phase 8E). The holdout expectancies, and the compounding fidelity
  check, require both opened levels to carry one identical non-empty digest. That proves the 1.5×
  and 2.0× runs read the same bytes.

## 6. Not done here

- **Past bundles carry no liquidity**, so capacity stays `unavailable` for every candidate
  registered before this phase. A capacity claim needs a new protocol, and so a new trial.
- **`lots_per_tick` is an assumption** this repository cannot check. A broker's real volume, or
  a depth-of-book feed, would replace it; the model literal is versioned for that.
- **The chain is all the unseen rule sees.** A person who studied a chart of the window, or a run
  made outside this ledger, is invisible to it, and `holdout-check` says so.
- **Collecting the prospective data** is still a calendar wait: the window has to pass and its bars
  have to be ingested before the holdout can be opened.
