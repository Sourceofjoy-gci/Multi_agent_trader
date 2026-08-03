"""Ed25519 signing helpers for the risk constitution."""

import base64
import os
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
    """Generate an Ed25519 keypair without overwriting either destination.

    The two files are published independently; cross-directory atomicity is not
    guaranteed. If a later write fails, any file already created by this call is
    retained for explicit operator recovery rather than risk deleting a replacement.
    """

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
    try:
        _write_new_file(private_path, private_bytes, 0o600)
        _write_new_file(public_path, public_bytes, 0o644)
    except OSError as error:
        raise SignatureVerificationError() from error


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


def _write_new_file(destination: Path, contents: bytes, mode: int) -> None:
    """Write bytes only through a newly and exclusively created descriptor."""

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(destination, flags, mode)
    try:
        if os.name == "posix":
            os.fchmod(descriptor, mode)  # type: ignore[attr-defined]
        _write_all(descriptor, contents)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_all(descriptor: int, contents: bytes) -> None:
    remaining = memoryview(contents)
    while remaining:
        written = os.write(descriptor, remaining)
        if written == 0:
            raise OSError("could not write key material")
        remaining = remaining[written:]
