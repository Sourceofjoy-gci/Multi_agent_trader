"""What the one-time holdout opening reads and what ``decide`` reads back from it.

Phase 8D2. ``research trial open-holdout`` runs the declared cost grid over the locked window
and seals three bundles whose provenance says the holdout was OPENED. This module is the
reads around that: what must be true before the first write, and how the opened bundles
become the two measurements gate 2 reads. Nothing here judges, and nothing here appends.

The holdout's dataset hash is DECLARED, never computed. The protocol states one when it locks
the holdout, the opening records that declared value in each bundle's provenance, and nothing
in this repository can hash a bar store to compare it with. Umbrella 10's "dataset hash
mismatch" check therefore does not exist, and this module does not pretend to it.
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
