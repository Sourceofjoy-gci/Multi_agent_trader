"""I-8: a stop may never be moved away from profit.

Spec Section 15's ``test_stop_monotonic``. The failure mode this exists for is
a SEQUENCE of individually plausible steps -- each one looks like a tighten,
the series is not -- which is why it is generative rather than a table.

Both assertions are deliberate. The per-transition one is the property:
Section 15's wording is "the recorded stop is non-decreasing for a BUY across
the entire sequence", and a run that moved the stop away from profit at step 3
and back at step 7 satisfies the endpoints while breaking the invariant at the
moment a broker could have filled it. The endpoint one is kept because it is
free and it states the property a reader came for.

How many examples actually reach the per-transition assertion is visible in
``pytest --hypothesis-show-statistics`` via the ``event()`` below: a generative
test that never reaches its assertion is the most convincing hollow test there
is, and this file is worthless without that number.

``decide_tighten()`` has no caller in ``src/`` yet -- trailing is deliberately
out of scope this phase (spec Section 10) until a strategy can A/B it. This
test is not dead weight for that: it pre-proves I-8 for the trailing path
ahead of the caller that will use it, the same way the rest of I-8 is already
enforced today by ``decide()``'s structure and ``amend_protection()`` (see
``README.md``'s Phase 5 section).
"""

from decimal import Decimal

from hypothesis import event, given
from hypothesis import strategies as st

from trading_house.execution.guard import ActionKind, decide_tighten

STOPS = st.decimals(min_value=Decimal("1.0"), max_value=Decimal("2.0"), places=5)


@given(
    is_buy=st.booleans(),
    start=STOPS,
    candidates=st.lists(STOPS, min_size=1, max_size=40),
)
def test_no_sequence_of_cycles_moves_a_stop_away_from_profit(
    is_buy: bool, start: Decimal, candidates: list[Decimal]
) -> None:
    stop = start
    tightened = 0
    for candidate in candidates:
        action = decide_tighten(is_buy=is_buy, current=stop, candidate=candidate)
        if action.kind is not ActionKind.TIGHTEN_STOP:
            continue
        assert action.stop_loss is not None
        assert (action.stop_loss > stop) if is_buy else (action.stop_loss < stop)
        stop = action.stop_loss
        tightened += 1

    event("tightened at least once" if tightened else "never tightened")
    assert (stop >= start) if is_buy else (stop <= start)
