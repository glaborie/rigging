"""Settings from the environment. Secrets live in .env, never in a manifest (ADR-0028/Q28).

A manifest names a `credential_profile`; the profile resolves to a base_url + key
here. Switching gateways is therefore a config change, not a code change.
"""

from __future__ import annotations

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Profile(BaseModel):
    base_url: str
    api_key: str = "not-needed"
    label: str = ""


class Settings(BaseSettings):
    # extra="allow" is how credential profiles stay open-ended: any
    # RIGGING_PROFILE_<NAME>_BASE_URL / _API_KEY in .env or the environment lands
    # in model_extra as profile_<name>_base_url / _api_key, with env beating .env.
    model_config = SettingsConfigDict(env_prefix="RIGGING_", env_file=".env",
                                      extra="allow")

    # default gateway: OmniRoute locally, or Ollama's OpenAI-compatible surface
    base_url: str = "http://localhost:20128/v1"
    api_key: str = "not-needed"
    model: str = "beam:cloud"

    # FreeLLMAPI decision endpoint (SystemOne API, not chat completions)
    freellm_base_url: str = Field(
        default="http://localhost:3001/v1", validation_alias="FREELLM_BASE_URL"
    )
    freellm_api_key: str = Field(default="", validation_alias="FREELLM_API_KEY")

    # OpenRouter decision endpoint (SystemOne API, same contract as FreeLLMAPI)
    openrouter_api_key: str = Field(default="", validation_alias="OPENROUTER_API_KEY")

    # Local Ollama decision endpoint (no API key); use OLLAMA_BASE_URL for remote host
    ollama_base_url: str = Field(
        default="http://localhost:11434", validation_alias="OLLAMA_BASE_URL"
    )

    # Langfuse exporter: read from .env like every other key, so `harness run`
    # works without sourcing the file by hand. Empty host means cloud default.
    langfuse_host: str = Field(default="", validation_alias="LANGFUSE_HOST")
    langfuse_public_key: str = Field(default="", validation_alias="LANGFUSE_PUBLIC_KEY")
    langfuse_secret_key: str = Field(default="", validation_alias="LANGFUSE_SECRET_KEY")

    db_path: str = "rigging.db"
    prices_file: str = "prices.yaml"
    request_timeout_s: float = 120.0
    max_retries: int = 3

    # observability
    otlp_endpoint: str = ""
    otlp_headers: str = ""

    def profile(self, name: str = "default") -> Profile:
        """Named profiles come from RIGGING_PROFILE_<NAME>_BASE_URL / _API_KEY.

        They are read from `.env` too (see `extra="allow"` above), so a manifest
        naming `credential_profile: ollama` works with no shell exports. Precedence
        is pydantic-settings': real environment beats `.env`.
        """
        import os

        upper = name.upper()
        extra = self.model_extra or {}
        prefix = f"profile_{name.lower()}_"
        base = (
            os.environ.get(f"RIGGING_PROFILE_{upper}_BASE_URL")
            or extra.get(f"{prefix}base_url")
            or self.base_url
        )
        key = (
            os.environ.get(f"RIGGING_PROFILE_{upper}_API_KEY")
            or extra.get(f"{prefix}api_key")
            or self.api_key
        )
        return Profile(base_url=str(base), api_key=str(key), label=name)


settings = Settings()
