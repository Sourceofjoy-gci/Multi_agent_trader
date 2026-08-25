from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError

from trading_house.constitution.binding import VenueBinding, load_venue_binding, parse_venue_binding
from trading_house.core.errors import ConfigurationError, SignatureVerificationError

CONFIG_DIR = Path("config")

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


def _mutate_one_byte(data: bytes, index: int = 0) -> bytes:
    mutated = bytearray(data)
    mutated[index % len(data)] ^= 0xFF
    return bytes(mutated)


def test_load_venue_binding_rejects_a_tampered_binding(tmp_path: Path) -> None:
    """Any single-byte edit to the checked-in binding must fail verification.

    Mirrors the constitution's own ``test_loader_rejects_a_tampered_constitution``:
    the checked-in signature must not survive any edit to the checked-in YAML.
    """

    original = (CONFIG_DIR / "venue_binding.mt5.yaml").read_bytes()
    tampered_path = tmp_path / "venue_binding.mt5.yaml"
    tampered_path.write_bytes(_mutate_one_byte(original))

    with pytest.raises(SignatureVerificationError):
        load_venue_binding(
            tampered_path,
            CONFIG_DIR / "venue_binding.mt5.yaml.sig",
            CONFIG_DIR / "risk_constitution.public.pem",
        )


def test_load_venue_binding_rejects_the_wrong_public_key(tmp_path: Path) -> None:
    """Mirrors the constitution's own signature-family coverage: a checked-in,
    validly formatted signature must not verify against the wrong key."""

    wrong_public_key = tmp_path / "wrong.public.pem"
    wrong_public_key.write_bytes(
        Ed25519PrivateKey.generate()
        .public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )

    with pytest.raises(SignatureVerificationError):
        load_venue_binding(
            CONFIG_DIR / "venue_binding.mt5.yaml",
            CONFIG_DIR / "venue_binding.mt5.yaml.sig",
            wrong_public_key,
        )
