"""OpenRouter adapter for Respan Span-01 Lite (free decision model).

Span-01 is a behavior-scoring decision model; on OpenRouter it is reached
through the same SystemOne endpoint as other decision models
(``POST /api/v1/systemone``), never through chat completions. The free tier
has a daily cap, so quota errors surface as ``TransportError``.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import httpx

from rigging.config import Settings
from rigging.decide.systemone import decide_via, post_json
from rigging.errors import TransportError
from rigging.protocols import DecisionAnswer
from rigging.registry import decision_model

DEFAULT_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


@decision_model("respan/span-01-lite:free")
class OpenRouterSpan01Lite:
    """Use OpenRouter's SystemOne endpoint to ask choice questions of Span-01 Lite.

    The key defaults to ``OPENROUTER_API_KEY`` and the base URL to OpenRouter
    production; both can be supplied directly for isolated deployments/tests.
    """

    name = "respan/span-01-lite:free"
    model_id = "respan/span-01-lite:free"

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        *,
        timeout: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        config = Settings()
        self.base_url = (base_url or DEFAULT_OPENROUTER_BASE_URL).rstrip("/")
        self.api_key = api_key or config.openrouter_api_key
        if not self.api_key:
            raise ValueError("set OPENROUTER_API_KEY to use respan/span-01-lite:free")
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=timeout)
        self.endpoint = f"{self.base_url}/systemone"

    def decide(
        self, state: str, questions: dict[str, Sequence[str]]
    ) -> dict[str, DecisionAnswer]:
        """Return a choice and calibrated option distribution for each question."""
        return decide_via(self, "OpenRouter", self.model_id, state, questions)

    def send(self, payload: dict[str, Any]) -> Any:
        body = post_json(
            self._client, self.endpoint, self.api_key, payload, provider="OpenRouter"
        )
        if not isinstance(body, dict):
            raise TransportError(None, "OpenRouter decision response must be a JSON object", attempts=1)
        error = body.get("error")
        if isinstance(error, dict) and error.get("message"):
            raise TransportError(
                error.get("code"), str(error["message"]), attempts=1
            )
        return body

    def close(self) -> None:
        """Close the owned HTTP client; injected clients remain caller-owned."""
        if self._owns_client:
            self._client.close()
