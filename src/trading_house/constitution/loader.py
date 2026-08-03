"""Verified loading for the risk constitution."""

import hashlib
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives import serialization

from trading_house.constitution.models import Constitution, parse_constitution_yaml
from trading_house.constitution.signing import decode_signature, load_public_key, verify_signature
from trading_house.core.errors import ConfigurationError


@dataclass(frozen=True, slots=True)
class LoadedConstitution:
    """A parsed constitution and immutable verification metadata."""

    constitution: Constitution
    constitution_sha256: str
    public_key_fingerprint: str


def load_constitution(
    yaml_path: Path, signature_path: Path, public_key_path: Path
) -> LoadedConstitution:
    """Verify exact YAML bytes before parsing and return verified metadata."""

    try:
        yaml_bytes = yaml_path.read_bytes()
    except OSError as error:
        raise ConfigurationError() from error

    signature = decode_signature(signature_path.read_bytes())
    public_key = load_public_key(public_key_path.read_bytes())
    verify_signature(public_key, signature, yaml_bytes)
    constitution = parse_constitution_yaml(yaml_bytes)
    raw_public_key = public_key.public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    return LoadedConstitution(
        constitution=constitution,
        constitution_sha256=hashlib.sha256(yaml_bytes).hexdigest(),
        public_key_fingerprint=hashlib.sha256(raw_public_key).hexdigest(),
    )
