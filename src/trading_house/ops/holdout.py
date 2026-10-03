"""What the one-time holdout opening reads and what ``decide`` reads back from it.

Phase 8D2. ``research trial open-holdout`` runs the declared cost grid over the locked window
and seals three bundles whose provenance says the holdout was OPENED. This module is the
reads around that: what must be true before the first write, and how the opened bundles
become the two measurements gate 2 reads. Nothing here judges, and nothing here appends.

The holdout's dataset hash is declared when the protocol locks the holdout and COMPUTED when it
is opened (8E): ``research trial open-holdout`` hashes the stored bars of the holdout window
before its first write and refuses a different hash (umbrella 10's "dataset hash mismatch"),
and each opened bundle's provenance carries the digest its run computed. A holdout declared
with a placeholder hash can therefore not be opened.

Opened-bundle contents are verified only for provenance, window, dataset hash and costs, at
``decide`` time (``holdout_expectancies``). The trades inside an opened bundle are never
re-simulated: nothing here can replay the holdout again, so the bundle's trades are taken as
sealed. What keeps a fabricated opened bundle out is that ``record`` refuses to seal one
(``refuse_forged_holdout_provenance``) and ``open-holdout`` is the only sealer.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal

from trading_house.core.errors import PromotionRefusedError, ScenarioEvidenceError
from trading_house.marketdata.models import Coverage
from trading_house.ops.compounding import refuse_unfaithful
from trading_house.ops.scenarios import is_research_run, refuse_reportable
from trading_house.research.backtest.sizing import SizingMode
from trading_house.research.evidence import EvidenceBundle
from trading_house.research.trial_ledger import HoldoutState, ReturnSeriesBasis, TrialProtocol
from trading_house.research.validation.coverage import scenario_expectancy
from trading_house.research.validation.measurement import Measurement
from trading_house.research.validation.series import TradeSample

FORBIDDEN_TO_RECORD = frozenset({HoldoutState.OPENED, HoldoutState.CONSUMED, HoldoutState.LOCKED})
"""Holdout states no bundle handed to ``record`` may claim. Only ``open-holdout`` seals an opened
bundle, after its own checks; nothing produces a bundle that ran while the holdout was merely
locked. A bundle claiming one of these was not made by the commands that may make it."""


def refuse_forged_holdout_provenance(bundle: EvidenceBundle) -> None:
    """Refuse, before any write, a bundle whose provenance claims an opening it never had.

    ``research trial record`` seals whatever bundle it is given, so without this an operator
    could seal an opened bundle with no locked holdout, no decision, no policy check and
    fabricated trades. ``open-holdout`` seals through ``seal_bundle`` directly and is unaffected.
    """

    if bundle.provenance.holdout_state in FORBIDDEN_TO_RECORD:
        raise PromotionRefusedError() from ValueError(
            f"a bundle claiming its holdout was {bundle.provenance.holdout_state.value} "
            "cannot be recorded; only open-holdout seals one"
        )


HOLDOUT_LEVELS = (Decimal("1.5"), Decimal(2))
"""The two stressed levels gate 2 reads on the opened window."""


def refuse_outside_coverage(start: datetime, end: datetime, coverage: Coverage) -> None:
    """The window must lie inside the stored bars, checked before the first write.

    The simulator refuses the same window, but only after the first start row is appended;
    an opening is a one-way door, so the answer is taken first. A store with no bars at all
    covers nothing.
    """

    earliest, latest = coverage.earliest_event_time, coverage.latest_event_time
    if earliest is None or latest is None or start < earliest or end > latest:
        raise ScenarioEvidenceError() from ValueError(
            f"the holdout window [{start}, {end}] is not inside the stored bars "
            f"[{earliest}, {latest}]"
        )


def holdout_expectancies(
    trial_id: str, protocol: TrialProtocol, sealed: Sequence[tuple[str, EvidenceBundle]]
) -> tuple[Measurement, Measurement]:
    """Gate 2's two inputs: the opened 1.5x and 2.0x net expectancies (8C3's measurement).

    Each level must be sealed exactly once as an opened constant-notional run, and that run
    must be the protocol's candidate on the holdout window, at the declared baseline costs,
    carrying the declared holdout dataset hash. Anything else is ``PromotionRefusedError``:
    a ``decide`` that cannot supply both would consume the holdout for nothing.
    """

    opened = [item for item in sealed if not is_research_run(item[1])]
    # Presence first, for BOTH levels, so a half-made opening is always the same refusal
    # (PromotionRefusedError) however faithful the half that exists is.
    chosen: list[tuple[Decimal, str, EvidenceBundle]] = []
    for level in HOLDOUT_LEVELS:
        found = [
            item
            for item in opened
            if item[1].sizing is SizingMode.CONSTANT_NOTIONAL
            and item[1].result.cost_model.stress_multiplier == level
        ]
        if len(found) != 1:
            raise PromotionRefusedError() from ValueError(
                f"trial {trial_id} has {len(found)} opened bundles at {level}x; one is required"
            )
        chosen.append((level, *found[0]))
    measured: list[Measurement] = []
    for level, digest, bundle in chosen:
        refuse_reportable(trial_id, [(digest, bundle)])
        if bundle.provenance.holdout_state is not HoldoutState.OPENED:
            raise PromotionRefusedError() from ValueError(
                f"the {level}x bundle says its holdout was {bundle.provenance.holdout_state.value}"
            )
        if bundle.provenance.dataset_sha256 != protocol.holdout.dataset_sha256:
            raise PromotionRefusedError() from ValueError(
                f"the {level}x bundle does not carry the protocol's declared holdout dataset hash"
            )
        refuse_unfaithful(trial_id, protocol, bundle, level, opened=True)
        measured.append(
            scenario_expectancy(
                TradeSample.from_bundle(bundle),
                level,
                evidence_sha256=(digest,),
                basis_is_mark_to_market=bundle.return_series_basis
                is ReturnSeriesBasis.MARK_TO_MARKET,
            )
        )
    return measured[0], measured[1]
