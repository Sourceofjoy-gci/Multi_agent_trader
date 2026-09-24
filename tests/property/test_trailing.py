"""I-8 over arbitrary price paths, not chosen examples.

Monotonicity is exactly the shape Hypothesis falsifies well: a hand-written
case proves the path it walks, and the paths that break a trail are the ones
nobody thinks to write.

The brief's generator drew a list of highs and nothing else, with the close
pinned to the high, one fixed parameter pair and ``Side.BUY`` only. Four of
the five inputs ``trail_candidate`` reads were therefore constant, so a sign
error on the short side, a clamp that binds only when the close sits below the
high, or a hysteresis-versus-monotonic interaction at some other step size
would all have survived it. Everything the function reads is drawn here
instead, and the walk asserts the distance floor on every candidate it is
given as well as the ordering -- one path, two invariants, no extra cost.
"""

from decimal import Decimal

from hypothesis import given
from hypothesis import strategies as st

from tests.unit.research.backtest.conftest import _bar, _contract
from trading_house.core.schemas import Side
from trading_house.research.backtest.engine import trail_candidate
from trading_house.research.backtest.strategy import ChandelierPolicy

PRICES = st.decimals(min_value=Decimal("1.00000"), max_value=Decimal("2.00000"), places=5)
"""A one-unit band on the 5-digit grid: wide enough that a trail can walk a
long way, tight enough that every draw stays a plausible FX price."""

ATRS = st.decimals(min_value=Decimal("0.00001"), max_value=Decimal("0.01000"), places=5)
ATR_MULTIPLES = st.decimals(min_value=Decimal("0.01"), max_value=Decimal("5"), places=2)
# Zero is a legal step -- "no hysteresis" -- and is the setting under which the
# monotonic check is the only thing left holding I-8 up. It must be reachable.
STEPS = st.decimals(min_value=Decimal(0), max_value=Decimal(500), places=0)
SIDES = st.sampled_from([Side.BUY, Side.SELL])


@given(
    side=SIDES,
    path=st.lists(st.tuples(PRICES, PRICES), min_size=2, max_size=40),
    start=PRICES,
    atr=ATRS,
    atr_multiple=ATR_MULTIPLES,
    min_step_points=STEPS,
)
def test_a_trail_only_ever_moves_toward_profit_over_any_sequence_of_bars(
    side: Side,
    path: list[tuple[Decimal, Decimal]],
    start: Decimal,
    atr: Decimal,
    atr_multiple: Decimal,
    min_step_points: Decimal,
) -> None:
    policy = ChandelierPolicy(
        kind="chandelier", atr_multiple=atr_multiple, min_step_points=min_step_points
    )
    contract = _contract()
    # D-6's broker floor, the same pair ``compute_stop_distance`` maxes over: a
    # stop inside the freeze band is legal to place and illegal to MODIFY,
    # which is the one thing a trail does on every bar.
    floor = max(contract.min_stop_distance, contract.freeze_distance)
    stop = start

    for extreme, close in path:
        bar = _bar(high=max(extreme, close), low=min(extreme, close), close=close)
        candidate = trail_candidate(
            side=side,
            current_stop=stop,
            bar=bar,
            atr=atr,
            contract=contract,
            policy=policy,
        )
        if candidate is None:
            continue
        assert candidate > stop if side is Side.BUY else candidate < stop
        assert abs(close - candidate) >= floor
        stop = candidate
