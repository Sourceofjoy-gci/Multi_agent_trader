import pytest
from pydantic import ValidationError

from trading_house.constitution.binding import VenueBinding, parse_venue_binding
from trading_house.core.errors import ConfigurationError

BINDING = b"""
venue: mt5
books:
  fx_scalp: {magic_range: [110000, 119999]}
  fx_swing: {magic_range: [120000, 129999]}
instruments:
  fx.eurusd: {server_symbol: "EURUSD.raw"}
"""


def test_binding_parses() -> None:
    binding: VenueBinding = parse_venue_binding(BINDING)
    assert binding.books["fx_scalp"].magic_range == (110000, 119999)
    assert binding.instruments["fx.eurusd"].server_symbol == "EURUSD.raw"


def test_magic_ranges_must_not_overlap() -> None:
    """Overlapping ranges make book identity unrecoverable after a restart."""

    overlapping = BINDING.replace(b"[120000, 129999]", b"[119000, 129999]")
    with pytest.raises(ConfigurationError):
        parse_venue_binding(overlapping)


def test_magic_ranges_must_not_overlap_when_one_fully_contains_the_other() -> None:
    """A range fully inside another (declared second, the wider range first)
    must be rejected, not just partial overlap at the edges."""

    contained = BINDING.replace(b"[120000, 129999]", b"[112000, 113000]")
    with pytest.raises(ConfigurationError):
        parse_venue_binding(contained)


def test_magic_ranges_must_not_overlap_when_the_contained_range_is_declared_first() -> None:
    """The narrower range appearing first in the mapping must be caught too --
    the overlap check must be symmetric regardless of declaration order."""

    reversed_contained = BINDING.replace(b"[110000, 119999]", b"[111000, 112000]").replace(
        b"[120000, 129999]", b"[100000, 200000]"
    )
    with pytest.raises(ConfigurationError):
        parse_venue_binding(reversed_contained)


def test_magic_ranges_must_be_ordered() -> None:
    with pytest.raises(ConfigurationError):
        parse_venue_binding(BINDING.replace(b"[110000, 119999]", b"[119999, 110000]"))


def test_binding_is_frozen() -> None:
    binding = parse_venue_binding(BINDING)
    with pytest.raises(ValidationError):
        binding.books["fx_scalp"].magic_range = (1, 2)


def test_malformed_binding_is_redacted() -> None:
    with pytest.raises(ConfigurationError):
        parse_venue_binding(b"venue: [")
