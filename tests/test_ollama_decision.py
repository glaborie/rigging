from __future__ import annotations

import json

import httpx
import pytest

from rigging.decide.ollama import OllamaD1Omni
from rigging.errors import RiggingError, TransportError

QUESTION = "Which team should handle this request?"
OPTIONS = ["billing", "technical", "sales"]
STATE = "Our checkout has returned 500 errors since 9am."


def test_ollama_d1_posts_systemone_choice_without_auth() -> None:
    captured: dict[str, object] = {}

    def respond(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "model": "d1-omni",
                "answers": {
                    "q0": {
                        "type": "choice",
                        "choice": "technical",
                        "probabilities": {"billing": 0.01, "technical": 0.98, "sales": 0.01},
                        "confidence": 0.97,
                    }
                },
                "usage": {"input_tokens": 174, "output_tokens": 0},
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(respond))
    model = OllamaD1Omni(client=client)
    answer = model.decide(STATE, {QUESTION: OPTIONS})

    assert captured["url"] == "http://localhost:11434/v1/systemone"
    assert captured["auth"] is None
    assert captured["body"] == {
        "model": "d1-omni",
        "state": STATE,
        "questions": {
            "q0": {
                "type": "choice",
                "instructions": QUESTION,
                "criteria": {option: option for option in OPTIONS},
            }
        },
    }
    assert answer[QUESTION].choice == "technical"
    assert answer[QUESTION].probability == 0.98
    assert answer[QUESTION].distribution["billing"] == 0.01
    client.close()


def test_ollama_base_url_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://ollama.local:11434/")
    model = OllamaD1Omni()
    assert model.base_url == "http://ollama.local:11434"
    assert model.endpoint == "http://ollama.local:11434/v1/systemone"
    model.close()


def test_ollama_http_errors_become_transport_error() -> None:
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(404, text="model not found")
        )
    )
    model = OllamaD1Omni(client=client)
    with pytest.raises(TransportError, match="status=404"):
        model.decide(STATE, {QUESTION: OPTIONS})
    client.close()


def test_ollama_invalid_choice_is_rejected() -> None:
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={"answers": {"q0": {"type": "choice", "choice": "other", "probabilities": {}}}},
            )
        )
    )
    model = OllamaD1Omni(client=client)
    with pytest.raises(RiggingError, match="unknown choice"):
        model.decide(STATE, {QUESTION: OPTIONS})
    client.close()
