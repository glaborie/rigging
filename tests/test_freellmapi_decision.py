from __future__ import annotations

import json

import httpx
import pytest

from rigging.decide.freellmapi import FreeLLMClefFlash
from rigging.errors import TransportError

QUESTION = "Which kind of model should answer this?"
STATE = "my react useEffect runs twice, why?"
OPTIONS = ["chat", "code", "reasoning", "writing"]


def test_freellmapi_posts_decision_payload_and_maps_probabilities() -> None:
    captured: dict[str, object] = {}

    def respond(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "object": "decision",
                "model": "@cf/cloudflare/clef-flash",
                "provider": "cloudflare",
                "answers": {
                    "q0": {
                        "type": "choice",
                        "choice": "code",
                        "confidence": 0.91,
                        "probabilities": {
                            "chat": 0.02,
                            "code": 0.91,
                            "reasoning": 0.04,
                            "writing": 0.03,
                        },
                    }
                },
                "usage": {"input_tokens": 17, "output_tokens": 0},
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(respond))
    model = FreeLLMClefFlash(
        base_url="http://freellm.test/v1",
        api_key="test-api-key",
        client=client,
    )
    answer = model.decide(STATE, {QUESTION: OPTIONS})

    assert captured["url"] == "http://freellm.test/v1/decisions"
    assert captured["auth"] == "Bearer test-api-key"
    assert captured["body"] == {
        "model": "@cf/cloudflare/clef-flash",
        "state": STATE,
        "questions": {
            "q0": {
                "type": "choice",
                "instructions": QUESTION,
                "criteria": {option: option for option in OPTIONS},
            }
        },
    }
    assert answer[QUESTION].question == QUESTION
    assert answer[QUESTION].choice == "code"
    assert answer[QUESTION].probability == 0.91
    assert answer[QUESTION].distribution == {
        "chat": 0.02,
        "code": 0.91,
        "reasoning": 0.04,
        "writing": 0.03,
    }
    client.close()


def test_freellmapi_reads_configured_endpoint_and_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FREELLM_BASE_URL", "http://gateway.example/v1")
    monkeypatch.setenv("FREELLM_API_KEY", "key-from-env")
    model = FreeLLMClefFlash()
    assert model.base_url == "http://gateway.example/v1"
    assert model.api_key == "key-from-env"
    model.close()


def test_freellmapi_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FREELLM_API_KEY", "")
    with pytest.raises(ValueError, match="FREELLM_API_KEY"):
        FreeLLMClefFlash()


def test_freellmapi_turns_rate_limit_into_transport_error() -> None:
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                429,
                json={"error": {"message": "decision model quota exhausted"}},
            )
        )
    )
    model = FreeLLMClefFlash(
        base_url="http://freellm.test/v1", api_key="test-api-key", client=client
    )
    with pytest.raises(TransportError, match="status=429"):
        model.decide("state", {"pick": ["a", "b"]})
    client.close()


def test_freellmapi_requires_multiple_unique_options() -> None:
    client = httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(500)))
    model = FreeLLMClefFlash(
        base_url="http://freellm.test/v1", api_key="test-api-key", client=client
    )
    with pytest.raises(ValueError, match="at least two options"):
        model.decide("state", {"pick": ["only one"]})
    with pytest.raises(ValueError, match="unique"):
        model.decide("state", {"pick": ["duplicate", "duplicate"]})
    client.close()
