from __future__ import annotations

import json

import httpx
import pytest

from rigging.decide.openrouter import OpenRouterSpan01Lite
from rigging.errors import RiggingError, TransportError

QUESTION = "Which kind of model should answer this?"
STATE = "my react useEffect runs twice, why?"
OPTIONS = ["chat", "code", "reasoning", "writing"]


def _noul_response(nouls: dict[str, float]) -> dict[str, object]:
    return {
        "id": "gen-dec-1",
        "model": "respan/span-01-lite-20260925",
        "provider": "Respan",
        "answers": {qid: {"type": "noul", "noul": p} for qid, p in nouls.items()},
        "usage": {"input_tokens": 42, "output_tokens": 0, "cost": 0},
    }


def test_openrouter_fans_choice_out_to_noul_and_renormalises() -> None:
    captured: dict[str, object] = {}

    def respond(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=_noul_response({
            "q0__0": 0.10,
            "q0__1": 0.70,
            "q0__2": 0.15,
            "q0__3": 0.05,
        }))

    client = httpx.Client(transport=httpx.MockTransport(respond))
    model = OpenRouterSpan01Lite(api_key="test-api-key", client=client)
    answer = model.decide(STATE, {QUESTION: OPTIONS})

    body = captured["body"]
    assert body["model"] == "respan/span-01-lite:free"
    assert body["state"] == STATE
    # one noul question per option, no criteria key
    assert body["questions"] == {
        "q0__0": {
            "type": "noul",
            "instructions": f"For the question '{QUESTION}', the correct answer is chat.",
        },
        "q0__1": {
            "type": "noul",
            "instructions": f"For the question '{QUESTION}', the correct answer is code.",
        },
        "q0__2": {
            "type": "noul",
            "instructions": f"For the question '{QUESTION}', the correct answer is reasoning.",
        },
        "q0__3": {
            "type": "noul",
            "instructions": f"For the question '{QUESTION}', the correct answer is writing.",
        },
    }
    assert captured["url"] == "https://openrouter.ai/api/v1/systemone"
    assert captured["auth"] == "Bearer test-api-key"

    assert answer[QUESTION].choice == "code"
    total = 0.10 + 0.70 + 0.15 + 0.05
    assert answer[QUESTION].probability == pytest.approx(0.70 / total)
    assert answer[QUESTION].distribution["chat"] == pytest.approx(0.10 / total)
    assert abs(sum(answer[QUESTION].distribution.values()) - 1.0) < 1e-9
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


def test_openrouter_rejects_missing_noul_and_all_zero_scores() -> None:
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={"answers": {"q0__0": {"type": "noul", "noul": 0.4}}},
            )
        )
    )
    model = OpenRouterSpan01Lite(api_key="test-api-key", client=client)
    with pytest.raises(RiggingError, match="missing answer q0__1"):
        model.decide("state", {"pick": ["a", "b"]})

    zero = httpx.Client(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json=_noul_response({"q0__0": 0.0, "q0__1": 0.0}),
            )
        )
    )
    model._client = zero
    with pytest.raises(RiggingError, match="all-zero"):
        model.decide("state", {"pick": ["a", "b"]})
    client.close()
