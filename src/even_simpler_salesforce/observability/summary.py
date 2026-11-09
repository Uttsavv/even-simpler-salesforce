"""Per-unit-of-work roll-up (§11.5)."""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Iterator
from contextvars import ContextVar
from typing import Any

from .events import Event

__all__ = ["Summary", "capture", "record_event"]


class Summary:
    """Accumulates ``sf.request`` events emitted inside a :func:`capture` block."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.calls = 0
        self.ms = 0.0
        self.errors: list[str] = []
        self.retries = 0
        self.refreshed = False
        self.api_used: int | None = None
        self.api_max: int | None = None
        self.ops: dict[str, int] = {}

    def add(self, event: Event) -> None:
        if event.name == "sf.auth.refreshed":
            if event.fields.get("initial"):
                return  # first token acquisition is not a refresh
            with self._lock:
                self.refreshed = True
            return
        if event.name != "sf.request":
            return
        f = event.fields
        with self._lock:
            self.calls += 1
            self.ms += float(f.get("duration_ms") or 0)
            self.retries += max(0, int(f.get("attempts") or 1) - 1)
            if f.get("refreshed"):
                self.refreshed = True
            if f.get("outcome") == "error":
                self.errors.append(str(f.get("error_code") or f.get("error_class") or "error"))
            if f.get("api_used") is not None:
                self.api_used = f["api_used"]
                self.api_max = f.get("api_max")
            op = str(f.get("operation") or "raw")
            self.ops[op] = self.ops.get(op, 0) + 1

    def as_fields(self) -> dict[str, Any]:
        """Flat fields for the app's own canonical log line."""
        with self._lock:
            return {
                "sf_calls": self.calls,
                "sf_ms": round(self.ms),
                "sf_errors": list(self.errors),
                "sf_retries": self.retries,
                "sf_refreshed": self.refreshed,
                "sf_api_used": self.api_used,
                "sf_api_max": self.api_max,
                "sf_ops": dict(self.ops),
            }


_ACTIVE: ContextVar[tuple[Summary, ...]] = ContextVar("esf_summaries", default=())


@contextlib.contextmanager
def capture() -> Iterator[Summary]:
    """Collect a roll-up of every Salesforce call made inside the block.

    Contextvar-scoped: works across awaits and child clients. Nested blocks each get the calls.
    """
    summary = Summary()
    token = _ACTIVE.set((*_ACTIVE.get(), summary))
    try:
        yield summary
    finally:
        _ACTIVE.reset(token)


def record_event(event: Event) -> None:
    for s in _ACTIVE.get():
        s.add(event)
