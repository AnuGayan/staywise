"""Spans for each action the agent takes, on top of what `amp-instrument` records automatically.

Traceloop (behind `amp-instrument`) traces the native Ollama client and each MCP `tools/call`. It does
not see model calls made through an OpenAI-format gateway endpoint (plain httpx), token requests to
ThunderID, the MCP handshake, or requests the agent turns away, so those spans are created here.

Attribute names follow the OpenTelemetry GenAI conventions plus the Traceloop ones Agent Manager's trace
view reads. Prompts, replies, tool arguments and results are recorded only while content tracing is on
(`TRACELOOP_TRACE_CONTENT`, which `amp-instrument` sets from `AMP_TRACE_CONTENT`). Tokens never are.
"""

from __future__ import annotations

import json
import os
from typing import Any

from opentelemetry import trace
from opentelemetry.trace import Span, Status, StatusCode

AGENT_NAME = "melody"
TRACE_CONTENT_ENV = "TRACELOOP_TRACE_CONTENT"

tracer = trace.get_tracer("concierge")


def content_enabled() -> bool:
    """Whether prompts, replies and tool payloads may go into spans (Traceloop's switch, default on)."""
    return os.environ.get(TRACE_CONTENT_ENV, "true").strip().lower() == "true"


def set_content(span: Span, key: str, value: Any) -> None:
    """Record `value` (as text, JSON for non-strings) under `key`, only while content tracing is on."""
    if value is None or not content_enabled():
        return
    span.set_attribute(key, value if isinstance(value, str) else json.dumps(value, default=str))


def mark_error(span: Span, description: str, error_type: str) -> None:
    """Flag a span as failed for an error the code handles rather than raises (e.g. a refused tool call)."""
    span.set_attribute("error.type", error_type)
    span.set_status(Status(StatusCode.ERROR, description))
