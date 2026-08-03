"""Ed25519 signing helpers for the risk constitution."""

import base64
import os
import stat
import tempfile
from binascii import Error as Base64Error
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from trading_house.core.errors import SignatureVerificationError


def load_private_key(pem: bytes) -> Ed25519PrivateKey:
    """Load an unencrypted PKCS8 Ed25519 private key from PEM bytes."""

    try:
        private_key = serialization.load_pem_private_key(pem, password=None)
        if not isinstance(private_key, Ed25519PrivateKey):
            raise TypeError("private key is not Ed25519")
        return private_key
    except (TypeError, ValueError) as error:
        raise SignatureVerificationError() from error


def load_public_key(pem: bytes) -> Ed25519PublicKey:
    """Load an Ed25519 SubjectPublicKeyInfo public key from PEM bytes."""

    try:
        public_key = serialization.load_pem_public_key(pem)
        if not isinstance(public_key, Ed25519PublicKey):
            raise TypeError("public key is not Ed25519")
        return public_key
    except (TypeError, ValueError) as error:
        raise SignatureVerificationError() from error


def sign_bytes(private_key: Ed25519PrivateKey, data: bytes) -> bytes:
    """Return the 64-byte Ed25519 signature for the exact input bytes."""

    try:
        signature = private_key.sign(data)
        if len(signature) != 64:
            raise ValueError("Ed25519 signatures must be 64 bytes")
        return signature
    except (AttributeError, TypeError, ValueError) as error:
        raise SignatureVerificationError() from error


def verify_signature(public_key: Ed25519PublicKey, signature: bytes, data: bytes) -> None:
    """Verify a 64-byte Ed25519 signature for the exact input bytes."""

    try:
        if not isinstance(public_key, Ed25519PublicKey):
            raise TypeError("public key is not Ed25519")
        if len(signature) != 64:
            raise ValueError("Ed25519 signatures must be 64 bytes")
        public_key.verify(signature, data)
    except (InvalidSignature, TypeError, ValueError) as error:
        raise SignatureVerificationError() from error


def generate_key_pair(private_path: Path, public_path: Path) -> None:
    """Generate an unencrypted PKCS8 Ed25519 keypair without overwriting files."""

    private_key = Ed25519PrivateKey.generate()
    private_path.parent.mkdir(parents=True, exist_ok=True)
    public_path.parent.mkdir(parents=True, exist_ok=True)
    if _same_destination(private_path, public_path):
        raise ValueError("private and public key destinations must differ")
    _reject_existing_destination(private_path)
    _reject_existing_destination(public_path)

    private_bytes = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    public_bytes = private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    staged_paths: list[Path] = []
    created_paths: list[tuple[Path, tuple[int, int]]] = []
    try:
        private_staged_path = _stage_file(private_path, private_bytes, 0o600)
        staged_paths.append(private_staged_path)
        public_staged_path = _stage_file(public_path, public_bytes, 0o644)
        staged_paths.append(public_staged_path)

        private_identity = _publish_staged_file(private_staged_path, private_path, 0o600)
        created_paths.append((private_path, private_identity))
        public_identity = _publish_staged_file(public_staged_path, public_path, 0o644)
        created_paths.append((public_path, public_identity))
    except BaseException:
        for path, identity in reversed(created_paths):
            _remove_if_unchanged(path, identity)
        raise
    finally:
        for staged_path in staged_paths:
            _remove_if_present(staged_path)


def sign_file(constitution_path: Path, private_path: Path, signature_path: Path) -> None:
    """Sign exact constitution bytes and write their Base64 signature."""

    private_key = load_private_key(private_path.read_bytes())
    signature = sign_bytes(private_key, constitution_path.read_bytes())
    signature_path.parent.mkdir(parents=True, exist_ok=True)
    signature_path.write_bytes(base64.b64encode(signature) + b"\n")


def decode_signature(encoded_signature: bytes) -> bytes:
    """Decode and validate a Base64-encoded Ed25519 signature."""

    try:
        signature = base64.b64decode(encoded_signature.strip(), validate=True)
        if len(signature) != 64:
            raise ValueError("Ed25519 signatures must be 64 bytes")
        return signature
    except (Base64Error, ValueError) as error:
        raise SignatureVerificationError() from error


def _same_destination(first_path: Path, second_path: Path) -> bool:
    return os.path.normcase(os.path.abspath(first_path)) == os.path.normcase(
        os.path.abspath(second_path)
    )


def _reject_existing_destination(path: Path) -> None:
    try:
        os.lstat(path)
    except FileNotFoundError:
        return
    raise FileExistsError()


def _stage_file(destination: Path, contents: bytes, mode: int) -> Path:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        if os.name == "posix":
            os.fchmod(descriptor, mode)  # type: ignore[attr-defined]
        _write_all(descriptor, contents)
        os.fsync(descriptor)
    except BaseException:
        os.close(descriptor)
        _remove_if_present(temporary_path)
        raise
    else:
        os.close(descriptor)
        return temporary_path


def _publish_staged_file(staged_path: Path, destination: Path, mode: int) -> tuple[int, int]:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(destination, flags, mode)
    identity = _file_identity(os.fstat(descriptor))
    try:
        if os.name == "posix":
            os.fchmod(descriptor, mode)  # type: ignore[attr-defined]
        _write_all(descriptor, staged_path.read_bytes())
        os.fsync(descriptor)
    except BaseException:
        os.close(descriptor)
        _remove_if_unchanged(destination, identity)
        raise
    else:
        os.close(descriptor)
        return identity


def _write_all(descriptor: int, contents: bytes) -> None:
    remaining = memoryview(contents)
    while remaining:
        written = os.write(descriptor, remaining)
        if written == 0:
            raise OSError("could not write key material")
        remaining = remaining[written:]


def _file_identity(file_stat: os.stat_result) -> tuple[int, int]:
    return file_stat.st_dev, file_stat.st_ino


def _remove_if_unchanged(path: Path, identity: tuple[int, int]) -> None:
    try:
        file_stat = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISREG(file_stat.st_mode) and _file_identity(file_stat) == identity:
        os.unlink(path)


def _remove_if_present(path: Path) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        return
