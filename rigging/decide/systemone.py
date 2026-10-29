"""Shared SystemOne request/response plumbing for decision-model adapters.

FreeLLMAPI's ``/v1/decisions`` and OpenRouter's ``/api/v1/systemone`` speak the
same wire contract (ADR-0019): ``model`` + ``state`` + ``questions`` in,
``answers`` with ``choice``/``probabilities`` out. The adapters are therefore
thin: identity (model id, base URL, key) plus one ``send()``.
"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from typing import Any, Protocol

import httpx

from rigging.errors import RiggingError, TransportError
from rigging.protocols import DecisionAnswer


def build_questions(questions: dict[str, Sequence[str]]) -> tuple[dict[str, Any], dict[str, tuple[str, tuple[str, ...]]]]:
    """Validate question specs and build the wire ``questions`` object."""
    if not questions:
        raise ValueError("at least one decision question is required")
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
    return wire_questions, question_ids


def map_answers(
    body: Any,
    question_ids: dict[str, tuple[str, tuple[str, ...]]],
    *,
    provider: str,
) -> dict[str, DecisionAnswer]:
    """Map a SystemOne response body onto typed answers, failing loudly."""
    if not isinstance(body, dict):
        raise RiggingError(f"{provider} decision response must be a JSON object")
    result = body.get("result", body)
    if not isinstance(result, dict) or not isinstance(result.get("answers"), dict):
        raise RiggingError(f"{provider} decision response is missing answers")

    answers: dict[str, DecisionAnswer] = {}
    for question_id, (prompt, option_labels) in question_ids.items():
        raw = result["answers"].get(question_id)
        if not isinstance(raw, dict):
            raise RiggingError(f"{provider} decision response is missing answer {question_id}")
        choice = raw.get("choice")
        if not isinstance(choice, str) or choice not in option_labels:
            raise RiggingError(
                f"{provider} returned an unknown choice for {question_id}: {choice!r}"
            )
        raw_distribution = raw.get("probabilities")
        if not isinstance(raw_distribution, dict):
            raise RiggingError(
                f"{provider} response is missing probabilities for {question_id}"
            )
        distribution: dict[str, float] = {}
        for label in option_labels:
            value = raw_distribution.get(label)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise RiggingError(
                    f"{provider} returned an invalid probability for {question_id}/{label}"
                )
            probability = float(value)
            if not math.isfinite(probability) or not 0.0 <= probability <= 1.0:
                raise RiggingError(
                    f"{provider} returned an out-of-range probability for {question_id}/{label}"
                )
            distribution[label] = probability
        answers[prompt] = DecisionAnswer(
            question=prompt,
            choice=choice,
            probability=distribution[choice],
            distribution=distribution,
        )
    return answers


class SystemOneSender(Protocol):
    """The one operation a decision adapter contributes: an HTTP send."""

    def send(self, payload: dict[str, Any]) -> Any:
        """POST the payload, return the parsed JSON body or raise."""
        ...


def decide_via(
    sender: SystemOneSender,
    provider: str,
    model_id: str,
    state: str,
    questions: dict[str, Sequence[str]],
) -> dict[str, DecisionAnswer]:
    """Build, send, and map one SystemOne decision request."""
    wire_questions, question_ids = build_questions(questions)
    payload = {"model": model_id, "state": state, "questions": wire_questions}
    body = sender.send(payload)
    return map_answers(body, question_ids, provider=provider)


def post_json(
    client: httpx.Client,
    endpoint: str,
    api_key: str | None,
    payload: dict[str, Any],
    *,
    provider: str,
) -> Any:
    """Shared HTTP path: bearer auth, error mapping, JSON parse."""
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        response = client.post(
            endpoint,
            headers=headers,
            json=payload,
        )
    except httpx.HTTPError as exc:
        raise TransportError(None, str(exc), attempts=1) from exc
    if response.is_error:
        raise TransportError(response.status_code, response.text, attempts=1)
    try:
        return response.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise RiggingError(f"{provider} returned invalid JSON for a decision request") from exc
