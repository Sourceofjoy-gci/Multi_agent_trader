import base64
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.rsa import generate_private_key

from trading_house.constitution.signing import (
    generate_key_pair,
    load_private_key,
    load_public_key,
    sign_bytes,
    sign_file,
    verify_signature,
)
from trading_house.core.errors import SignatureVerificationError


def test_ed25519_signature_verifies_exact_payload() -> None:
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    payload = b"constitution: signed\n"

    verify_signature(public_key, sign_bytes(private_key, payload), payload)


def test_signature_rejects_a_one_byte_payload_change() -> None:
    private_key = Ed25519PrivateKey.generate()
    signature = sign_bytes(private_key, b"constitution: signed\n")

    with pytest.raises(SignatureVerificationError) as error:
        verify_signature(private_key.public_key(), signature, b"constitution: signed!")

    assert str(error.value) == "signature verification failed"
    assert error.value.__cause__ is not None


def test_signature_rejects_a_wrong_public_key() -> None:
    signer = Ed25519PrivateKey.generate()
    signature = sign_bytes(signer, b"constitution: signed\n")
    wrong_public_key = Ed25519PrivateKey.generate().public_key()

    with pytest.raises(SignatureVerificationError):
        verify_signature(wrong_public_key, signature, b"constitution: signed\n")


def test_signature_rejects_a_non_64_byte_signature() -> None:
    public_key = Ed25519PrivateKey.generate().public_key()

    with pytest.raises(SignatureVerificationError) as error:
        verify_signature(public_key, b"too short", b"constitution: signed\n")

    assert str(error.value) == "signature verification failed"


def test_public_key_loader_rejects_malformed_pem_without_disclosure() -> None:
    non_ed25519_pem = b"not a PEM: confidential"

    with pytest.raises(SignatureVerificationError) as error:
        load_public_key(non_ed25519_pem)

    assert str(error.value) == "signature verification failed"
    assert non_ed25519_pem.decode(errors="ignore") not in str(error.value)
    assert error.value.__cause__ is not None


def test_public_key_loader_rejects_another_key_algorithm() -> None:
    rsa_public_key = generate_private_key(public_exponent=65537, key_size=2048).public_key()
    private_pem = rsa_public_key.public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )

    with pytest.raises(SignatureVerificationError):
        load_public_key(private_pem)


def test_private_key_loader_rejects_a_public_key_pem() -> None:
    public_pem = Ed25519PrivateKey.generate().public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )

    with pytest.raises(SignatureVerificationError):
        load_private_key(public_pem)


def test_generate_key_pair_and_sign_file_use_required_formats(tmp_path: Path) -> None:
    private_path = tmp_path / "keys" / "risk_constitution.private.pem"
    public_path = tmp_path / "config" / "risk_constitution.public.pem"
    constitution_path = tmp_path / "config" / "risk_constitution.yaml"
    signature_path = tmp_path / "config" / "risk_constitution.yaml.sig"
    constitution_path.parent.mkdir()
    constitution_path.write_bytes(b"constitution: signed\n")

    generate_key_pair(private_path, public_path)
    sign_file(constitution_path, private_path, signature_path)

    assert private_path.read_bytes().startswith(b"-----BEGIN PRIVATE KEY-----")
    assert public_path.read_bytes().startswith(b"-----BEGIN PUBLIC KEY-----")
    signature = base64.b64decode(signature_path.read_bytes().strip(), validate=True)
    assert len(signature) == 64
    public_key = load_public_key(public_path.read_bytes())
    verify_signature(public_key, signature, constitution_path.read_bytes())
