"""Settings from the environment. Secrets live in .env, never in a manifest.

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
    model: str = "deepseek/deepseek-v4.1-flash"

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

    # web view auth (ADR-0021): comma-separated to allow rotation. Empty means no
    # auth, which `harness serve` only allows on a loopback bind.
    service_token: str = Field(default="", validation_alias="RIGGING_SERVICE_TOKEN")
    # Per-client request cap for /api/*; 0 disables it. A backstop, not a
    # replacement for the proxy's limiter (docs/threat-model.md).
    service_rate_limit: int = Field(default=240,
                                    validation_alias="RIGGING_SERVICE_RATE_LIMIT")
    # A bearer token over plain HTTP is refused on a non-loopback bind unless the
    # operator says the network already protects the link.
    service_allow_plaintext: bool = Field(
        default=False, validation_alias="RIGGING_SERVICE_ALLOW_PLAINTEXT"
    )
    # Which peers may set X-Forwarded-For, and therefore choose the rate-limit
    # key. Default is a proxy on the same host. Never "*" (checked at startup).
    service_trusted_proxies: str = Field(
        default="127.0.0.1,::1",
        validation_alias="RIGGING_SERVICE_TRUSTED_PROXIES",
    )

    # subagents (ADR-0019): how deep spawn_* tools may nest. Enforced at runtime,
    # not per manifest, so a manifest cannot promote itself.
    subagent_max_depth: int = 1

    # observability
    # Comma-separated labels sent as `langfuse.trace.tags` on every span, so a
    # run can be filtered in the UI (v4 filters observations, not traces).
    # `scripts/bench.py` sets this per model and repeat; nothing else needs it.
    trace_tags: str = Field(default="", validation_alias="RIGGING_TRACE_TAGS")
    # The trace's name in Langfuse (`langfuse.trace.name`). Without it every run
    # shares the root span's name, so a scan of 33 probes is 33 rows called
    # `invoke_agent`. `redteam/provider.py` sets this per probe.
    trace_name: str = Field(default="", validation_alias="RIGGING_TRACE_NAME")
    # MLflow exporter (ADR-0020): the tracking server is HTTP, so no SDK is needed.
    mlflow_tracking_uri: str = Field(default="", validation_alias="MLFLOW_TRACKING_URI")
    mlflow_experiment_id: str = Field(default="0", validation_alias="MLFLOW_EXPERIMENT_ID")
    mlflow_tracking_token: str = Field(default="", validation_alias="MLFLOW_TRACKING_TOKEN")
    # PII at rest (ADR-0027). Empty key = sentinel maps stay in memory and tool
    # rows are plaintext; a key seals both and is never written anywhere itself.
    pii_key: str = Field(default="", validation_alias="RIGGING_PII_KEY")
    pii_vault_dir: str = Field(default="", validation_alias="RIGGING_PII_VAULT_DIR")
    pii_vault_ttl_s: float = Field(default=3600.0, validation_alias="RIGGING_PII_VAULT_TTL_S")

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
