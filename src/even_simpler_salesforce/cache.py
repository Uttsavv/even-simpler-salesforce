"""Cache protocol and the default in-memory TTL/LRU cache (§12, O12)."""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any, Protocol, runtime_checkable

__all__ = ["Cache", "MemoryCache"]


@runtime_checkable
class Cache(Protocol):
    """Shared cache used for describe results.

    Implement this to share describes across processes (e.g. Redis). Values are plain
    JSON-compatible data. Implementations must be thread-safe and should be fast: the async
    client calls them inline.
    """

    def get(self, key: str) -> Any | None: ...

    def set(self, key: str, value: Any, ttl: float) -> None: ...

    def delete(self, key: str) -> None: ...


class MemoryCache:
    """Thread-safe in-memory cache with per-entry TTL and LRU eviction."""

    def __init__(self, max_entries: int = 500, *, clock: Any = time.monotonic) -> None:
        self._max = max_entries
        self._data: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self._lock = threading.Lock()
        self._clock = clock

    def get(self, key: str) -> Any | None:
        with self._lock:
            item = self._data.get(key)
            if item is None:
                return None
            expires, value = item
            if expires <= self._clock():
                del self._data[key]
                return None
            self._data.move_to_end(key)
            return value

    def set(self, key: str, value: Any, ttl: float) -> None:
        with self._lock:
            self._data[key] = (self._clock() + ttl, value)
            self._data.move_to_end(key)
            while len(self._data) > self._max:
                self._data.popitem(last=False)

    def delete(self, key: str) -> None:
        with self._lock:
            self._data.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def __len__(self) -> int:
        return len(self._data)
