from __future__ import annotations

import json

import httpx
import pytest

from rigging.decide.openrouter import OpenRouterSpan01Lite
from rigging.errors import TransportError

QUESTION = "Which kind of model should answer this?"
STATE = "my react useEffect runs twice, why?"
OPTIONS = ["chat", "code", "reasoning", "writing"]


def test_openrouter_posts_systemone_payload_and_maps_probabilities() -> None:
    captured: dict[str, object] = {}

    def respond(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "gen-dec-1",
                "model": "respan/span-01-lite",
                "provider": "Respan",
                "result": {
                    "answers": {
                        "q0": {
                            "type": "choice",
                            "choice": "code",
                            "confidence": 0.88,
                            "probabilities": {
                                "chat": 0.03,
                                "code": 0.88,
                                "reasoning": 0.05,
                                "writing": 0.04,
                            },
                        }
                    }
                },
                "usage": {"input_tokens": 42, "output_tokens": 0},
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(respond))
    model = OpenRouterSpan01Lite(api_key="test-api-key", client=client)
    answer = model.decide(STATE, {QUESTION: OPTIONS})

    assert captured["url"] == "https://openrouter.ai/api/v1/systemone"
    assert captured["auth"] == "Bearer test-api-key"
    assert captured["body"] == {
        "model": "respan/span-01-lite:free",
        "state": STATE,
        "questions": {
            "q0": {
                "type": "choice",
                "instructions": QUESTION,
                "criteria": {option: option for option in OPTIONS},
            }
        },
    }
    assert answer[QUESTION].choice == "code"
    assert answer[QUESTION].probability == 0.88
    assert answer[QUESTION].distribution["chat"] == 0.03
    client.close()


def test_openrouter_reads_key_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "key-from-env")
    model = OpenRouterSpan01Lite()
    assert model.api_key == "key-from-env"
    assert model.base_url == "https://openrouter.ai/api/v1"
    model.close()


def test_openrouter_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
        OpenRouterSpan01Lite()


def test_openrouter_turns_rate_limit_into_transport_error() -> None:
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                429,
                json={"error": {"message": "Rate limit exceeded"}},
            )
        )
    )
    model = OpenRouterSpan01Lite(api_key="test-api-key", client=client)
    with pytest.raises(TransportError, match="status=429"):
        model.decide("state", {"pick": ["a", "b"]})
    client.close()


def test_openrouter_rejects_unknown_choice_and_missing_probabilities() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        answer = {"type": "choice", "choice": "banana", "probabilities": {"chat": 1.0}}
        if "banana" in json.dumps(body):
            pass  # first call: unknown choice
        return httpx.Response(
            200,
            json={"result": {"answers": {"q0": dict(answer)}}},
        )

    client = httpx.Client(transport=httpx.MockTransport(respond))
    model = OpenRouterSpan01Lite(api_key="test-api-key", client=client)
    with pytest.raises(Exception, match="unknown choice"):
        model.decide(STATE, {QUESTION: OPTIONS})

    def respond_missing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"result": {"answers": {"q0": {"type": "choice", "choice": "code"}}}},
        )

    model._client = httpx.Client(transport=httpx.MockTransport(respond_missing))
    with pytest.raises(Exception, match="missing probabilities"):
        model.decide(STATE, {QUESTION: OPTIONS})
    client.close()
