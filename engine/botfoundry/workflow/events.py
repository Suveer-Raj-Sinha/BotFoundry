"""Structured run events. The event log is the authoritative, ordered record of what happened."""
import logging
import threading
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)


class EventType(str, Enum):
    RUN_CREATED = "run_created"
    VALIDATION_STARTED = "validation_started"
    VALIDATION_SUCCEEDED = "validation_succeeded"
    VALIDATION_FAILED = "validation_failed"
    NODE_STARTED = "node_started"
    NODE_PROGRESS = "node_progress"
    NODE_COMPLETED = "node_completed"
    NODE_FAILED = "node_failed"
    NODE_SKIPPED = "node_skipped"
    APPROVAL_REQUESTED = "approval_requested"  # used from Phase 2 (Rulebook / Approval Gate)
    APPROVAL_GRANTED = "approval_granted"
    APPROVAL_DENIED = "approval_denied"
    RUN_CANCELLED = "run_cancelled"
    RUN_COMPLETED = "run_completed"
    RUN_FAILED = "run_failed"


TERMINAL_EVENTS = frozenset({EventType.RUN_CANCELLED, EventType.RUN_COMPLETED, EventType.RUN_FAILED})


class RunEvent(BaseModel):
    id: int  # 1, 2, 3 ... within one run; stable and strictly increasing
    run_id: str
    type: EventType
    timestamp: datetime
    node_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class EventLog:
    """Append-only, thread-safe list of events for one run, with optional live listeners."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self._events: list[RunEvent] = []
        self._lock = threading.Lock()
        self._listeners: list[Callable[[RunEvent], None]] = []

    def append(self, event_type: EventType, node_id: str | None = None,
               payload: dict[str, Any] | None = None) -> RunEvent:
        with self._lock:
            event = RunEvent(id=len(self._events) + 1, run_id=self.run_id, type=event_type,
                             timestamp=datetime.now(timezone.utc), node_id=node_id, payload=payload or {})
            self._events.append(event)
            listeners = list(self._listeners)
        for listener in listeners:  # outside the lock; a broken listener must never break a run
            try:
                listener(event)
            except Exception:
                log.exception("Event listener failed for event %s", event.id)
        return event

    def since(self, after_id: int = 0) -> list[RunEvent]:
        """Events with id greater than `after_id` (use for polling with a cursor)."""
        with self._lock:
            return self._events[after_id:]

    def all(self) -> list[RunEvent]:
        return self.since(0)

    def subscribe(self, listener: Callable[[RunEvent], None]) -> Callable[[], None]:
        with self._lock:
            self._listeners.append(listener)

        def unsubscribe() -> None:
            with self._lock:
                if listener in self._listeners:
                    self._listeners.remove(listener)

        return unsubscribe
