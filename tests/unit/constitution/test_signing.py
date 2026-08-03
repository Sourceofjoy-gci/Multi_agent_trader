import base64
import os
import stat
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.rsa import generate_private_key

import trading_house.constitution.signing as signing
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


def test_private_key_loader_rejects_another_key_algorithm() -> None:
    rsa_private_key = generate_private_key(public_exponent=65537, key_size=2048)
    rsa_private_pem = rsa_private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )

    with pytest.raises(SignatureVerificationError) as error:
        load_private_key(rsa_private_pem)

    assert str(error.value) == "signature verification failed"
    assert error.value.__cause__ is not None


@pytest.mark.parametrize("existing_destination", ["private", "public"])
def test_generate_key_pair_rejects_existing_regular_destination_without_mutation(
    tmp_path: Path, existing_destination: str
) -> None:
    private_path = tmp_path / "risk_constitution.private.pem"
    public_path = tmp_path / "risk_constitution.public.pem"
    existing_path = private_path if existing_destination == "private" else public_path
    other_path = public_path if existing_destination == "private" else private_path
    original_contents = b"pre-existing key material"
    existing_path.write_bytes(original_contents)

    with pytest.raises(FileExistsError):
        generate_key_pair(private_path, public_path)

    assert existing_path.read_bytes() == original_contents
    assert not other_path.exists()


@pytest.mark.parametrize("symlink_destination", ["private", "public"])
def test_generate_key_pair_rejects_symlink_destination_without_following_it(
    tmp_path: Path, symlink_destination: str
) -> None:
    private_path = tmp_path / "risk_constitution.private.pem"
    public_path = tmp_path / "risk_constitution.public.pem"
    destination = private_path if symlink_destination == "private" else public_path
    other_path = public_path if symlink_destination == "private" else private_path
    symlink_target = tmp_path / "target.pem"
    target_contents = b"do not overwrite"
    symlink_target.write_bytes(target_contents)
    try:
        destination.symlink_to(symlink_target)
    except (NotImplementedError, OSError):
        pytest.skip("symlink creation is unavailable on this platform")

    with pytest.raises(FileExistsError):
        generate_key_pair(private_path, public_path)

    assert destination.is_symlink()
    assert symlink_target.read_bytes() == target_contents
    assert not other_path.exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits are unavailable")
def test_generate_key_pair_creates_owner_only_private_key(tmp_path: Path) -> None:
    private_path = tmp_path / "risk_constitution.private.pem"
    public_path = tmp_path / "risk_constitution.public.pem"

    generate_key_pair(private_path, public_path)

    assert stat.S_IMODE(private_path.stat().st_mode) == 0o600


def test_generate_key_pair_retains_its_private_file_without_cleanup_race_when_public_publish_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    private_path = tmp_path / "risk_constitution.private.pem"
    public_path = tmp_path / "risk_constitution.public.pem"
    real_open = os.open
    real_unlink = os.unlink
    cleanup_attempted = False

    def fail_public_open(
        path: str | bytes | os.PathLike[str], flags: int, mode: int = 0o777
    ) -> int:
        if isinstance(path, (str, os.PathLike)) and Path(path) == public_path:
            raise OSError("injected public publish failure")
        return real_open(path, flags, mode)

    monkeypatch.setattr(os, "open", fail_public_open)

    def replace_before_unlink(path: str | bytes | os.PathLike[str], *args: object) -> None:
        nonlocal cleanup_attempted
        if isinstance(path, (str, os.PathLike)) and Path(path) == private_path:
            cleanup_attempted = True
            private_path.write_bytes(b"replacement private key")
        real_unlink(path, *args)

    monkeypatch.setattr(os, "unlink", replace_before_unlink)

    with pytest.raises(SignatureVerificationError) as error:
        generate_key_pair(private_path, public_path)

    assert str(error.value) == "signature verification failed"
    assert error.value.__cause__ is not None
    assert private_path.exists()
    assert not public_path.exists()
    assert cleanup_attempted is False


def test_generate_key_pair_never_reads_staged_private_bytes_into_public_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    private_path = tmp_path / "risk_constitution.private.pem"
    public_path = tmp_path / "risk_constitution.public.pem"
    swapped_private_pem = b"-----BEGIN PRIVATE KEY-----\nattacker private material\n"
    original_read_bytes = Path.read_bytes

    def swap_staged_source(path: Path) -> bytes:
        if path.suffix == ".tmp":
            return swapped_private_pem
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", swap_staged_source)

    generate_key_pair(private_path, public_path)

    public_bytes = public_path.read_bytes()
    assert public_bytes.startswith(b"-----BEGIN PUBLIC KEY-----")
    assert swapped_private_pem not in public_bytes


def test_generate_key_pair_writes_exact_cryptography_pem_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    private_path = tmp_path / "risk_constitution.private.pem"
    public_path = tmp_path / "risk_constitution.public.pem"
    expected_private_key = Ed25519PrivateKey.generate()

    class FixedKeyFactory:
        @staticmethod
        def generate() -> Ed25519PrivateKey:
            return expected_private_key

    monkeypatch.setattr(signing, "Ed25519PrivateKey", FixedKeyFactory)

    generate_key_pair(private_path, public_path)

    assert private_path.read_bytes() == expected_private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    assert public_path.read_bytes() == expected_private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )


@pytest.mark.skipif(not hasattr(os, "O_BINARY"), reason="binary mode flag is platform-specific")
def test_generate_key_pair_opens_destinations_in_binary_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    private_path = tmp_path / "risk_constitution.private.pem"
    public_path = tmp_path / "risk_constitution.public.pem"
    real_open = os.open
    destination_flags: list[int] = []

    def record_destination_flags(
        path: str | bytes | os.PathLike[str], flags: int, mode: int = 0o777
    ) -> int:
        if isinstance(path, (str, os.PathLike)) and Path(path) in {private_path, public_path}:
            destination_flags.append(flags)
        return real_open(path, flags, mode)

    monkeypatch.setattr(os, "open", record_destination_flags)

    generate_key_pair(private_path, public_path)

    assert len(destination_flags) == 2
    assert all(flags & os.O_BINARY for flags in destination_flags)


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
