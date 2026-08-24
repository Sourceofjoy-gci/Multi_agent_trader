"""Invariant I-2: risk limits are only ever accepted from signed bytes."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from hypothesis import given
from hypothesis import strategies as st

from trading_house.constitution.loader import load_constitution
from trading_house.constitution.models import parse_constitution_yaml
from trading_house.constitution.signing import (
    decode_signature,
    load_public_key,
    sign_bytes,
    verify_signature,
)
from trading_house.core.errors import ConfigurationError, SignatureVerificationError

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = PROJECT_ROOT / "config"
CONSTITUTION_BYTES = (CONFIG_DIR / "risk_constitution.yaml").read_bytes()

_SIGNING_KEY = Ed25519PrivateKey.generate()
_PUBLIC_KEY = _SIGNING_KEY.public_key()
_OTHER_PUBLIC_KEY = Ed25519PrivateKey.generate().public_key()

PAYLOADS = st.binary(min_size=1, max_size=512)


def _mutate_one_byte(data: bytes, index_seed: int) -> bytes:
    mutated = bytearray(data)
    index = index_seed % len(data)
    mutated[index] ^= 0xFF
    return bytes(mutated)


@given(PAYLOADS)
def test_signing_then_verifying_always_succeeds(data: bytes) -> None:
    verify_signature(_PUBLIC_KEY, sign_bytes(_SIGNING_KEY, data), data)


@given(PAYLOADS, st.integers(min_value=0, max_value=10_000))
def test_any_single_byte_mutation_breaks_the_signature(data: bytes, index_seed: int) -> None:
    signature = sign_bytes(_SIGNING_KEY, data)

    with pytest.raises(SignatureVerificationError):
        verify_signature(_PUBLIC_KEY, signature, _mutate_one_byte(data, index_seed))


@given(PAYLOADS, st.integers(min_value=0, max_value=10_000))
def test_a_mutated_signature_never_verifies(data: bytes, index_seed: int) -> None:
    signature = sign_bytes(_SIGNING_KEY, data)

    with pytest.raises(SignatureVerificationError):
        verify_signature(_PUBLIC_KEY, _mutate_one_byte(signature, index_seed), data)


@given(PAYLOADS)
def test_another_key_never_verifies(data: bytes) -> None:
    signature = sign_bytes(_SIGNING_KEY, data)

    with pytest.raises(SignatureVerificationError):
        verify_signature(_OTHER_PUBLIC_KEY, signature, data)


@given(PAYLOADS, st.integers(min_value=0, max_value=63))
def test_truncated_signatures_are_rejected(data: bytes, cut: int) -> None:
    signature = sign_bytes(_SIGNING_KEY, data)

    with pytest.raises(SignatureVerificationError):
        verify_signature(_PUBLIC_KEY, signature[:cut], data)


@given(PAYLOADS, st.binary(min_size=1, max_size=8))
def test_extended_signatures_are_rejected(data: bytes, extra: bytes) -> None:
    signature = sign_bytes(_SIGNING_KEY, data)

    with pytest.raises(SignatureVerificationError):
        verify_signature(_PUBLIC_KEY, signature + extra, data)


@given(PAYLOADS)
def test_base64_round_trip_preserves_the_signature(data: bytes) -> None:
    signature = sign_bytes(_SIGNING_KEY, data)
    encoded = base64.b64encode(signature) + b"\n"

    assert decode_signature(encoded) == signature


@given(st.integers(min_value=0, max_value=10_000))
def test_one_byte_of_constitution_drift_fails_verification(index_seed: int) -> None:
    """The checked-in signature must not survive any edit to the YAML."""

    signature = decode_signature((CONFIG_DIR / "risk_constitution.yaml.sig").read_bytes())
    public_key = load_public_key((CONFIG_DIR / "risk_constitution.public.pem").read_bytes())

    with pytest.raises(SignatureVerificationError):
        verify_signature(public_key, signature, _mutate_one_byte(CONSTITUTION_BYTES, index_seed))


@given(st.integers(min_value=0, max_value=10_000))
def test_mutated_yaml_never_silently_parses_into_the_same_limits(index_seed: int) -> None:
    mutated = _mutate_one_byte(CONSTITUTION_BYTES, index_seed)
    original = parse_constitution_yaml(CONSTITUTION_BYTES)

    try:
        parsed = parse_constitution_yaml(mutated)
    except ConfigurationError:
        return

    assert parsed != original or mutated == CONSTITUTION_BYTES


def test_loader_rejects_a_tampered_constitution(tmp_path: Path) -> None:
    """Any single-byte edit to the checked-in file must fail the loader's verification.

    This mutates an arbitrary byte rather than string-replacing a specific limit's
    literal value: tying the tamper to one field's exact text (e.g. a particular
    ``risk_per_trade_pct``) silently stops testing anything the moment that literal
    value changes elsewhere in the config (a no-op replace leaves the bytes identical
    to the checked-in file, which then still verifies, and the test passes for the
    wrong reason without ever tampering anything). A byte-level mutation via the
    module's own ``_mutate_one_byte`` helper stays meaningful regardless of book
    contents. ``index_seed=0`` is deterministic and always a valid index into a
    non-empty file, so this needs no ``@given``/``tmp_path`` combination (which
    Hypothesis's function-scoped-fixture health check rejects).
    """

    tampered_path = tmp_path / "risk_constitution.yaml"
    tampered_path.write_bytes(_mutate_one_byte(CONSTITUTION_BYTES, index_seed=0))

    with pytest.raises(SignatureVerificationError):
        load_constitution(
            tampered_path,
            CONFIG_DIR / "risk_constitution.yaml.sig",
            CONFIG_DIR / "risk_constitution.public.pem",
        )
