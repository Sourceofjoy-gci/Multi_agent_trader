"""Ed25519 signing helpers for the risk constitution."""

import base64
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
    """Generate an unencrypted PKCS8 Ed25519 keypair at the supplied paths."""

    private_key = Ed25519PrivateKey.generate()
    private_path.parent.mkdir(parents=True, exist_ok=True)
    public_path.parent.mkdir(parents=True, exist_ok=True)
    private_path.write_bytes(
        private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    public_path.write_bytes(
        private_key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )


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
