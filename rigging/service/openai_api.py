"""An OpenAI-compatible `/v1` surface for chat UIs such as LibreChat (ADR-0028).

LibreChat, OpenWebUI and AnythingLLM speak OpenAI's chat-completions protocol to
a server. This module is that server for exactly one agent: the request's
`messages` array is the conversation, and `run_agent` is still the only thing that
runs the agent — no second loop, no shortcut around the guardrails.

Three consequences follow from the client having no approval channel, and each is
enforced rather than described:

* The manifest's `cli` policy is replaced with `AutoDenyApproval`: its prompt
  would block a request thread on a terminal nobody can see. `auto_deny` and
  `scripted` are deliberate unattended choices and are left alone (the same rule
  as the page, ADR-0023). Either way, a side-effecting tool cannot run unapproved
  (hard rule 4).
* The session is keyed by the client's `X-Conversation-Id` header. Without it the
  request is refused: a fresh session per request would quietly lose the sentinel
  map, and a shared one would resolve `[EMAIL_1]` across two conversations
  (ADR-0025, ADR-0027).
* Only the assistant's text (or the denial reason when there is no text) is
  returned. Tool calls stay in the event log, which is where a reviewer reads
  them.

Streaming is live only when the manifest has no `on_output` guardrail. With one,
the answer is buffered until the policy has run: a token stream that shows a
commitment the guardrail then denies is a second, unguarded answer path.
"""

from __future__ import annotations

import hashlib
import json
import queue
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from rigging.approvals import AutoDenyApproval
from rigging.errors import RiggingError
from rigging.events import EventLog
from rigging.loop import run_agent
from rigging.manifest import AgentSpec
from rigging.redact import redact_text
from rigging.types import Message, ModelDelta, RunResult

#: Longest a single user message may be. The manifest's own limits still apply to
#: the run; this only stops a client from posting a megabyte into a request body.
MAX_MESSAGE_CHARS = 32_000
#: Bounds on the whole conversation the client replays into one request. A chat UI
#: sends its history every turn, so without a cap a long conversation becomes a
#: large model call that the manifest's token budget only sees after the fact.
MAX_MESSAGES = 200
MAX_CONVERSATION_CHARS = 200_000

#: A LibreChat conversation id is the client's key, not ours: hash it so a header
#: can never collide with a harness session id or travel into the log verbatim.
SESSION_PREFIX = "lc_"


class OpenAIMessage(BaseModel):
    """One message from the client. Extra keys are accepted and ignored."""

    model_config = ConfigDict(extra="allow")

    role: str
    #: A plain string, or the OpenAI multi-part form (`[{"type": "text", ...}]`).
    #: The multi-part form is accepted so a text-only client is not rejected for
    #: a shape LibreChat started sending for attachments; images are refused.
    content: str | list[dict[str, Any]] | None = ""
    name: str | None = None


class ChatCompletionRequest(BaseModel):
    """The parts of the OpenAI request this server reads.

    `extra="allow"` on purpose: clients send `user`, `conversationId`, `top_p` and
    a dozen other fields, and rejecting an unknown one would break the UI on its
    next release rather than here.
    """

    model_config = ConfigDict(extra="allow")

    model: str = ""
    messages: list[OpenAIMessage] = Field(default_factory=list)
    stream: bool = False
    stream_options: dict[str, Any] | None = None


@dataclass(frozen=True)
class PreparedTurn:
    """A validated request: what to run, and in which session."""

    prompt: str
    history: list[Message]
    session_id: str


#: What a client is told. The exception's own text is for the server and tests:
#: returning it lets a gateway message or a path leak through the HTTP response
#: (CodeQL's "information exposure through an exception"). The handler looks the
#: public message up by code, so no exception string is ever the response body.
PUBLIC_MESSAGES = {
    "invalid_request": "invalid request",
    "model_not_found": "model not found; GET /v1/models lists the one model this "
                       "endpoint serves",
    "missing_conversation_id": "missing X-Conversation-Id; set it to "
                               "'{{LIBRECHAT_BODY_CONVERSATIONID}}' in librechat.yaml",
    "no_user_message": "no user message in `messages`",
    "conversation_too_large": "this endpoint accepts at most 200 messages / 200000 "
                             "characters per conversation",
    "message_too_long": "this endpoint accepts at most 32000 characters per user message",
    "unsupported_content": "this endpoint accepts text messages only",
    "run_failed": "run failed; see the event log for this run's error",
}


class RequestError(RiggingError):
    """A 400 with a fixed public code; the exception text stays server-side."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code


class ModelNotFound(RequestError):
    """The client asked for a model this server does not serve."""

    def __init__(self, detail: str) -> None:
        super().__init__("model_not_found", detail)


class TurnFailed(RiggingError):
    """The run ended in `error`; there is no assistant text to return."""


class OpenAIBridge:
    """Turns one OpenAI request into one `run_agent` turn.

    `client_factory` and `registry` exist for tests and for `harness serve` to
    hand in a prebuilt gateway, the same way `ChatRunner` takes them.
    """

    def __init__(self, spec: AgentSpec, db: str, *,
                 client_factory: Callable[[], Any] | None = None,
                 registry: Any = None) -> None:
        self.spec = spec
        self.db = db
        #: What `/v1/models` advertises. The agent's name, not its model id: the
        #: manifest can switch models without the UI reconfiguring.
        self.model_id = spec.name
        self._client_factory = client_factory or (lambda: None)
        self._registry = registry

    # ------------------------------------------------------------------ request

    def session_id(self, conversation_id: str | None) -> str:
        """Map the client's conversation to a harness session id, or refuse.

        A stable hash per conversation is what makes turn 2 resolve the `[EMAIL_1]`
        that turn 1's answer left in the client's history. Both alternatives are
        worse: a new session loses the sentinel map, and one shared session lets a
        placeholder from one conversation resolve inside another.
        """
        if not conversation_id or not conversation_id.strip():
            raise RequestError(
                "missing_conversation_id",
                "missing X-Conversation-Id: this server keys a session (and its PII "
                "sentinel map) by conversation, so the header is required. In "
                "librechat.yaml set `X-Conversation-Id: "
                "'{{LIBRECHAT_BODY_CONVERSATIONID}}'` on the custom endpoint.",
            )
        digest = hashlib.sha256(conversation_id.strip().encode()).hexdigest()[:16]
        return f"{SESSION_PREFIX}{digest}"

    def check_model(self, requested: str) -> None:
        if requested and requested != self.model_id:
            raise ModelNotFound(
                f"model {requested!r} is not served here; this build serves "
                f"{self.model_id!r} (/v1/models)")

    def prepare(self, body: ChatCompletionRequest,
                conversation_id: str | None) -> PreparedTurn:
        """Validate the request and resolve the session, before any response starts.

        Separated from `stream()` because a generator's body does not run until it
        is iterated: a validation error inside it would arrive as a broken
        connection instead of a 400 with an OpenAI error body.
        """
        self.check_model(body.model)
        prompt, history = _split(body)
        return PreparedTurn(prompt, history, self.session_id(conversation_id))

    def _clean_history(self, session_id: str, history: list[Message]) -> list[Message]:
        """Keep the client's raw copy of earlier turns out of the model's context.

        The client stores what the user typed, not what the harness sent to the
        model. Replaying that raw text would undo the previous turn's
        pseudonymisation (ADR-0025), so history passes the same session sentinel
        map when the manifest declares `pii_pseudonymise`; otherwise it gets the
        one-way `redact_text` floor. A sentinel survives and the vault can resolve
        it; `[REDACTED]` cannot, and the customer has to give the value again.
        """
        if any(ref.name == "pii_pseudonymise" for ref in self.spec.guardrails.on_input):
            from rigging.guardrails.pii import DEFAULT_SENTINEL_STORE

            sentinels = DEFAULT_SENTINEL_STORE.get(session_id)
            return [
                message.model_copy(update={"content": sentinels.sentinelise(message.content)})
                for message in history
            ]
        return [
            message.model_copy(update={"content": redact_text(message.content)})
            for message in history
        ]

    # ------------------------------------------------------------------- answer

    def complete(self, turn: PreparedTurn, body: ChatCompletionRequest) -> dict[str, Any]:
        """One turn, as a `chat.completion` object."""
        result = self._turn(turn.prompt, turn.history, turn.session_id)
        content = _content(result)
        return {
            "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": self.model_id,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": _finish_reason(result),
            }],
            "usage": _usage(result),
            # Not OpenAI's field, and harmless: the run is the receipt.
            "rigging": {"run_id": result.run_id, "session_id": turn.session_id,
                        "status": result.status},
        }

    def stream(self, turn: PreparedTurn,
               body: ChatCompletionRequest) -> Iterator[str]:
        """One turn, as `chat.completion.chunk` SSE frames.

        With no `on_output` guardrail the model's deltas are forwarded as they
        arrive: that is what streaming is for. With one, the answer is buffered
        until the guardrail has run, because streaming a sentence that the
        `commitment_check` is about to deny (or text another `on_output` guardrail
        is about to rewrite) would show the user text the policy had not approved.
        The trade is deliberate: a guardrailed manifest gets a late first token,
        not a second, unguarded answer path.
        """
        include_usage = bool(body.stream_options
                             and body.stream_options.get("include_usage"))
        if self.spec.guardrails.on_output:
            yield from self._buffered_stream(turn, include_usage)
            return

        events: queue.Queue[tuple[str, Any]] = queue.Queue()

        def emit(delta: ModelDelta) -> None:
            if delta.text:
                events.put(("text", delta.text))

        def work() -> None:
            try:
                events.put(("done", self._turn(turn.prompt, turn.history,
                                               turn.session_id, emit)))
            except Exception as exc:  # handed to the stream, not raised here
                events.put(("error", exc))

        threading.Thread(target=work, name="rigging-openai-stream", daemon=True).start()

        completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        created = int(time.time())
        yield _chunk(completion_id, created, self.model_id,
                     {"role": "assistant", "content": ""}, None)
        streamed: list[str] = []
        while True:
            kind, value = events.get()
            if kind == "text":
                streamed.append(value)
                yield _chunk(completion_id, created, self.model_id,
                             {"content": value}, None)
                continue
            if kind == "error":
                # A frame, not a broken connection: the UI shows the reason and
                # the client is not left waiting for a `[DONE]` that never comes.
                yield _error_frame(value)
                yield "data: [DONE]\n\n"
                return
            result: RunResult = value
            seen = "".join(streamed)
            if not seen:
                # A denial, or a budget-exhausted run whose final answer was a
                # non-streamed call: there are no deltas, but there is a reason.
                for piece in _pieces(_content(result)):
                    yield _chunk(completion_id, created, self.model_id,
                                 {"content": piece}, None)
            elif result.output and result.output != seen:
                # The budget-exhausted final answer is a second, non-streamed model
                # call. It is the authoritative output, so deliver it even though
                # the narration before it already went out.
                for piece in _pieces(result.output):
                    yield _chunk(completion_id, created, self.model_id,
                                 {"content": piece}, None)
            yield _final_chunk(completion_id, created, self.model_id, result)
            if include_usage:
                yield _usage_chunk(completion_id, created, self.model_id, result)
            yield "data: [DONE]\n\n"
            return

    def _buffered_stream(self, turn: PreparedTurn, include_usage: bool) -> Iterator[str]:
        """Run first, then stream the answer the guardrails approved."""
        completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        created = int(time.time())
        yield _chunk(completion_id, created, self.model_id,
                     {"role": "assistant", "content": ""}, None)
        try:
            result = self._turn(turn.prompt, turn.history, turn.session_id)
        except Exception as exc:  # a frame, not a broken connection
            yield _error_frame(exc)
            yield "data: [DONE]\n\n"
            return
        for piece in _pieces(_content(result)):
            yield _chunk(completion_id, created, self.model_id,
                         {"content": piece}, None)
        yield _final_chunk(completion_id, created, self.model_id, result)
        if include_usage:
            yield _usage_chunk(completion_id, created, self.model_id, result)
        yield "data: [DONE]\n\n"

    # --------------------------------------------------------------------- turn

    def _turn(self, prompt: str, history: list[Message], session_id: str,
              stream: Callable[[ModelDelta], None] | None = None) -> RunResult:
        log = EventLog(self.db)
        try:
            sid = log.start_session(session_id, title=self.spec.name,
                                    spec_hash=self.spec.spec_hash)
            history = self._clean_history(sid, history)
            # Name the run before `run_agent` does, so a crash in assembly or a
            # guardrail cannot leave a row saying "running" forever. The insert is
            # idempotent: `run_agent` confirms this id rather than opening another.
            run_id = log.start_run(session_id=sid, spec_hash=self.spec.spec_hash,
                                   model=self.spec.model)
            try:
                result = run_agent(
                    self.spec, prompt, log=log, session_id=sid, history=history,
                    # The manifest's `cli` policy would block this request thread on
                    # a terminal prompt nobody can see. `auto_deny` and `scripted`
                    # are deliberate unattended choices, so they are left alone
                    # (the same rule as the page, ADR-0023).
                    approval=(AutoDenyApproval()
                              if self.spec.approval.policy == "cli" else None),
                    client=self._client_factory(), registry=self._registry,
                    stream=stream, run_id=run_id,
                )
            except Exception as exc:
                log.append(run_id, "error", {"error": f"{type(exc).__name__}: {exc}"})
                log.finish_run(run_id, "error")
                raise
        finally:
            log.close()
        if result.status == "error":
            raise TurnFailed(f"run {result.run_id} ended in error: {result.error}")
        return result


# --------------------------------------------------------------------- helpers


def _text(message: OpenAIMessage) -> str:
    """The text of one client message, from either OpenAI content shape.

    Text parts are joined; an image or audio part is refused rather than silently
    dropped. The harness is a text agent, and a client that attached a PDF should
    learn that here, not wonder why the model ignored it.
    """
    content = message.content
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind not in (None, "text", "input_text"):
            raise RequestError(
                "unsupported_content",
                f"this endpoint accepts text messages only; got a {kind!r} part")
        text = part.get("text")
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts)


def _split(body: ChatCompletionRequest) -> tuple[str, list[Message]]:
    """The last user message, and everything before it as history.

    System and tool messages are dropped: the manifest owns the system prompt
    (ADR-0008 keeps that out of the client's hands), and this server never emits a
    tool call to the client, so a client-sent tool message has no `call_id` to
    attach to. Refusing silently would be worse than ignoring — the request still
    answers, and a client that needs its own system prompt has the manifest.
    """
    if len(body.messages) > MAX_MESSAGES:
        raise RequestError(
            "conversation_too_large",
            f"conversation has {len(body.messages)} messages; this endpoint accepts "
            f"at most {MAX_MESSAGES}")
    texts = [_text(message) for message in body.messages]
    total = sum(len(text) for text in texts)
    if total > MAX_CONVERSATION_CHARS:
        raise RequestError(
            "conversation_too_large",
            f"conversation is {total} characters; this endpoint accepts at most "
            f"{MAX_CONVERSATION_CHARS}")
    for message, text in zip(body.messages, texts, strict=True):
        if message.role == "user" and len(text) > MAX_MESSAGE_CHARS:
            raise RequestError(
                "message_too_long",
                f"message of {len(text)} characters exceeds "
                f"{MAX_MESSAGE_CHARS}; raise limits on the manifest, not the request")

    # Find the last user message by index, not by equality: two identical prompts
    # ("yes", "yes") must not make `.index()` return the first one and drop the
    # assistant turn between them.
    last = -1
    for index, message in enumerate(body.messages):
        if message.role == "user" and texts[index].strip():
            last = index
    if last < 0:
        raise RequestError("no_user_message", "no user message in `messages`")
    prompt = texts[last].strip()

    history: list[Message] = []
    for message, text in zip(body.messages[:last], texts[:last], strict=True):
        if not text.strip():
            continue
        if message.role == "user":
            history.append(Message(role="user", content=text))
        elif message.role == "assistant":
            history.append(Message(role="assistant", content=text))
    return prompt, history


def _content(result: RunResult) -> str:
    text = result.output or ""
    if not text and result.error:
        # A denial has no answer and a reason worth showing: the alternative is an
        # empty bubble that makes the agent look broken instead of policy-bound.
        return f"[{result.status}] {result.error}"
    return text


def _usage(result: RunResult) -> dict[str, Any]:
    return {
        "prompt_tokens": result.usage.input_tokens,
        "completion_tokens": result.usage.output_tokens,
        "total_tokens": result.usage.input_tokens + result.usage.output_tokens,
    }


def _finish_reason(result: RunResult) -> str:
    """OpenAI's word for how the answer ended, not the harness's status."""
    if result.status == "budget_exhausted":
        return "length"
    if result.status == "denied":
        return "content_filter"
    return "stop"


def _pieces(text: str, size: int = 240) -> Iterator[str]:
    """Split a buffered answer into chunk-sized pieces, without re-encoding it."""
    for start in range(0, len(text), size):
        yield text[start:start + size]


def _chunk(completion_id: str, created: int, model: str, delta: dict[str, Any],
           finish_reason: str | None) -> str:
    body: dict[str, Any] = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    return f"data: {json.dumps(body)}\n\n"


def _final_chunk(completion_id: str, created: int, model: str,
                 result: RunResult) -> str:
    return _chunk(completion_id, created, model, {}, _finish_reason(result))


def _usage_chunk(completion_id: str, created: int, model: str,
                 result: RunResult) -> str:
    """The optional totals frame: empty `choices`, as OpenAI sends it.

    Kept separate from the finish frame so a client that did not ask for usage
    still receives a normal `finish_reason` chunk.
    """
    body = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [],
        "usage": _usage(result),
    }
    return f"data: {json.dumps(body)}\n\n"


def _error_frame(exc: Exception) -> str:
    body = {"error": {"message": str(exc), "type": type(exc).__name__, "code": 500}}
    return f"data: {json.dumps(body)}\n\n"
