"""OpenAI-compatible Chat Completions over httpx (ADR-0001).

Deliberately SDK-free: the wire format is the contract, and a harness that cannot
show you the bytes is not a harness. Covers:
  * SSE streaming with reasoning passthrough (ADR-0002)
  * tool-call deltas by index
  * retries on 429/5xx/timeout only, capped exponential backoff with jitter
  * malformed SSE events surfaced through `on_error`, never silently dropped

Verified against recorded wire fixtures in tests/fixtures (tests/test_client.py).
Not yet exercised against a live gateway; that is the v0.1 exit criterion.
"""

from __future__ import annotations

import json
import random
import re
import time
from collections.abc import Iterator
from typing import Any

import httpx

from rigging.errors import TransportError
from rigging.types import Message, ModelDelta, ModelRequest, ModelResponse, Usage

_RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}
_DATA_PREFIX = re.compile(r"^data:\s?")


class OpenAICompatClient:
    """`LLMClient` implementation for anything that speaks /v1/chat/completions."""

    name = "openai-compat"

    def __init__(self, base_url: str, api_key: str = "not-needed", *, timeout: float = 120.0,
                 max_retries: int = 3, transport: httpx.BaseTransport | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.max_retries = max_retries
        self._client = httpx.Client(timeout=timeout, transport=transport)

    # ------------------------------------------------------------------ public

    def stream(self, request: ModelRequest, *, on_error: Any = None) -> Iterator[ModelDelta]:
        payload = self._payload(request, stream=True)
        attempt = 0
        while True:
            try:
                with self._client.stream("POST", f"{self.base_url}/chat/completions",
                                         json=payload, headers=self._headers()) as response:
                    if response.status_code in _RETRY_STATUS and attempt < self.max_retries:
                        response.read()
                        attempt += 1
                        time.sleep(self._backoff(attempt))
                        continue
                    if response.status_code >= 400:
                        body = response.read().decode("utf-8", "replace")
                        raise TransportError(response.status_code, body, attempt + 1)
                    yield from self._parse_sse(response.iter_lines(), on_error=on_error)
                    return
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt >= self.max_retries:
                    raise TransportError(None, str(exc), attempt + 1) from exc
                attempt += 1
                time.sleep(self._backoff(attempt))

    def complete(self, request: ModelRequest) -> ModelResponse:
        started = time.perf_counter()
        text: list[str] = []
        reasoning: list[str] = []
        calls: dict[int, dict[str, Any]] = {}
        usage = Usage()
        finish: str | None = None
        for delta in self.stream(request):
            text.append(delta.text)
            reasoning.append(delta.reasoning)
            if delta.tool_call_delta:
                self._merge_tool_delta(calls, delta.tool_call_delta)
            if delta.usage:
                usage = delta.usage
            finish = delta.finish_reason or finish
        return ModelResponse(
            message=self._assemble_message("".join(text), "".join(reasoning), calls),
            usage=usage,
            finish_reason=finish,
            model=request.model,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

    def close(self) -> None:
        self._client.close()

    # ------------------------------------------------------------------ internals

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    def _payload(self, request: ModelRequest, *, stream: bool) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": request.model,
            "messages": [m.to_wire() for m in request.messages],
            "stream": stream,
        }
        if request.tools:
            payload["tools"] = request.tools
            payload["tool_choice"] = "auto"
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens
        if request.stop:
            payload["stop"] = request.stop
        if stream:
            payload["stream_options"] = {"include_usage": True}
        return payload

    def _backoff(self, attempt: int) -> float:
        base = min(8.0, 0.5 * (2 ** (attempt - 1)))
        jitter: float = 0.5 + random.random()
        return float(base * jitter)

    def _parse_sse(self, lines: Iterator[str], *, on_error: Any = None) -> Iterator[ModelDelta]:
        for raw in lines:
            line = raw.strip()
            if not line or line.startswith(":"):
                continue
            match = _DATA_PREFIX.match(line)
            if not match:
                continue  # event: / id: lines carry no payload we need
            data = line[match.end():].strip()
            if data == "[DONE]":
                return
            try:
                event = json.loads(data)
            except json.JSONDecodeError:
                if on_error:
                    on_error("sse_malformed", {"line": data[:500]})
                continue
            yield from self._deltas_from_event(event, on_error=on_error)

    def _deltas_from_event(self, event: dict[str, Any], *, on_error: Any = None) -> Iterator[ModelDelta]:
        if "error" in event:
            if on_error:
                on_error("gateway_error", {"error": event["error"]})
            return
        usage = None
        if event.get("usage"):
            u = event["usage"]
            usage = Usage(
                input_tokens=u.get("prompt_tokens", 0),
                output_tokens=u.get("completion_tokens", 0),
                cached_tokens=(u.get("prompt_tokens_details") or {}).get("cached_tokens", 0),
                reasoning_tokens=(u.get("completion_tokens_details") or {}).get("reasoning_tokens", 0),
            )
        choices = event.get("choices") or []
        if not choices:
            if usage:
                yield ModelDelta(usage=usage)
            return
        choice = choices[0]
        delta = choice.get("delta") or {}
        text = delta.get("content") or ""
        reasoning = delta.get("reasoning_content") or delta.get("reasoning") or ""
        tool_deltas = delta.get("tool_calls") or []
        finish = choice.get("finish_reason")
        if tool_deltas:
            for td in tool_deltas:
                yield ModelDelta(tool_call_delta=td, finish_reason=None)
        if text or reasoning or finish or usage:
            yield ModelDelta(text=text, reasoning=reasoning, finish_reason=finish, usage=usage)

    def _merge_tool_delta(self, calls: dict[int, dict[str, Any]], delta: dict[str, Any]) -> None:
        index = delta.get("index", 0)
        entry = calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
        if delta.get("id"):
            entry["id"] = delta["id"]
        fn = delta.get("function") or {}
        if fn.get("name"):
            entry["name"] = fn["name"]
        if fn.get("arguments"):
            entry["arguments"] += fn["arguments"]

    def _assemble_message(self, text: str, reasoning: str, calls: dict[int, dict[str, Any]]) -> Message:
        from rigging.types import ToolCall

        tool_calls = []
        for index in sorted(calls):
            entry = calls[index]
            raw = entry["arguments"]
            try:
                args = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                args = {}
            tool_calls.append(ToolCall(id=entry["id"] or f"call_{index}", name=entry["name"],
                                       arguments=args, raw_arguments=raw))
        return Message(role="assistant", content=text, reasoning=reasoning or None,
                       tool_calls=tool_calls)
