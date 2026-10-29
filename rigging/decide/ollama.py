"""Local Ollama adapter for Liquid AI's d1-omni decision model.

Ollama serves SystemOne locally at ``/v1/systemone`` (Ollama >= 0.35).
No API key, hosted provider, quota, or model-specific wire shim is required;
choice questions use the same shared SystemOne contract as other providers.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import httpx

from rigging.config import Settings
from rigging.decide.systemone import build_questions, map_answers, post_json
from rigging.protocols import DecisionAnswer
from rigging.registry import decision_model


@decision_model("d1-omni")
class OllamaD1Omni:
    """Run d1-omni locally through Ollama's native SystemOne endpoint."""

    name = "d1-omni"
    model_id = "d1-omni"

    def __init__(
        self,
        base_url: str | None = None,
        *,
        timeout: float = 120.0,
        client: httpx.Client | None = None,
    ) -> None:
        config = Settings()
        self.base_url = (base_url or config.ollama_base_url).rstrip("/")
        self.endpoint = f"{self.base_url}/v1/systemone"
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=timeout)

    def decide(
        self, state: str, questions: dict[str, Sequence[str]]
    ) -> dict[str, DecisionAnswer]:
        """Return Ollama's choice and per-option probabilities."""
        wire_questions, question_ids = build_questions(questions)
        payload = {"model": self.model_id, "state": state, "questions": wire_questions}
        body = self.send(payload)
        return map_answers(body, question_ids, provider="Ollama")

    def send(self, payload: dict[str, Any]) -> Any:
        return post_json(
            self._client,
            self.endpoint,
            None,
            payload,
            provider="Ollama",
        )

    def close(self) -> None:
        """Close the owned HTTP client; injected clients remain caller-owned."""
        if self._owns_client:
            self._client.close()
