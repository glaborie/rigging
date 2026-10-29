"""FreeLLMAPI adapter for Cloudflare's Clef-Flash decision model.

Clef-Flash uses FreeLLMAPI's SystemOne-style ``/v1/decisions`` endpoint; it is
not a chat-completions model and must not be called through ``LLMClient``.
"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from typing import Any

import httpx

from rigging.config import Settings
from rigging.errors import RiggingError, TransportError
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
        if not questions:
            raise ValueError("at least one decision question is required")
        if len(questions) > 64:
            raise ValueError("Clef-Flash accepts at most 64 questions per request")

        wire_questions: dict[str, Any] = {}
        question_ids: dict[str, tuple[str, tuple[str, ...]]] = {}
        for index, (prompt, choices) in enumerate(questions.items()):
            if not prompt.strip():
                raise ValueError("decision question text must not be empty")
            if isinstance(choices, (str, bytes)):
                raise ValueError(f"question {prompt!r} choices must be a sequence of labels")
            labels = tuple(choices)
            if len(labels) < 2:
                raise ValueError(f"question {prompt!r} requires at least two options")
            if any(not isinstance(label, str) or not label.strip() for label in labels):
                raise ValueError(f"question {prompt!r} options must be non-empty strings")
            if len(set(labels)) != len(labels):
                raise ValueError(f"question {prompt!r} options must be unique")

            question_id = f"q{index}"
            question_ids[question_id] = (prompt, labels)
            wire_questions[question_id] = {
                "type": "choice",
                "instructions": prompt,
                "criteria": {label: label for label in labels},
            }

        payload = {
            "model": self.model_id,
            "state": state,
            "questions": wire_questions,
        }
        try:
            response = self._client.post(
                self.endpoint,
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=payload,
            )
        except httpx.HTTPError as exc:
            raise TransportError(None, str(exc), attempts=1) from exc
        if response.is_error:
            raise TransportError(response.status_code, response.text, attempts=1)
        try:
            body = response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise RiggingError("FreeLLMAPI returned invalid JSON for a decision request") from exc
        if not isinstance(body, dict):
            raise RiggingError("FreeLLMAPI decision response must be a JSON object")
        result = body.get("result", body)
        if not isinstance(result, dict) or not isinstance(result.get("answers"), dict):
            raise RiggingError("FreeLLMAPI decision response is missing answers")

        answers: dict[str, DecisionAnswer] = {}
        for question_id, (prompt, option_labels) in question_ids.items():
            raw = result["answers"].get(question_id)
            if not isinstance(raw, dict):
                raise RiggingError(f"FreeLLMAPI decision response is missing answer {question_id}")
            choice = raw.get("choice")
            if not isinstance(choice, str) or choice not in option_labels:
                raise RiggingError(
                    f"FreeLLMAPI returned an unknown choice for {question_id}: {choice!r}"
                )
            raw_distribution = raw.get("probabilities")
            if not isinstance(raw_distribution, dict):
                raise RiggingError(
                    f"FreeLLMAPI response is missing probabilities for {question_id}"
                )
            distribution: dict[str, float] = {}
            for label in option_labels:
                value = raw_distribution.get(label)
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise RiggingError(
                        f"FreeLLMAPI returned an invalid probability for {question_id}/{label}"
                    )
                probability = float(value)
                if not math.isfinite(probability) or not 0.0 <= probability <= 1.0:
                    raise RiggingError(
                        f"FreeLLMAPI returned an out-of-range probability for {question_id}/{label}"
                    )
                distribution[label] = probability
            answers[prompt] = DecisionAnswer(
                question=prompt,
                choice=choice,
                probability=distribution[choice],
                distribution=distribution,
            )
        return answers

    def close(self) -> None:
        """Close the owned HTTP client; injected clients remain caller-owned."""
        if self._owns_client:
            self._client.close()
