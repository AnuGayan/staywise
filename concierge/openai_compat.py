"""Chat client for OpenAI-format `/chat/completions` endpoints, e.g. Ollama behind a WSO2 AI gateway.

It satisfies the same `ChatClient` protocol as `ollama.AsyncClient`, so the agent loop is
unchanged: requests are translated to the OpenAI wire format and responses back to
`ollama.ChatResponse`.
"""

from __future__ import annotations

import json
from collections import deque
from collections.abc import Mapping, Sequence
from typing import Any

import httpx
from ollama import ChatResponse, Message, ResponseError
from opentelemetry.trace import Span, SpanKind

from concierge.telemetry import content_enabled, mark_error, tracer

DEFAULT_TIMEOUT_SECONDS = 300.0  # local models can be slow to produce a first token
DEFAULT_API_KEY_HEADER = "Authorization"
# gen_ai.system on model-call spans: the wire format, since a gateway may front any provider.
GEN_AI_SYSTEM = "openai"


class OpenAIChatClient:
    """Async client for `{base_url}/chat/completions`. Close it with `aclose()`."""

    def __init__(
        self,
        base_url: str,
        *,
        api_key: str | None = None,
        api_key_header: str = DEFAULT_API_KEY_HEADER,
        reasoning_effort: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        # Sent with every request when set. Reasoning models such as OpenAI's GPT-5 family refuse
        # tools on /chat/completions unless it is "none".
        self._reasoning_effort = reasoning_effort
        headers = {api_key_header: _auth_value(api_key_header, api_key)} if api_key else {}
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), headers=headers, timeout=timeout, transport=transport
        )

    async def aclose(self) -> None:
        """Release the underlying HTTP connection pool."""
        await self._http.aclose()

    async def chat(
        self,
        model: str = "",
        messages: Sequence[Mapping[str, Any] | Message] | None = None,
        *,
        tools: Sequence[Mapping[str, Any]] | None = None,
    ) -> ChatResponse:
        """Send one chat turn and return the model's reply.

        Raises:
            ResponseError: if the endpoint answers with an HTTP error or an unusable body.
            ConnectionError: if the endpoint cannot be reached.
        """
        payload: dict[str, Any] = {"model": model, "messages": to_openai_messages(messages or [])}
        if tools:
            payload["tools"] = list(tools)
        if self._reasoning_effort:
            payload["reasoning_effort"] = self._reasoning_effort
        # Traceloop only instruments provider SDKs, so a call over plain httpx gets its span here, laid
        # out like the spans of Traceloop's Ollama instrumentor.
        with tracer.start_as_current_span(
            f"chat {model}", kind=SpanKind.CLIENT, attributes=_request_attributes(payload)
        ) as span:
            if content_enabled():
                _set_prompt_attributes(span, payload)
            try:
                response = await self._http.post("/chat/completions", json=payload)
            except httpx.TransportError as exc:
                raise ConnectionError(f"{self._http.base_url}: {exc}") from exc
            span.set_attribute("http.response.status_code", response.status_code)
            if response.is_error:
                mark_error(span, f"HTTP {response.status_code}", str(response.status_code))
                raise ResponseError(response.text or response.reason_phrase, response.status_code)
            try:
                body = response.json()
                reply = from_openai_response(body)
            except (ValueError, KeyError, IndexError, TypeError) as exc:
                raise ResponseError(f"unexpected response from {response.url}: {exc}", response.status_code) from exc
            _set_response_attributes(span, body, reply)
            return reply


def _request_attributes(payload: Mapping[str, Any]) -> dict[str, Any]:
    attributes: dict[str, Any] = {
        "gen_ai.operation.name": "chat",
        "llm.request.type": "chat",
        "gen_ai.system": GEN_AI_SYSTEM,
        "gen_ai.request.model": payload["model"],
        "llm.is_streaming": False,
    }
    if "reasoning_effort" in payload:
        attributes["gen_ai.request.reasoning_effort"] = payload["reasoning_effort"]
    return attributes


def _set_prompt_attributes(span: Span, payload: Mapping[str, Any]) -> None:
    """The conversation and tool schemas sent, as `gen_ai.prompt.N.*` and `llm.request.functions.N.*`."""
    for index, message in enumerate(payload["messages"]):
        prefix = f"gen_ai.prompt.{index}"
        span.set_attribute(f"{prefix}.role", message["role"])
        span.set_attribute(f"{prefix}.content", message.get("content") or "")
        if "tool_call_id" in message:
            span.set_attribute(f"{prefix}.tool_call_id", message["tool_call_id"])
        _set_tool_call_attributes(span, prefix, message.get("tool_calls") or [])
    for index, tool in enumerate(payload.get("tools", [])):
        function = tool.get("function", {})
        prefix = f"llm.request.functions.{index}"
        span.set_attribute(f"{prefix}.name", function.get("name", ""))
        span.set_attribute(f"{prefix}.description", function.get("description", ""))
        span.set_attribute(f"{prefix}.parameters", json.dumps(function.get("parameters", {})))


def _set_response_attributes(span: Span, body: Mapping[str, Any], reply: ChatResponse) -> None:
    """Response model, finish reason and token usage; the reply itself only while content tracing is on."""
    span.set_attribute("gen_ai.response.model", reply.model or "")
    if body.get("id"):
        span.set_attribute("gen_ai.response.id", str(body["id"]))
    if reply.done_reason:
        span.set_attribute("gen_ai.response.finish_reasons", [reply.done_reason])
    usage = body.get("usage") or {}
    if isinstance(usage.get("prompt_tokens"), int) and isinstance(usage.get("completion_tokens"), int):
        span.set_attribute("gen_ai.usage.input_tokens", usage["prompt_tokens"])
        span.set_attribute("gen_ai.usage.output_tokens", usage["completion_tokens"])
        span.set_attribute("llm.usage.total_tokens", usage["prompt_tokens"] + usage["completion_tokens"])
    if not content_enabled():
        return
    span.set_attribute("gen_ai.completion.0.role", "assistant")
    span.set_attribute("gen_ai.completion.0.content", reply.message.content or "")
    if reply.done_reason:
        span.set_attribute("gen_ai.completion.0.finish_reason", reply.done_reason)
    calls = body["choices"][0]["message"].get("tool_calls") or []
    _set_tool_call_attributes(span, "gen_ai.completion.0", calls)


def _set_tool_call_attributes(span: Span, prefix: str, calls: Sequence[Mapping[str, Any]]) -> None:
    for index, call in enumerate(calls):
        function = call.get("function", {})
        span.set_attribute(f"{prefix}.tool_calls.{index}.name", function.get("name", ""))
        arguments = function.get("arguments", "")
        span.set_attribute(
            f"{prefix}.tool_calls.{index}.arguments", arguments if isinstance(arguments, str) else json.dumps(arguments)
        )


def _auth_value(header: str, api_key: str) -> str:
    """`Authorization` carries a bearer token; any other header (e.g. X-API-Key) the raw key."""
    return f"Bearer {api_key}" if header.lower() == "authorization" else api_key


def to_openai_messages(messages: Sequence[Mapping[str, Any] | Message]) -> list[dict[str, Any]]:
    """Convert Ollama-style history to OpenAI format.

    Ollama tool calls carry no ids, but OpenAI requires each tool result to name the call it
    answers. The agent appends results in the same order as the calls, so ids are assigned
    per call and handed out to the following tool messages in order.
    """
    converted: list[dict[str, Any]] = []
    pending_ids: deque[str] = deque()
    for index, raw in enumerate(messages):
        message = raw if isinstance(raw, Message) else Message.model_validate(dict(raw))
        if message.role == "tool":
            if not pending_ids:
                raise ValueError(f"tool message at position {index} has no matching tool call")
            converted.append({"role": "tool", "tool_call_id": pending_ids.popleft(), "content": message.content or ""})
            continue
        entry: dict[str, Any] = {"role": message.role, "content": message.content or ""}
        if message.tool_calls:
            entry["tool_calls"] = []
            for position, call in enumerate(message.tool_calls):
                call_id = f"call_{index}_{position}"
                pending_ids.append(call_id)
                entry["tool_calls"].append(
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": call.function.name,
                            "arguments": json.dumps(dict(call.function.arguments)),
                        },
                    }
                )
        converted.append(entry)
    return converted


def from_openai_response(body: Mapping[str, Any]) -> ChatResponse:
    """Convert an OpenAI chat-completions response body to an `ollama.ChatResponse`.

    Raises:
        ValueError: if a tool call's arguments are not a JSON object.
        KeyError, IndexError: if the body has no choices/message.
    """
    choice = body["choices"][0]
    message = choice["message"]
    tool_calls = [
        Message.ToolCall(
            function=Message.ToolCall.Function(
                name=call["function"]["name"], arguments=_parse_arguments(call["function"].get("arguments"))
            )
        )
        for call in message.get("tool_calls") or []
    ]
    usage = body.get("usage") or {}
    return ChatResponse(
        model=body.get("model", ""),
        done=True,
        done_reason=choice.get("finish_reason"),
        prompt_eval_count=usage.get("prompt_tokens"),
        eval_count=usage.get("completion_tokens"),
        message=Message(role="assistant", content=message.get("content") or "", tool_calls=tool_calls or None),
    )


def _parse_arguments(raw: str | Mapping[str, Any] | None) -> dict[str, Any]:
    if raw is None or raw == "":
        return {}
    if isinstance(raw, Mapping):
        return dict(raw)
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError(f"tool arguments must be a JSON object, got {raw!r}")
    return parsed
