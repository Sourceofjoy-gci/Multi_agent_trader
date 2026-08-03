import base64
import hashlib
from dataclasses import fields
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from trading_house.constitution.loader import LoadedConstitution, load_constitution
from trading_house.core.errors import ConfigurationError, SignatureVerificationError


class SignedFixture:
    def __init__(self, constitution: Path, signature: Path, public_key: Path) -> None:
        self.constitution = constitution
        self.signature = signature
        self.public_key = public_key


def signed_fixture(tmp_path: Path, yaml_bytes: bytes) -> SignedFixture:
    private_key = Ed25519PrivateKey.generate()
    constitution = tmp_path / "risk_constitution.yaml"
    signature = tmp_path / "risk_constitution.yaml.sig"
    public_key = tmp_path / "risk_constitution.public.pem"
    constitution.write_bytes(yaml_bytes)
    signature.write_bytes(base64.b64encode(private_key.sign(yaml_bytes)))
    public_key.write_bytes(
        private_key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    return SignedFixture(constitution, signature, public_key)


def valid_yaml() -> bytes:
    return Path("config/risk_constitution.yaml").read_bytes()


def test_loader_returns_constitution_and_exact_bytes_metadata(tmp_path: Path) -> None:
    yaml_bytes = valid_yaml()
    paths = signed_fixture(tmp_path, yaml_bytes)

    loaded = load_constitution(paths.constitution, paths.signature, paths.public_key)
    raw_public_key = serialization.load_pem_public_key(paths.public_key.read_bytes()).public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )

    assert loaded.constitution.version == 1
    assert loaded.constitution_sha256 == hashlib.sha256(yaml_bytes).hexdigest()
    assert loaded.public_key_fingerprint == hashlib.sha256(raw_public_key).hexdigest()
    assert len(loaded.constitution_sha256) == 64
    assert len(loaded.public_key_fingerprint) == 64
    assert loaded.constitution_sha256.islower()
    assert loaded.public_key_fingerprint.islower()


def test_loaded_constitution_is_frozen_and_uses_slots() -> None:
    assert LoadedConstitution.__dataclass_params__.frozen is True  # type: ignore[attr-defined]
    assert tuple(field.name for field in fields(LoadedConstitution)) == (
        "constitution",
        "constitution_sha256",
        "public_key_fingerprint",
    )
    assert hasattr(LoadedConstitution, "__slots__")


def test_loader_rejects_tamper_before_yaml_parse(tmp_path: Path) -> None:
    paths = signed_fixture(tmp_path, b"version: [invalid yaml")
    paths.constitution.write_bytes(b"version: [changed invalid yaml")

    with pytest.raises(SignatureVerificationError):
        load_constitution(paths.constitution, paths.signature, paths.public_key)


def test_loader_rejects_malformed_base64_without_echoing_signature(tmp_path: Path) -> None:
    paths = signed_fixture(tmp_path, valid_yaml())
    malformed_signature = b"not base64: confidential"
    paths.signature.write_bytes(malformed_signature)

    with pytest.raises(SignatureVerificationError) as error:
        load_constitution(paths.constitution, paths.signature, paths.public_key)

    assert str(error.value) == "signature verification failed"
    assert malformed_signature.decode() not in str(error.value)
    assert error.value.__cause__ is not None


def test_loader_rejects_a_signature_with_wrong_decoded_length(tmp_path: Path) -> None:
    paths = signed_fixture(tmp_path, valid_yaml())
    paths.signature.write_bytes(base64.b64encode(b"not 64 bytes"))

    with pytest.raises(SignatureVerificationError):
        load_constitution(paths.constitution, paths.signature, paths.public_key)


@pytest.mark.parametrize("missing_file", ["signature", "public_key"])
def test_loader_maps_missing_verification_files_to_redacted_signature_error(
    tmp_path: Path, missing_file: str
) -> None:
    paths = signed_fixture(tmp_path, valid_yaml())
    missing_path = paths.signature if missing_file == "signature" else paths.public_key
    missing_path.unlink()

    with pytest.raises(SignatureVerificationError) as error:
        load_constitution(paths.constitution, paths.signature, paths.public_key)

    assert str(error.value) == "signature verification failed"
    assert str(missing_path) not in str(error.value)
    assert error.value.__cause__ is not None


@pytest.mark.parametrize("unreadable_file", ["signature", "public_key"])
def test_loader_maps_unreadable_verification_files_to_redacted_signature_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unreadable_file: str
) -> None:
    paths = signed_fixture(tmp_path, valid_yaml())
    unreadable_path = paths.signature if unreadable_file == "signature" else paths.public_key
    original_read_bytes = Path.read_bytes

    def raise_for_unreadable_file(path: Path) -> bytes:
        if path == unreadable_path:
            raise OSError(f"cannot read sensitive file {path}")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", raise_for_unreadable_file)

    with pytest.raises(SignatureVerificationError) as error:
        load_constitution(paths.constitution, paths.signature, paths.public_key)

    assert str(error.value) == "signature verification failed"
    assert str(unreadable_path) not in str(error.value)
    assert error.value.__cause__ is not None


def test_loader_rejects_a_non_ed25519_public_key(tmp_path: Path) -> None:
    paths = signed_fixture(tmp_path, valid_yaml())
    private_key = Ed25519PrivateKey.generate()
    paths.public_key.write_bytes(
        private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )

    with pytest.raises(SignatureVerificationError) as error:
        load_constitution(paths.constitution, paths.signature, paths.public_key)

    assert str(error.value) == "signature verification failed"
    assert error.value.__cause__ is not None


def test_loader_maps_validly_signed_malformed_yaml_to_configuration_error(tmp_path: Path) -> None:
    paths = signed_fixture(tmp_path, b"version: [invalid yaml")

    with pytest.raises(ConfigurationError) as error:
        load_constitution(paths.constitution, paths.signature, paths.public_key)

    assert str(error.value) == "configuration invalid"
    assert error.value.__cause__ is not None
