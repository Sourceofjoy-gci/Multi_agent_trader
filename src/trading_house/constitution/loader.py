"""Verified loading for any signed artifact, plus the risk constitution atop it."""

import hashlib
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives import serialization

from trading_house.constitution.models import Constitution, parse_constitution_yaml
from trading_house.constitution.signing import decode_signature, load_public_key, verify_signature
from trading_house.core.errors import ConfigurationError, SignatureVerificationError


@dataclass(frozen=True, slots=True)
class VerifiedArtifact:
    """Exact verified bytes plus immutable provenance."""

    content: bytes
    sha256: str
    public_key_fingerprint: str


@dataclass(frozen=True, slots=True)
class LoadedConstitution:
    """A parsed constitution and immutable verification metadata."""

    constitution: Constitution
    constitution_sha256: str
    public_key_fingerprint: str


def load_signed(
    artifact_path: Path, signature_path: Path, public_key_path: Path
) -> VerifiedArtifact:
    """Verify exact bytes before any decoding. Never parses."""

    try:
        artifact_bytes = artifact_path.read_bytes()
    except OSError as error:
        raise ConfigurationError() from error

    try:
        signature_bytes = signature_path.read_bytes()
    except OSError as error:
        raise SignatureVerificationError() from error
    signature = decode_signature(signature_bytes)
    try:
        public_key_bytes = public_key_path.read_bytes()
    except OSError as error:
        raise SignatureVerificationError() from error
    public_key = load_public_key(public_key_bytes)
    verify_signature(public_key, signature, artifact_bytes)
    raw_public_key = public_key.public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    return VerifiedArtifact(
        content=artifact_bytes,
        sha256=hashlib.sha256(artifact_bytes).hexdigest(),
        public_key_fingerprint=hashlib.sha256(raw_public_key).hexdigest(),
    )


def load_constitution(
    yaml_path: Path, signature_path: Path, public_key_path: Path
) -> LoadedConstitution:
    """Verify exact YAML bytes before parsing and return verified metadata."""

    verified = load_signed(yaml_path, signature_path, public_key_path)
    return LoadedConstitution(
        constitution=parse_constitution_yaml(verified.content),
        constitution_sha256=verified.sha256,
        public_key_fingerprint=verified.public_key_fingerprint,
    )
