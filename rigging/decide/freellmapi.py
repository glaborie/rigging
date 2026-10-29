"""FreeLLMAPI adapter for Cloudflare's Clef-Flash decision model.

Clef-Flash uses FreeLLMAPI's SystemOne-style ``/v1/decisions`` endpoint; it is
not a chat-completions model and must not be called through ``LLMClient``.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import httpx

from rigging.config import Settings
from rigging.decide.systemone import decide_via, post_json
from rigging.protocols import DecisionAnswer
from rigging.registry import decision_model


@decision_model("@cf/cloudflare/clef-flash")
class FreeLLMClefFlash:
    """Use FreeLLMAPI to ask typed choice questions of Cloudflare Clef-Flash.

    The endpoint and unified FreeLLMAPI key default to ``FREELLM_BASE_URL``
    (default ``http://localhost:3001/v1``) and ``FREELLM_API_KEY``. Values can
    also be supplied directly, which is useful for isolated deployments/tests.
    """

    name = "@cf/cloudflare/clef-flash"
    model_id = "@cf/cloudflare/clef-flash"

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        *,
        timeout: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        config = Settings()
        self.base_url = (base_url or config.freellm_base_url).rstrip("/")
        self.api_key = api_key or config.freellm_api_key
        if not self.api_key:
            raise ValueError("set FREELLM_API_KEY to use @cf/cloudflare/clef-flash")
        if not self.base_url:
            raise ValueError("set FREELLM_BASE_URL to the FreeLLMAPI /v1 endpoint")
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=timeout)
        self.endpoint = f"{self.base_url}/decisions"

    def decide(
        self, state: str, questions: dict[str, Sequence[str]]
    ) -> dict[str, DecisionAnswer]:
        """Return a choice and calibrated option distribution for each question."""
        return decide_via(self, "FreeLLMAPI", self.model_id, state, questions)

    def send(self, payload: dict[str, Any]) -> Any:
        return post_json(
            self._client, self.endpoint, self.api_key, payload, provider="FreeLLMAPI"
        )

    def close(self) -> None:
        """Close the owned HTTP client; injected clients remain caller-owned."""
        if self._owns_client:
            self._client.close()
