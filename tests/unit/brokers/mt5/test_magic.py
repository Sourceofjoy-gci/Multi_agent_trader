import pytest

from trading_house.brokers.mt5.magic import derive_magic

FX_SCALP = (110000, 119999)


def test_derivation_is_deterministic() -> None:
    """Recovery after a crash recomputes the magic with no persisted mapping."""

    assert derive_magic("intent-1", FX_SCALP) == derive_magic("intent-1", FX_SCALP)


def test_derivation_stays_inside_the_books_range() -> None:
    for index in range(500):
        magic = derive_magic(f"intent-{index}", FX_SCALP)
        assert FX_SCALP[0] <= magic <= FX_SCALP[1]


def test_different_books_give_different_magics_for_one_intent() -> None:
    assert derive_magic("intent-1", (110000, 119999)) != derive_magic("intent-1", (120000, 129999))


def test_a_descending_range_is_rejected() -> None:
    with pytest.raises(ValueError, match="ascending"):
        derive_magic("intent-1", (119999, 110000))


def test_a_single_slot_range_is_usable() -> None:
    assert derive_magic("intent-1", (110000, 110000)) == 110000
