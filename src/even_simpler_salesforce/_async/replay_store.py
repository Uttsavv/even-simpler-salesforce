"""Replay stores for Pub/Sub subscriptions (§9.16.4): the protocol and an in-memory store."""

from __future__ import annotations

import threading
from typing import Protocol, runtime_checkable

__all__ = ["AsyncInMemoryReplayStore", "AsyncReplayStore"]


@runtime_checkable
class AsyncReplayStore(Protocol):
    """Where a subscription keeps its position, so a restart resumes where it stopped.

    ``sf.pubsub.subscribe(..., replay_store=store)`` loads the position before it subscribes
    and saves the replay id of the last processed event after each batch and on a normal exit.
    The key is ``"{org_id}:{topic}"``, so a sandbox refresh or a second org never resumes from
    another org's position.

    Contract:

    1. ``load`` returns exactly the bytes that the last ``save`` for that key stored.
    2. ``load`` of a key that was never saved returns ``None``.
    3. ``save`` overwrites; keys are independent of each other.

    Use ``esf.testing.replay_store_contract`` to check your implementation.
    """

    async def load(self, key: str) -> bytes | None:
        """The stored replay id for ``key``, or ``None``."""
        ...

    async def save(self, key: str, replay_id: bytes) -> None:
        """Store ``replay_id`` as the position for ``key``, replacing any earlier value."""
        ...


class AsyncInMemoryReplayStore:
    """A dict. Lost on restart: use it for tests, or when replaying from ``replay=`` after a
    restart is fine. Safe to share between subscriptions and threads."""

    def __init__(self) -> None:
        self._data: dict[str, bytes] = {}
        self._guard = threading.Lock()

    def __repr__(self) -> str:
        return f"<{type(self).__name__} keys={len(self._data)}>"

    async def load(self, key: str) -> bytes | None:
        """The stored replay id for ``key``, or ``None``."""
        with self._guard:
            return self._data.get(key)

    async def save(self, key: str, replay_id: bytes) -> None:
        """Store ``replay_id`` for ``key``."""
        if not isinstance(replay_id, bytes) or not replay_id:  # pyright: ignore[reportUnnecessaryIsInstance]
            raise ValueError("replay_id must be non-empty bytes")
        with self._guard:
            self._data[key] = bytes(replay_id)
