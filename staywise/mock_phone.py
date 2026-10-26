"""A pretend phone for the demo: Thunder's custom SMS sender posts one-time codes here and the UI shows them."""

from __future__ import annotations

import json
import re
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qsl

MAX_MESSAGES = 50
MAX_TEXT_CHARS = 1000
_OTP = re.compile(r"(?<!\d)(\d{4,8})(?!\d)")
_TEXT_KEYS = ("body", "message", "text", "content", "msg")


@dataclass(frozen=True)
class PhoneMessage:
    """One received SMS."""

    id: str
    receivedAt: str
    text: str
    otp: str | None

    def to_json(self) -> dict[str, Any]:
        """The UI's wire shape."""
        return asdict(self)


def extract_text(body: bytes, content_type: str) -> str:
    """Pull the human-readable message out of whatever the SMS sender posted (JSON, form or plain text)."""
    raw = body.decode("utf-8", errors="replace")
    if "json" in content_type or raw.lstrip().startswith(("{", "[")):
        try:
            return _text_from_json(json.loads(raw))[:MAX_TEXT_CHARS]
        except ValueError:
            pass
    if "x-www-form-urlencoded" in content_type:
        fields = dict(parse_qsl(raw))
        for key in _TEXT_KEYS:
            if fields.get(key):
                return fields[key][:MAX_TEXT_CHARS]
    return raw.strip()[:MAX_TEXT_CHARS]


def _text_from_json(value: Any) -> str:
    if isinstance(value, dict):
        for key in _TEXT_KEYS:
            if isinstance(value.get(key), str):
                return value[key]
        strings = [_text_from_json(item) for item in value.values()]
    elif isinstance(value, list):
        strings = [_text_from_json(item) for item in value]
    else:
        return str(value) if isinstance(value, str) else ""
    # Prefer the string that carries a code, else the longest one.
    with_code = [text for text in strings if _OTP.search(text)]
    return max(with_code or strings or [""], key=len)


class PhoneInbox:
    """Keeps the last messages and tells a listener about each new one."""

    def __init__(self, on_message: Callable[[PhoneMessage], None] | None = None) -> None:
        self._messages: deque[PhoneMessage] = deque(maxlen=MAX_MESSAGES)
        self._on_message = on_message

    def set_listener(self, on_message: Callable[[PhoneMessage], None]) -> None:
        """Replace the new-message listener."""
        self._on_message = on_message

    def receive(self, body: bytes, content_type: str = "") -> PhoneMessage:
        """Store an incoming SMS and notify the listener."""
        text = extract_text(body, content_type.lower())
        match = _OTP.search(text)
        message = PhoneMessage(
            id=uuid.uuid4().hex[:10],
            receivedAt=datetime.now(UTC).isoformat(timespec="seconds"),
            text=text,
            otp=match.group(1) if match else None,
        )
        self._messages.append(message)
        if self._on_message is not None:
            self._on_message(message)
        return message

    def messages(self) -> list[PhoneMessage]:
        """Oldest first."""
        return list(self._messages)
