"""OpenRouter adapter for Respan Span-01 Lite (free decision model).

Span-01 is a behaviour-scoring decision model reached through OpenRouter's
SystemOne endpoint (``POST /api/v1/systemone``), never chat completions. It
accepts **only ``noul`` questions** (plain-string instructions, probability of
"yes"), so a multi-option ``choice`` question is fanned out to one noul per
option and the option scores are renormalised into a distribution.

The free tier has a daily cap; quota errors surface as ``TransportError``.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

import httpx

from rigging.config import Settings
from rigging.decide.systemone import build_questions, post_json
from rigging.errors import RiggingError, TransportError
from rigging.protocols import DecisionAnswer
from rigging.registry import decision_model

DEFAULT_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


@decision_model("respan/span-01-lite:free")
class OpenRouterSpan01Lite:
    """Ask choice questions of Span-01 Lite through OpenRouter's SystemOne API.

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
        _, specs = build_questions(questions)
        wire: dict[str, Any] = {}
        plan: dict[str, tuple[str, tuple[str, ...], list[str]]] = {}
        for question_id, (prompt, labels) in specs.items():
            ids: list[str] = []
            for index, label in enumerate(labels):
                noul_id = f"{question_id}__{index}"
                wire[noul_id] = {
                    "type": "noul",
                    "instructions": (
                        f"For the question '{prompt}', the correct answer is {label}."
                    ),
                }
                ids.append(noul_id)
            plan[question_id] = (prompt, labels, ids)

        body = self.send({"model": self.model_id, "state": state, "questions": wire})
        result = body.get("result", body) if isinstance(body, dict) else body
        answers_raw = result.get("answers") if isinstance(result, dict) else None
        if not isinstance(answers_raw, dict):
            raise RiggingError("OpenRouter decision response is missing answers")

        answers: dict[str, DecisionAnswer] = {}
        for prompt, labels, ids in plan.values():
            scores: dict[str, float] = {}
            for label, noul_id in zip(labels, ids, strict=True):
                raw = answers_raw.get(noul_id)
                if not isinstance(raw, dict):
                    raise RiggingError(
                        f"OpenRouter decision response is missing answer {noul_id}"
                    )
                value = raw.get("noul")
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise RiggingError(
                        f"OpenRouter returned an invalid noul for {noul_id}: {value!r}"
                    )
                score = float(value)
                if not math.isfinite(score) or not 0.0 <= score <= 1.0:
                    raise RiggingError(
                        f"OpenRouter returned an out-of-range noul for {noul_id}"
                    )
                scores[label] = score
            total = sum(scores.values())
            if total <= 0.0:
                raise RiggingError(
                    f"OpenRouter returned all-zero scores for question {prompt!r}"
                )
            distribution = {label: score / total for label, score in scores.items()}
            choice = max(scores, key=lambda label: scores[label])
            answers[prompt] = DecisionAnswer(
                question=prompt,
                choice=choice,
                probability=distribution[choice],
                distribution=distribution,
            )
        return answers

    def send(self, payload: dict[str, Any]) -> Any:
        body = post_json(
            self._client, self.endpoint, self.api_key, payload, provider="OpenRouter"
        )
        if not isinstance(body, dict):
            raise TransportError(
                None, "OpenRouter decision response must be a JSON object", attempts=1
            )
        error = body.get("error")
        if isinstance(error, dict) and error.get("message"):
            raise TransportError(error.get("code"), str(error["message"]), attempts=1)
        return body

    def close(self) -> None:
        """Close the owned HTTP client; injected clients remain caller-owned."""
        if self._owns_client:
            self._client.close()
