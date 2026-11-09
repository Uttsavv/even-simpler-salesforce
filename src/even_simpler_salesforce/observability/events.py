"""Events and the observer protocol (§11.2, §11.4)."""

from __future__ import annotations

import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable

__all__ = ["Event", "Observer", "ObserverHub"]


@dataclass(frozen=True, slots=True)
class Event:
    """A structured event. ``name`` is e.g. ``"sf.request"``; ``fields`` never hold secrets."""

    name: str
    fields: Mapping[str, Any]
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __getitem__(self, key: str) -> Any:
        return self.fields[key]

    def get(self, key: str, default: Any = None) -> Any:
        """Field ``key``, or ``default`` when absent."""
        return self.fields.get(key, default)


@runtime_checkable
class Observer(Protocol):
    """Receives every event. MUST NOT raise; exceptions are swallowed and counted."""

    def on_event(self, event: Event) -> None:
        """Handle one event. Must be fast and must not raise."""
        ...


class ObserverHub:
    """Fan-out to observers, isolating failures."""

    def __init__(self, observers: Iterable[Observer] = ()) -> None:
        self._observers: tuple[Observer, ...] = tuple(observers)
        self._lock = threading.Lock()
        self.errors = 0

    @property
    def observers(self) -> tuple[Observer, ...]:
        """The registered observers, in order."""
        return self._observers

    def add(self, observer: Observer) -> None:
        """Register another observer (it receives events from now on)."""
        with self._lock:
            self._observers = (*self._observers, observer)

    def emit(self, name: str, fields: Mapping[str, Any]) -> Event:
        """Send an event to every observer; exceptions are swallowed and counted in ``errors``."""
        event = Event(name, dict(fields))
        for obs in self._observers:
            try:
                obs.on_event(event)
            except Exception:
                with self._lock:
                    self.errors += 1
        return event
