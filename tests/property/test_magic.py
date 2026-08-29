from hypothesis import given
from hypothesis import strategies as st

from trading_house.brokers.mt5.magic import derive_magic

RANGES = st.tuples(
    st.integers(min_value=1, max_value=900_000),
    st.integers(min_value=0, max_value=99_999),
).map(lambda pair: (pair[0], pair[0] + pair[1]))


@given(st.text(min_size=1, max_size=64), RANGES)
def test_magic_is_always_inside_the_range(intent_id: str, magic_range: tuple[int, int]) -> None:
    magic = derive_magic(intent_id, magic_range)

    assert magic_range[0] <= magic <= magic_range[1]


@given(st.text(min_size=1, max_size=64), RANGES)
def test_magic_is_always_deterministic(intent_id: str, magic_range: tuple[int, int]) -> None:
    assert derive_magic(intent_id, magic_range) == derive_magic(intent_id, magic_range)


@given(st.text(min_size=1, max_size=64), RANGES)
def test_magic_is_always_a_positive_int(intent_id: str, magic_range: tuple[int, int]) -> None:
    """MetaTrader 5 magic numbers must be positive integers."""

    magic = derive_magic(intent_id, magic_range)

    assert isinstance(magic, int)
    assert magic > 0
