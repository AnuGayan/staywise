"""Events streamed to the web UI (Server-Sent Events), and the per-call context they are tagged with."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

MAX_QUEUED_EVENTS = 500
# Kept for SSE reconnects (Last-Event-ID): Agent Manager's route ends every request after 30 s.
REPLAY_EVENTS = 200

EventSink = Callable[[str, dict[str, Any]], None]


@dataclass(frozen=True)
class Event:
    """One SSE event: `type` becomes the SSE event name, `data` its JSON payload, `id` the SSE id."""

    type: str
    data: dict[str, Any]
    id: int = 0


class EventBus:
    """Fans events out to every open SSE stream of one chat session."""

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[Event]] = set()
        self._recent: deque[Event] = deque(maxlen=REPLAY_EVENTS)
        self._next_id = 1

    def subscribe(self, after: int | None = None) -> asyncio.Queue[Event]:
        """A new queue that receives every event published from now on.

        `after` is the last event id a reconnecting client saw; the buffered events since then are
        queued first, so nothing published while it was disconnected is lost.
        """
        queue: asyncio.Queue[Event] = asyncio.Queue(MAX_QUEUED_EVENTS)
        if after is not None:
            for event in self._recent:
                if event.id > after:
                    queue.put_nowait(event)  # at most REPLAY_EVENTS, which is below the queue size
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[Event]) -> None:
        """Stop delivering to `queue`."""
        self._subscribers.discard(queue)

    def publish(self, type_: str, data: dict[str, Any]) -> None:
        """Deliver an event, stamped with the current time, to all subscribers. A full queue drops it."""
        event = Event(type_, {**data, "ts": datetime.now(UTC).isoformat(timespec="milliseconds")}, self._next_id)
        self._next_id += 1
        self._recent.append(event)
        for queue in self._subscribers:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning("dropping %s event for a slow SSE client", type_)


@dataclass(frozen=True)
class CallInfo:
    """The tool call currently in flight, so lower layers can tag their events with it."""

    turn_id: str
    call_id: str
    tool: str
    server: str


class CallContext:
    """Holds the in-flight call. The agent runs one tool call at a time per session, so one slot is enough;
    a contextvar would not work because the MCP client sends requests from its own task."""

    def __init__(self) -> None:
        self.current: CallInfo | None = None
