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


@given(st.lists(st.text(min_size=1, max_size=64), min_size=32, max_size=64, unique=True))
def test_distinct_intents_do_not_collapse_onto_one_magic(intent_ids: list[str]) -> None:
    """Containment and determinism are both satisfied by an implementation
    that always returns the range's lower bound. Magic is a locator: if every
    intent in a book shared one, Phase 3 recovery could never tell two
    positions apart. Over a 10,000-wide range, 32 distinct ids collapsing to
    a single value is not a hash collision, it is a constant.
    """

    magics = {derive_magic(intent_id, (110_000, 119_999)) for intent_id in intent_ids}

    assert len(magics) > 1
