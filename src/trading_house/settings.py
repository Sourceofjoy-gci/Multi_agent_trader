"""Environment-sourced runtime settings.

Risk limits are deliberately absent from this model. They are readable only
from the Ed25519-verified constitution, so no environment variable can widen
a limit at runtime (invariant I-2).
"""

from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class RuntimeSettings(BaseSettings):
    """Runtime configuration read from the process environment only.

    No ``env_file`` is configured: a checked-in ``.env`` must never become a
    silent source of production configuration.
    """

    model_config = SettingsConfigDict(
        env_prefix="TRADING_HOUSE_",
        extra="ignore",
        frozen=True,
    )

    database_dsn: SecretStr
    constitution_path: Path = Path("config/risk_constitution.yaml")
    constitution_signature_path: Path = Path("config/risk_constitution.yaml.sig")
    constitution_public_key_path: Path = Path("config/risk_constitution.public.pem")
